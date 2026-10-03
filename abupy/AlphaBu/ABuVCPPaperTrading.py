# -*- encoding: utf-8 -*-
"""Persistent close-to-next-open paper trading for the frozen VCP candidate."""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from .ABuPortfolioRisk import PortfolioRiskEngine
from .ABuTradeIntent import ApprovedOrder, Position, Reservation, TradeIntent
from .ABuVCPStrategy import VCPPositionState, VCPStrategy, make_vcp_exit_engine


STATE_VERSION = "vcp_paper_v1"
EXPERIMENT = "h_residual_stop_trailing_stagnation"
VARIANT = "residual_core"
EXIT_PROFILE = "stop_trailing_stagnation_v2"


def _next_month(value):
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return value.replace(year=year, month=month,
                         day=min(value.day, calendar.monthrange(year, month)[1]))


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _trade_intent(payload):
    payload = dict(payload)
    payload["required_fields"] = tuple(payload.get("required_fields", ()))
    payload["missing_fields"] = tuple(payload.get("missing_fields", ()))
    return TradeIntent(**payload)


def _reservation(payload):
    payload = dict(payload)
    payload["reason_codes"] = tuple(payload.get("reason_codes", ()))
    return Reservation(**payload)


def _git_head(root):
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True).strip()
    except Exception:
        return "unknown"


def implementation_hashes(root):
    root = Path(root)
    paths = (
        "abupy/AlphaBu/ABuVCPPaperTrading.py",
        "abupy/AlphaBu/ABuVCPStrategy.py",
        "abupy/AlphaBu/ABuPortfolioRisk.py",
        "abupy/AlphaBu/ABuPortfolioExecutor.py",
        "scripts/update_paper_market_data.py",
        "configs/selection/vcp_core_v1.json",
        "configs/selection/vcp_attention_v1.json",
        "configs/selection/vcp_residual_v2.json",
        "configs/selection/risk_v1.json",
    )
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in paths}


def _last_close(panel, day):
    values = panel.exec_close[:day + 1].astype(float)
    result = np.full(values.shape[1], np.nan, dtype=float)
    for column in range(values.shape[1]):
        valid = values[:, column][np.isfinite(values[:, column]) &
                                  (values[:, column] > 0)]
        if len(valid):
            result[column] = valid[-1]
    return result


def _restore_executor(panel, state, execution_config):
    executor = PortfolioExecutor(panel, execution_config)
    account = state["account"]
    executor.cash = float(account["cash"])
    executor.reserved_cash = float(account["reserved_cash"])
    executor.positions = {
        symbol: Position(**payload)
        for symbol, payload in state["active"]["positions"].items()
    }
    executor.orders = [ApprovedOrder(**payload)
                       for payload in state["active"]["orders"]]
    executor.reservations = {
        order_id: _reservation(payload)
        for order_id, payload in state["active"]["reservations"].items()
    }
    executor._cash_receivables = {
        int(day): rows for day, rows in state["active"].get(
            "cash_receivables", {}).items()
    }
    executor._share_receivables = {
        int(day): rows for day, rows in state["active"].get(
            "share_receivables", {}).items()
    }
    last_date = int(state["last_processed_date"])
    day = int(np.searchsorted(panel.dates, last_date, side="right") - 1)
    executor.last_close = _last_close(panel, day)
    return executor


def _serialize_active(executor, exit_engine, intent_lookup, entry_intents):
    return {
        "positions": {symbol: asdict(position)
                      for symbol, position in sorted(executor.positions.items())},
        "orders": [asdict(order) for order in executor.orders],
        "reservations": {order_id: asdict(item)
                         for order_id, item in executor.reservations.items()},
        "cash_receivables": executor._cash_receivables,
        "share_receivables": executor._share_receivables,
        "exit_states": {symbol: asdict(item)
                        for symbol, item in exit_engine.states.items()},
        "intent_lookup": {key: asdict(item) for key, item in intent_lookup.items()},
        "entry_intents": {key: asdict(item) for key, item in entry_intents.items()},
    }


def _bind_next_session(executor, date):
    orders = []
    for order in executor.orders:
        if int(order.valid_session) == 0:
            updated = replace(order, valid_session=int(date))
            reservation = executor.reservations.get(order.order_id)
            if reservation is not None:
                executor.reservations[order.order_id] = replace(
                    reservation, expires_on=int(date))
            orders.append(updated)
        else:
            orders.append(order)
    executor.orders = orders


