#!/usr/bin/env python3
"""Run the frozen low-frequency Alpha158-lite portfolio diagnostic."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    Alpha158LiteExitEngine, Alpha158LiteFeatureEngine,
    Alpha158LiteLowTurnoverPolicy, load_alpha158_lite_config,
    load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioExecutor import (  # noqa: E402
    ExecutionConfig, PortfolioExecutor,
)
from abupy.AlphaBu.ABuPortfolioRisk import (  # noqa: E402
    PortfolioRiskEngine, load_risk_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTradeIntent import TradeIntent, make_record_id  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from scripts.backtest_alpha158_lite_v1 import (  # noqa: E402
    _save_audit, _seed_marks, fixed_path_cost_attribution, rank_frame,
)
from scripts.backtest_vcp_context_v1 import _trade_metrics  # noqa: E402


def _register_once(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None):
    matches = [item for item in read_trial_registry(path)
               if item["trial_id"] == trial_id]
    if matches:
        if (matches[0]["hypothesis"] != hypothesis or
                matches[0]["configuration"] != configuration):
            raise ValueError("registered low-turnover trial differs")
        return matches[0]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def holding_session_statistics(fills, panel):
    if fills.empty:
        return {"median_holding_sessions": np.nan,
                "mean_holding_sessions": np.nan,
                "p90_holding_sessions": np.nan}
    buys = fills[(fills.side == "buy") & fills.status.eq("filled")]
    sells = fills[(fills.side == "sell") & fills.status.eq("filled")]
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    values = []
    for symbol, buy_group in buys.groupby("symbol"):
        sell_group = sells[sells.symbol.eq(symbol)].sort_values("date")
        for buy in buy_group.sort_values("date").itertuples():
            eligible = sell_group[sell_group.date >= buy.date]
            if eligible.empty:
                continue
            sell = eligible.iloc[0]
            sell_group = sell_group.drop(eligible.index[0])
            values.append(date_index[int(sell.date)]-date_index[int(buy.date)])
    if not values:
        return {"median_holding_sessions": np.nan,
                "mean_holding_sessions": np.nan,
                "p90_holding_sessions": np.nan}
    return {
        "median_holding_sessions": float(np.median(values)),
        "mean_holding_sessions": float(np.mean(values)),
        "p90_holding_sessions": float(np.quantile(values, .90)),
    }


def annual_returns(curve, initial_cash=1_000_000.0):
    rows = []
    previous = float(initial_cash)
    dated = curve.assign(year=curve.date.astype(int)//10000)
    for year, group in dated.groupby("year", sort=True):
        ending = float(group.capital.iloc[-1])
        rows.append({"year": int(year),
                     "return_pct": float((ending/previous-1)*100),
                     "end_capital": ending})
        previous = ending
    return pd.DataFrame(rows)


def run_low_turnover(panel, scores, source_config, policy_config, risk_config,
                     end_date, sync_dynamic_stops=False,
                     position_add_policy=None,
                     position_add_execution_mode="executable",
                     initial_cash=1_000_000.0):
    grouped = {int(date): group.sort_values(
        ["daily_rank", "symbol"], kind="mergesort")
        for date, group in scores.groupby("signal_asof", sort=True)}
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    first_signal = int(scores.signal_asof.min())
    last_signal = min(int(scores.signal_asof.max()), int(end_date))
    first, last = date_index[first_signal], date_index[last_signal]
    strategy_config = replace(
        source_config, strategy_version=policy_config.strategy_version,
        entry_top_k=policy_config.target_positions,
        portfolio_score_depth=max(
            policy_config.entry_rank_limit,
            policy_config.retention_rank_limit))
    executor = PortfolioExecutor(panel, ExecutionConfig(
        initial_cash=float(initial_cash),
        slippage_bps=source_config.label_slippage_bps, mode="pit_corrected",
        max_positions=policy_config.target_positions))
    _seed_marks(executor, panel, first)
    risk = PortfolioRiskEngine(panel, risk_config)
    add_coordinator = None
    if position_add_policy is not None:
        from abupy.AlphaBu.ABuPositionAddResearch import PositionAddCoordinator
        add_coordinator = PositionAddCoordinator(
            panel, position_add_policy, risk,
            data_version="alpha158_position_add_v1",
            execution_mode=position_add_execution_mode)
    features = Alpha158LiteFeatureEngine(panel, strategy_config)
    exits = Alpha158LiteExitEngine(panel, strategy_config)
    policy = Alpha158LiteLowTurnoverPolicy(policy_config)
    entry_intents = {}
    decisions, exit_rows, selection_rows = [], [], []
    risk_state_rows, risk_position_rows = [], []
    pending_exits, pending_entries = [], []

    for day in range(first, last+1):
        signal_day = day-1
        if day > first:
            signal_date = int(panel.dates[signal_day])
            for symbol, reason in pending_exits:
                position = executor.positions.get(symbol)
                if position is None or any(
                        order.side == "sell" and order.symbol == symbol
                        for order in executor.orders):
                    continue
                entry = entry_intents[symbol]
                sell = TradeIntent(
                    intent_id=make_record_id(
                        "alpha158-low-turnover-exit", signal_date,
                        symbol, reason),
                    strategy_id=entry.strategy_id, strategy_version="1",
                    signal_asof=signal_date, symbol=symbol, side="sell",
                    metadata={"exit_reason": reason},
                )
                executor.approve_order(
                    sell, position.quantity, int(panel.dates[day]))
                exit_rows.append({"date": signal_date, "symbol": symbol,
                                  "reason": reason})
            held_or_ordered = set(executor.positions) | {
                order.symbol for order in executor.orders
                if order.side == "buy"}
            exiting = sum(symbol in executor.positions
                          for symbol, _ in pending_exits)
            slots = max(
                0, policy_config.target_positions-len(executor.positions)+exiting)
            for row in pending_entries:
                if slots <= 0:
                    break
                symbol = str(row["symbol"])
                if symbol in held_or_ordered:
                    continue
                intent = features.make_intent(
                    signal_day, int(row["column"]),
                    float(row[policy_config.score_column]))
                if intent is None:
                    continue
                order, _, decision = risk.approve(
                    executor, intent, signal_day, day)
                decisions.append(decision)
                selection_rows.append({
                    "signal_asof": signal_date, "symbol": symbol,
                    "score": float(row[policy_config.score_column]),
                    "daily_rank": int(row["daily_rank"]),
                    "risk_decision": decision.decision,
                    "order_created": order is not None,
                })
                entry_intents[symbol] = intent
                held_or_ordered.add(symbol)
                if order is not None:
                    slots -= 1

        fills_today = executor.process_open(day)
        for fill in fills_today:
            if fill.status != "filled":
                continue
            if fill.side == "sell":
                exits.remove(fill.symbol)
                entry_intents.pop(fill.symbol, None)
            else:
                if fill.position_effect == "INCREASE":
                    continue
                intent = entry_intents[fill.symbol]
                exits.register_entry(intent, fill, day)
        executor.process_close(day)

        pending_exits, pending_entries = [], []
        if day >= last:
            continue
        event_symbols = set()
        add_stop_overrides = {}
        for symbol in sorted(executor.positions):
            if any(order.side == "sell" and order.symbol == symbol
                   for order in executor.orders):
                continue
            reason = exits.signal(day, symbol)
            current_stop = (exits.current_stop_raw(day, symbol)
                            if sync_dynamic_stops or add_coordinator is not None
                            else None)
            if sync_dynamic_stops:
                if current_stop is not None:
                    executor.update_position_stop(symbol, current_stop)
            if add_coordinator is not None and current_stop is not None:
                add_stop_overrides[symbol] = current_stop
            if reason:
                event_symbols.add(symbol)
                pending_exits.append((symbol, reason))
        if (day-first) % policy_config.review_interval_sessions == 0:
            daily = grouped.get(int(panel.dates[day]), pd.DataFrame())
            holding_sessions = {
                symbol: day-exits.states[symbol].entry_day+1
                for symbol in executor.positions if symbol in exits.states}
            rank_exits, entry_symbols = policy.review(
                daily, executor.positions, holding_sessions,
                blocked_exits=event_symbols)
            pending_exits.extend((symbol, "PERSISTENT_RANK_EXIT")
                                 for symbol in rank_exits)
            lookup = {str(row.symbol): row._asdict()
                      for row in daily.itertuples(index=False)}
            pending_entries = [lookup[symbol] for symbol in entry_symbols
                               if symbol in lookup]
        if add_coordinator is not None:
            blocked_symbols = {symbol for symbol, _ in pending_exits}
            blocked_trades = {
                trade.trade_id
                for trade in executor.position_ledger.active_trades()
                if trade.symbol in blocked_symbols}
            add_results = add_coordinator.evaluate_active(
                executor, day, day+1, blocked_trades,
                add_stop_overrides)
            decisions.extend(item[3] for item in add_results
                             if item[3] is not None)

        risk_equity, risk_gross, open_risk, industries, same_day = \
            risk._state(executor, day)
        risk_state_rows.append({
            "date": int(panel.dates[day]), "equity": risk_equity,
            "gross_exposure_cash": risk_gross,
            "open_risk_cash": open_risk,
            "same_day_new_risk_cash": same_day,
            "industry_open_risk_cash": dict(industries),
        })
        for row in risk._portfolio(executor, day):
            if row.get("pending", False):
                continue
            risk_position_rows.append({
                "date": int(panel.dates[day]), **row})

    curve = executor.curve_frame()
    fills = executor.fills_frame()
    initial = executor.config.initial_cash
    capital = np.r_[initial, curve.capital.to_numpy()]
    liquidation_capital = np.r_[
        initial, curve.liquidation_nav_3_limits.to_numpy()]
    daily_return = capital[1:]/capital[:-1]-1
    cutoff = np.quantile(daily_return, .05)
    return_pct = float((capital[-1]/initial-1)*100)
    start_timestamp = pd.Timestamp(str(int(panel.dates[first])))
    end_timestamp = pd.Timestamp(str(int(panel.dates[last])))
    elapsed_years = max((end_timestamp-start_timestamp).days/365.25, 1/365.25)
    trades = _trade_metrics(fills)
    result = {
        "experiment": policy_config.strategy_version,
        "start": int(panel.dates[first]), "end": int(panel.dates[last]),
        "return_pct": return_pct,
        "benchmark_return_pct": float(
            (panel.benchmark_close[last]/panel.benchmark_close[first]-1)*100),
        "max_drawdown_pct": float(
            (capital/np.maximum.accumulate(capital)-1).min()*100),
        "liquidation_3_limits_return_pct": float(
            (liquidation_capital[-1]/initial-1)*100),
        "liquidation_3_limits_max_drawdown_pct": float(
            (liquidation_capital/np.maximum.accumulate(
                liquidation_capital)-1).min()*100),
        "daily_expected_shortfall_95_pct": float(
            daily_return[daily_return <= cutoff].mean()*100),
        "average_exposure_pct": float(curve.exposure.mean()*100),
        "filled_buys": int(((fills.side == "buy") &
                            fills.status.eq("filled")).sum()),
        "filled_sells": int(((fills.side == "sell") &
                             fills.status.eq("filled")).sum()),
        "buys_per_year": float(
            ((fills.side == "buy") & fills.status.eq("filled")).sum()/
            elapsed_years),
        "risk_rejected": sum(item.decision == "rejected" for item in decisions),
        "risk_reduced": sum(item.decision == "reduced" for item in decisions),
        "entry_clusters": int(fills[(fills.side == "buy") &
                                     fills.status.eq("filled")].date.nunique()),
        "open_positions_end": int(len(executor.positions)),
        "pending_orders_end": int(len(executor.orders)),
        "slippage_bps": float(source_config.label_slippage_bps),
        "dynamic_stop_sync": bool(sync_dynamic_stops),
        **trades,
        **holding_session_statistics(fills, panel),
    }
    result.update(fixed_path_cost_attribution(fills, return_pct, initial))
    audit = {
        "curve": curve, "fills": fills, "decisions": decisions,
        "exits": exit_rows, "selection": selection_rows,
        "orders": executor.order_history,
        "reservations": executor.reservation_history,
        "policy_evaluations": (
            list(add_coordinator.runner.evaluations.values())
            if add_coordinator is not None else []),
        "add_proposals": (
            list(add_coordinator.runner.proposals.values())
            if add_coordinator is not None else []),
        "fill_allocations": list(executor.position_ledger.fill_allocations),
        "position_lots": list(executor.position_ledger.lots.values()),
        "lot_dispositions": list(executor.position_ledger.lot_dispositions),
        "logical_trades": list(executor.position_ledger.logical_trades.values()),
        "risk_states_daily": risk_state_rows,
        "risk_positions_daily": risk_position_rows,
    }
    return result, audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/"
        "oos_predictions.csv.gz"))
    parser.add_argument("--source-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT/"configs/selection/risk_v1.json")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_low_turnover_v3"))
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument("--sync-dynamic-stops", action="store_true",
                        help="publish executable trailing stops to risk sizing")
    args = parser.parse_args()

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("low-turnover source config hash mismatch")
    risk = load_risk_config(args.risk_config)
    registration_config = {
        "source": asdict(source), "policy": asdict(policy),
        "risk_config_sha256": risk.sha256,
        "execution": {"slippage_bps": 25.0, "mode": "pit_corrected"},
        "dynamic_stop_sync": bool(args.sync_dynamic_stops),
        "parameter_search": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    registry = args.output_dir/"trial_registry.jsonl"
    trial_id = "alpha158-lite-low-turnover-v3-frozen-20261003"
    hypothesis = (
        "Five-session reviews, persistent entry and exit ranks, and at most "
        "one rank replacement per review reduce annual buys below 120 while "
        "preserving immediate event risk exits.")
    registration = _register_once(
        registry, trial_id, hypothesis, registration_config)

    predictions = pd.read_csv(
        args.predictions, usecols=["signal_asof", "symbol", "column",
                                   policy.score_column], dtype={"symbol": str})
    depth = max(policy.entry_rank_limit, policy.retention_rank_limit)
    scores = rank_frame(predictions, policy.score_column, depth)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    result, audit = run_low_turnover(
        panel, scores, source, policy, risk, args.end_date,
        sync_dynamic_stops=args.sync_dynamic_stops)
    _save_audit(args.output_dir/policy.strategy_version, audit)
    annual_returns(audit["curve"]).to_csv(
        args.output_dir/"year_returns.csv", index=False)
    pd.DataFrame([result]).to_csv(args.output_dir/"results.csv", index=False)
    report = {
        "strategy_version": policy.strategy_version,
        "registration_sha256": registration["record_sha256"],
        "frequency_target_buys_per_year": 120,
        "frequency_target_met": result["buys_per_year"] <= 120,
        "portfolio_stage_decision": (
            "RETAIN_FOR_FORWARD_VALIDATION"
            if result["buys_per_year"] <= 120 and
            result["return_pct"] > 0 and result["mean_r"] > 0 and
            result["liquidation_3_limits_return_pct"] > 0
            else "REJECT_AT_PORTFOLIO_STAGE"),
        "result": result, "post_hoc_diagnostic": True,
        "research_only": True,
    }
    (args.output_dir/"report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    result_hash = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    _register_once(
        registry, trial_id+"-result-"+result_hash[:12],
        "Observed diagnostic result for "+trial_id,
        registration_config, status="OBSERVED", observed_metrics=report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