def _freeze_new_orders(executor, order_ids):
    frozen = []
    for order in executor.orders:
        if order.order_id in order_ids:
            order = replace(order, valid_session=0)
            reservation = executor.reservations.get(order.order_id)
            if reservation is not None:
                executor.reservations[order.order_id] = replace(
                    reservation, expires_on=0)
        frozen.append(order)
    executor.orders = frozen


def _approve_close_orders(panel, day, executor, strategy, risk, exit_engine,
                          intent_lookup, entry_intents, history,
                          allow_terminal):
    date = int(panel.dates[day])
    created_ids = set()
    exit_rows = []
    for symbol in sorted(executor.positions):
        if any(order.side == "sell" and order.symbol == symbol
               for order in executor.orders):
            continue
        state = exit_engine.states.get(symbol)
        if state is None:
            continue
        reason = exit_engine.signal(day, symbol)
        if not reason:
            continue
        position = executor.positions[symbol]
        entry = entry_intents[symbol]
        sell = TradeIntent(
            intent_id="paper-exit-{}-{}-{}".format(date, symbol, reason),
            strategy_id=entry.strategy_id, strategy_version="paper_v1",
            signal_asof=date, symbol=symbol, side="sell",
            metadata={"exit_reason": reason},
        )
        order, reservation = executor.approve_order(
            sell, position.quantity, date)
        if order is not None:
            created_ids.add(order.order_id)
            history["orders"].append(asdict(order))
            history["reservations"].append(asdict(reservation))
            exit_rows.append({"date": date, "symbol": symbol, "reason": reason})
    history["exit_reasons"].extend(exit_rows)

    intents = strategy.generate_intents(
        day, VARIANT, allow_terminal=allow_terminal)
    history["intents"].extend(asdict(item) for item in intents)
    held_or_ordered = set(executor.positions) | {
        order.symbol for order in executor.orders if order.side == "buy"
    }
    candidates = [item for item in intents if item.symbol not in held_or_ordered]
    selling = sum(order.side == "sell" and order.symbol in executor.positions
                  for order in executor.orders)
    possible_slots = max(0, 10 - len(executor.positions) + selling)
    candidates = candidates[:possible_slots]
    results = risk.approve_batch(executor, candidates, day, day)
    for order, reservation, decision in results:
        history["risk_decisions"].append(asdict(decision))
        history["reservations"].append(asdict(reservation))
        if order is not None:
            created_ids.add(order.order_id)
            history["orders"].append(asdict(order))
            intent_lookup[order.intent_id] = next(
                item for item in candidates if item.intent_id == order.intent_id)
    _freeze_new_orders(executor, created_ids)
    return len(intents), len(created_ids), len(exit_rows)


def _export_frames(paper_dir, state):
    paper_dir = Path(paper_dir)
    mapping = {
        "fills": "fills.csv", "daily_nav": "daily_nav.csv",
        "exit_reasons": "exit_reasons.csv", "orders": "orders.csv",
        "reservations": "reservations.csv", "position_events": "position_events.csv",
    }
    for key, filename in mapping.items():
        pd.DataFrame(state["history"][key]).to_csv(paper_dir / filename, index=False)
    for key in ("intents", "risk_decisions", "runs"):
        with (paper_dir / (key + ".jsonl")).open("w", encoding="utf-8") as output:
            for row in state["history"][key]:
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _summary(state, paper_dir):
    nav = pd.DataFrame(state["history"]["daily_nav"])
    latest = nav.iloc[-1] if len(nav) else pd.Series({
        "date": state["last_processed_date"], "capital": state["account"]["cash"],
        "exposure": 0.0, "holdings": 0})
    capital = np.r_[float(state["initial_cash"]),
                    pd.to_numeric(nav.get("capital", pd.Series(dtype=float))).to_numpy()]
    drawdown = capital / np.maximum.accumulate(capital) - 1
    fills = pd.DataFrame(state["history"]["fills"])
    buys = int(((fills.get("side") == "buy") &
                (fills.get("status") == "filled")).sum()) if len(fills) else 0
    sells = int(((fills.get("side") == "sell") &
                 (fills.get("status") == "filled")).sum()) if len(fills) else 0
    review_due = datetime.now().astimezone().date().isoformat() >= state["observation_date"]
    summary = {
        "status": "REVIEW_DUE" if review_due else "RUNNING",
        "strategy": "vcp_residual_v2",
        "experiment": EXPERIMENT,
        "launched_at": state["launched_at"],
        "observation_date": state["observation_date"],
        "latest_market_date": int(latest["date"]),
        "capital": float(latest["capital"]),
        "return_pct": (float(latest["capital"]) / float(state["initial_cash"]) - 1) * 100,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "cash": float(state["account"]["cash"]),
        "exposure_pct": float(latest.get("exposure", 0.0) * 100),
        "positions": len(state["active"]["positions"]),
        "pending_orders": len(state["active"]["orders"]),
        "filled_buys": buys, "filled_sells": sells,
        "research_only": True,
    }
    _atomic_json(Path(paper_dir) / "summary.json", summary)
    report = """# VCP 模拟盘日报

- 状态：`{status}`
- 策略：`vcp_residual_v2 + stop_trailing_stagnation_v2`
- 启动时间：{launched_at}
- 一个月观察日：{observation_date}
- 最新行情日：{latest_market_date}
- 初始资金：{initial_cash:,.2f} 元
- 当前资产：{capital:,.2f} 元
- 累计收益：{return_pct:+.3f}%
- 最大回撤：{max_drawdown_pct:.3f}%
- 当前现金：{cash:,.2f} 元
- 当前仓位：{exposure_pct:.2f}%
- 持仓数量：{positions}
- 待执行订单：{pending_orders}
- 已成交买入 / 卖出：{filled_buys} / {filled_sells}

该账户是研究模拟盘，不发送真实证券订单。信号在收盘后冻结，下一交易日按统一开盘成交模型处理。
""".format(initial_cash=float(state["initial_cash"]), **summary)
    (Path(paper_dir) / "REPORT.md").write_text(report, encoding="utf-8")
    return summary


def initialize_paper(panel, paper_dir, core, attention, residual, risk_config,
                     repository_root, now=None):
    paper_dir = Path(paper_dir)
    state_path = paper_dir / "state.json"
    if state_path.exists():
        raise FileExistsError("paper state already exists: {}".format(state_path))
    now = now or datetime.now().astimezone()
    day = len(panel.dates) - 1
    date = int(panel.dates[day])
    execution = ExecutionConfig(slippage_bps=25.0, mode="pit_corrected",
                                max_positions=10)
    executor = PortfolioExecutor(panel, execution)
    executor.last_close = _last_close(panel, day)
    strategy = VCPStrategy(panel, core, attention, residual)
    risk = PortfolioRiskEngine(panel, risk_config)
    exits = make_vcp_exit_engine(panel, EXIT_PROFILE)
    history = {name: [] for name in (
        "fills", "daily_nav", "intents", "risk_decisions", "exit_reasons",
        "orders", "reservations", "position_events", "runs")}
    intent_lookup, entry_intents = {}, {}
    intents, orders, exit_orders = _approve_close_orders(
        panel, day, executor, strategy, risk, exits, intent_lookup,
        entry_intents, history, allow_terminal=True)
    history["daily_nav"].append({
        "date": date, "cash": execution.initial_cash, "stocks": 0.0,
        "capital": execution.initial_cash, "exposure": 0.0, "holdings": 0,
        "stale_value": 0.0, "reserved_cash": executor.reserved_cash,
        "liquidation_nav_1_limit": execution.initial_cash,
        "liquidation_nav_3_limits": execution.initial_cash,
        "liquidation_nav_5_limits": execution.initial_cash,
        "liquidation_nav_zero_stale": execution.initial_cash,
    })
    history["runs"].append({
        "ran_at": now.isoformat(), "mode": "initialize", "market_date": date,
        "signals": intents, "orders_created": orders, "exit_orders": exit_orders,
    })
    state = {
        "state_version": STATE_VERSION, "experiment": EXPERIMENT,
        "launched_at": now.isoformat(),
        "observation_date": _next_month(now.date()).isoformat(),
        "initial_cash": execution.initial_cash,
        "last_processed_date": date, "panel_start_date": 20200101,
        "repository_commit": _git_head(repository_root),
        "implementation_hashes": implementation_hashes(repository_root),
        "config_hashes": {
            "core": core.sha256, "attention": attention.sha256,
            "residual": residual.sha256, "risk": risk_config.sha256,
        },
        "execution_config": asdict(execution),
        "account": {"cash": executor.cash,
                    "reserved_cash": executor.reserved_cash},
        "active": _serialize_active(
            executor, exits, intent_lookup, entry_intents),
        "history": history,
    }
    _atomic_json(state_path, state)
    _export_frames(paper_dir, state)
    summary = _summary(state, paper_dir)
    return state, summary


def run_paper_sessions(panel, paper_dir, core, attention, residual, risk_config,
                       now=None):
    paper_dir = Path(paper_dir)
    state_path = paper_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("state_version") != STATE_VERSION:
        raise ValueError("paper state version mismatch")
    expected_hashes = {
        "core": core.sha256, "attention": attention.sha256,
        "residual": residual.sha256, "risk": risk_config.sha256,
    }
    if state.get("config_hashes") != expected_hashes:
        raise ValueError("frozen paper configuration changed")
    root = Path(__file__).resolve().parents[2]
    if (state.get("implementation_hashes") and
            state["implementation_hashes"] != implementation_hashes(root)):
        raise ValueError("frozen paper implementation changed")
    new_days = np.flatnonzero(panel.dates > int(state["last_processed_date"]))
    if not len(new_days):
        summary = _summary(state, paper_dir)
        return state, summary, {"status": "no_new_session"}
    execution = ExecutionConfig(**state["execution_config"])
    executor = _restore_executor(panel, state, execution)
    strategy = VCPStrategy(panel, core, attention, residual)
    risk = PortfolioRiskEngine(panel, risk_config)
    exits = make_vcp_exit_engine(panel, EXIT_PROFILE)
    exits.states = {symbol: VCPPositionState(**payload)
                    for symbol, payload in state["active"]["exit_states"].items()}
    intent_lookup = {key: _trade_intent(payload)
                     for key, payload in state["active"]["intent_lookup"].items()}
    entry_intents = {key: _trade_intent(payload)
                     for key, payload in state["active"]["entry_intents"].items()}
    history = state["history"]
    totals = {"sessions": 0, "filled_buys": 0, "filled_sells": 0,
              "rejections": 0, "signals": 0, "orders_created": 0,
              "exit_orders": 0}
    for day_value in new_days:
        day = int(day_value)
        date = int(panel.dates[day])
        _bind_next_session(executor, date)
        fills = executor.process_open(day)
        for fill in fills:
            history["fills"].append(asdict(fill))
            if fill.status != "filled":
                totals["rejections"] += 1
                continue
            if fill.side == "buy":
                intent = intent_lookup[fill.intent_id]
                exits.register_entry(intent, fill, day)
                entry_intents[fill.symbol] = intent
                totals["filled_buys"] += 1
            else:
                exits.remove(fill.symbol)
                entry_intents.pop(fill.symbol, None)
                totals["filled_sells"] += 1
        row = executor.process_close(day)
        history["daily_nav"].append(row)
        history["position_events"].extend(
            asdict(item) for item in executor.position_events)
        executor.position_events = []
        signals, orders, exit_orders = _approve_close_orders(
            panel, day, executor, strategy, risk, exits, intent_lookup,
            entry_intents, history, allow_terminal=(day == len(panel.dates) - 1))
        totals["sessions"] += 1
        totals["signals"] += signals
        totals["orders_created"] += orders
        totals["exit_orders"] += exit_orders
        active_intent_ids = {order.intent_id for order in executor.orders
                             if order.side == "buy"}
        active_intent_ids.update(item.intent_id for item in entry_intents.values())
        intent_lookup = {key: value for key, value in intent_lookup.items()
                         if key in active_intent_ids}
        state["last_processed_date"] = date
    now = now or datetime.now().astimezone()
    history["runs"].append({"ran_at": now.isoformat(), "mode": "daily", **totals,
                            "market_date": int(state["last_processed_date"])})
    state["account"] = {"cash": executor.cash,
                        "reserved_cash": executor.reserved_cash}
    state["active"] = _serialize_active(
        executor, exits, intent_lookup, entry_intents)
    _atomic_json(state_path, state)
    _export_frames(paper_dir, state)
    summary = _summary(state, paper_dir)
    return state, summary, {"status": "processed", **totals}
