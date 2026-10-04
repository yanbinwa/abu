#!/usr/bin/env python3
"""Run preregistered VCP context ablations through the common executor."""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine, load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTradeIntent import TradeIntent, make_record_id  # noqa: E402
from abupy.AlphaBu.ABuVCPContext import (  # noqa: E402
    load_vcp_context_config, load_vcp_context_registry,
)
from abupy.AlphaBu.ABuVCPStrategy import make_vcp_exit_engine  # noqa: E402


EXPERIMENTS = (
    "baseline_replay", "market_overlay", "industry_rank",
    "industry_leader_rank",
)


def _literal(value, default):
    if isinstance(value, type(default)):
        return value
    try:
        parsed = ast.literal_eval(str(value))
        return parsed if isinstance(parsed, type(default)) else default
    except (ValueError, SyntaxError):
        return default


def load_frozen_intents(frame):
    result = []
    for row in frame.to_dict("records"):
        result.append(TradeIntent(
            intent_id=str(row["intent_id"]), strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            signal_asof=int(row["signal_asof"]), symbol=str(row["symbol"]),
            side=str(row.get("side", "buy")), score=float(row.get("score", 0.0)),
            signal_price_adjusted=float(row["signal_price_adjusted"]),
            signal_price_raw=float(row["signal_price_raw"]),
            adjustment_factor_signal=float(row["adjustment_factor_signal"]),
            initial_stop_adjusted=float(row["initial_stop_adjusted"]),
            initial_stop_raw=float(row["initial_stop_raw"]),
            industry_asof=int(row.get("industry_asof", -1)),
            required_fields=tuple(_literal(row.get("required_fields", "()"), ())),
            missing_fields=tuple(_literal(row.get("missing_fields", "()"), ())),
            max_gap_atr=float(row.get("max_gap_atr", 1.0)),
            valid_for_sessions=int(row.get("valid_for_sessions", 1)),
            r_definition_version=str(row.get("r_definition_version", "none")),
            metadata=dict(_literal(row.get("metadata", "{}"), {})),
        ))
    return result


def order_context_candidates(frame, experiment, ranking_column=None):
    if ranking_column is not None:
        if ranking_column not in frame:
            raise ValueError("missing ranking column: {}".format(ranking_column))
        return frame.sort_values(
            [ranking_column, "symbol"], ascending=[False, True],
            na_position="last", kind="mergesort")
    if experiment not in EXPERIMENTS:
        raise ValueError("unknown context experiment")
    if experiment in ("baseline_replay", "market_overlay"):
        columns, ascending = ["score", "symbol"], [False, True]
    elif experiment == "industry_rank":
        columns = ["industry_rank_value", "score", "symbol"]
        ascending = [False, False, True]
    else:
        columns = ["industry_rank_value", "leader_rank_value", "score", "symbol"]
        ascending = [False, False, False, True]
    return frame.sort_values(columns, ascending=ascending, na_position="last",
                             kind="mergesort")


def requested_quantity_for_context(risk, executor, intent, day, multiplier):
    if multiplier >= 1:
        return None
    if multiplier <= 0:
        return 0
    max_price = float(intent.metadata["max_buy_price_raw"])
    risk_per_share = max_price - float(intent.initial_stop_raw)
    equity = risk._state(executor, day)[0]
    if risk_per_share <= 0 or equity <= 0:
        return 0
    base = int(equity * risk.config.single_trade_risk_fraction /
               risk_per_share / 100) * 100
    return int(base * float(multiplier) / 100) * 100


def _trade_metrics(fills):
    if fills.empty:
        return {"closed_trades": 0, "mean_r": np.nan,
                "profit_top5_contribution": np.nan}
    buys = fills[(fills.side == "buy") & (fills.status == "filled")].copy()
    sells = fills[(fills.side == "sell") & (fills.status == "filled")].copy()
    rows = []
    for symbol, buy_group in buys.groupby("symbol"):
        sell_group = sells[sells.symbol.eq(symbol)].sort_values("date")
        for buy in buy_group.sort_values("date").itertuples():
            eligible = sell_group[sell_group.date >= buy.date]
            if eligible.empty:
                continue
            sell = eligible.iloc[0]
            buy_cash = (buy.quantity * buy.fill_price_raw + buy.commission +
                        buy.transfer_fee + buy.stamp_tax)
            sell_cash = (sell.quantity * sell.fill_price_raw - sell.commission -
                         sell.transfer_fee - sell.stamp_tax)
            pnl = float(sell_cash - buy_cash)
            r_cash = float(buy.actual_initial_r_cash)
            rows.append((pnl, pnl / r_cash if r_cash > 0 else np.nan))
            sell_group = sell_group.drop(eligible.index[0])
    if not rows:
        return {"closed_trades": 0, "mean_r": np.nan,
                "profit_top5_contribution": np.nan}
    pnl = np.array([item[0] for item in rows])
    r_values = np.array([item[1] for item in rows])
    positive_total = pnl[pnl > 0].sum()
    top5 = np.sort(pnl[pnl > 0])[-5:].sum()
    return {
        "closed_trades": len(rows),
        "mean_r": float(np.nanmean(r_values)),
        "profit_top5_contribution": (
            float(top5 / positive_total) if positive_total > 0 else np.nan),
    }


def run_experiment(panel, shadow, experiment, risk_config, start_date, end_date,
                   ranking_column=None, slippage_bps=25.0):
    dates = np.flatnonzero((panel.dates >= int(start_date)) &
                           (panel.dates <= int(end_date)))
    if not len(dates) or dates[0] == 0:
        raise ValueError("period has no prior signal date")
    first, last = int(dates[0]), int(dates[-1])
    executor = PortfolioExecutor(panel, ExecutionConfig(
        slippage_bps=slippage_bps, mode="pit_corrected", max_positions=10))
    previous = np.asarray(panel.exec_close[:first], dtype=float)
    for column in range(previous.shape[1]):
        valid = previous[:, column][np.isfinite(previous[:, column]) &
                                    (previous[:, column] > 0)]
        if len(valid):
            executor.last_close[column] = valid[-1]
    risk = PortfolioRiskEngine(panel, risk_config)
    exits = make_vcp_exit_engine(panel, "stop_trailing_stagnation_v2")
    intents = load_frozen_intents(shadow)
    lookup = {item.intent_id: item for item in intents}
    grouped = {int(date): order_context_candidates(
                   group, experiment, ranking_column=ranking_column)
               for date, group in shadow.groupby("signal_asof", sort=True)}
    entry_intents = {}
    exit_rows, overlay_rows, decision_rows = [], [], []
    pending_exits = []

    for day in range(first, last + 1):
        signal_day = day - 1
        for symbol, reason in pending_exits:
            position = executor.positions.get(symbol)
            if position is None or any(
                    order.side == "sell" and order.symbol == symbol
                    for order in executor.orders):
                continue
            entry = entry_intents[symbol]
            sell = TradeIntent(
                intent_id=make_record_id("vcp-context-exit",
                                         int(panel.dates[signal_day]), symbol, reason),
                strategy_id=entry.strategy_id, strategy_version="context_v1",
                signal_asof=int(panel.dates[signal_day]), symbol=symbol, side="sell",
                metadata={"exit_reason": reason},
            )
            executor.approve_order(sell, position.quantity, int(panel.dates[day]))
            exit_rows.append({"date": int(panel.dates[signal_day]),
                              "symbol": symbol, "reason": reason})

        date = int(panel.dates[signal_day])
        source = grouped.get(date, pd.DataFrame())
        held_or_ordered = set(executor.positions) | {
            order.symbol for order in executor.orders if order.side == "buy"}
        if len(source):
            source = source[~source.symbol.isin(held_or_ordered)]
        slots = max(0, 10 - len(executor.positions) +
                    sum(symbol in executor.positions for symbol, _ in pending_exits))
        source = source.head(slots)
        for row in source.to_dict("records"):
            intent = lookup[row["intent_id"]]
            multiplier = (float(row["market_new_risk_multiplier"])
                          if experiment == "market_overlay" else 1.0)
            overlay_rows.append({
                "intent_id": intent.intent_id, "signal_asof": date,
                "symbol": intent.symbol, "experiment": experiment,
                "market_state": row.get("market_state", "UNSPECIFIED"),
                "risk_multiplier": multiplier,
                "action": ("REJECT_NEW_RISK" if multiplier <= 0 else
                           "REDUCE_NEW_RISK" if multiplier < 1 else
                           "ALLOW_NEW_RISK"),
            })
            if multiplier <= 0:
                continue
            requested = requested_quantity_for_context(
                risk, executor, intent, signal_day, multiplier)
            order, _, decision = risk.approve(
                executor, intent, signal_day, day,
                requested_quantity=requested)
            decision_rows.append(decision)

        fills_today = executor.process_open(day)
        for fill in fills_today:
            if fill.status != "filled":
                continue
            if fill.side == "sell":
                exits.remove(fill.symbol)
                entry_intents.pop(fill.symbol, None)
            else:
                entry = lookup[fill.intent_id]
                exits.register_entry(entry, fill, day)
                entry_intents[fill.symbol] = entry
        executor.process_close(day)
        pending_exits = []
        if day < last:
            for symbol in sorted(executor.positions):
                if any(order.side == "sell" and order.symbol == symbol
                       for order in executor.orders):
                    continue
                reason = exits.signal(day, symbol)
                if reason:
                    pending_exits.append((symbol, reason))

    curve = executor.curve_frame()
    fills = executor.fills_frame()
    initial = executor.config.initial_cash
    capital = np.r_[initial, curve.capital.to_numpy()]
    daily = capital[1:] / capital[:-1] - 1
    cutoff = np.quantile(daily, .05) if len(daily) else np.nan
    trades = _trade_metrics(fills)
    result = {
        "experiment": experiment, "start": int(panel.dates[first]),
        "end": int(panel.dates[last]),
        "return_pct": float((capital[-1] / initial - 1) * 100),
        "max_drawdown_pct": float(
            (capital / np.maximum.accumulate(capital) - 1).min() * 100),
        "daily_expected_shortfall_95_pct": (
            float(daily[daily <= cutoff].mean() * 100) if len(daily) else np.nan),
        "average_exposure_pct": float(curve.exposure.mean() * 100),
        "filled_buys": int(((fills.side == "buy") &
                            (fills.status == "filled")).sum()) if len(fills) else 0,
        "filled_sells": int(((fills.side == "sell") &
                             (fills.status == "filled")).sum()) if len(fills) else 0,
        "risk_rejected": sum(item.decision == "rejected" for item in decision_rows),
        "risk_reduced": sum(item.decision == "reduced" for item in decision_rows),
        "context_rejected": sum(row["action"] == "REJECT_NEW_RISK"
                                for row in overlay_rows),
        "entry_clusters": int(fills[(fills.side == "buy") &
                                     (fills.status == "filled")].date.nunique())
        if len(fills) else 0,
        **trades,
    }
    audit = {
        "curve": curve, "fills": fills,
        "decisions": decision_rows, "exits": exit_rows,
        "overlay": overlay_rows, "orders": executor.order_history,
        "reservations": executor.reservation_history,
    }
    return result, audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shadow-intents", type=Path,
        default=Path("/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path,
                        default=Path("/Users/wjy/abu/backtests/vcp_context_v1"))
    parser.add_argument("--start-date", type=int, default=20220104)
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument("--experiments", nargs="+", choices=EXPERIMENTS,
                        default=list(EXPERIMENTS))
    parser.add_argument("--registry", type=Path, default=
                        ROOT / "configs/selection/vcp_context_experiment_registry_v1.json")
    parser.add_argument("--context-config", type=Path, default=
                        ROOT / "configs/selection/vcp_context_overlay_v1.json")
    args = parser.parse_args()

    registry = load_vcp_context_registry(args.registry)
    context_config = load_vcp_context_config(args.context_config)
    shadow = pd.read_csv(args.shadow_intents, dtype={"symbol": str})
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    risk_config = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for experiment in args.experiments:
        result, audit = run_experiment(
            panel, shadow, experiment, risk_config,
            args.start_date, args.end_date)
        results.append(result)
        target = args.output_dir / experiment
        target.mkdir(parents=True, exist_ok=True)
        audit["curve"].to_csv(target / "daily_nav.csv", index=False)
        audit["fills"].to_csv(target / "fills.csv", index=False)
        pd.DataFrame(audit["exits"]).to_csv(target / "exit_reasons.csv", index=False)
        pd.DataFrame(audit["overlay"]).to_csv(
            target / "context_decisions.csv", index=False)
        pd.DataFrame([asdict(item) for item in audit["orders"]]).to_csv(
            target / "orders.csv", index=False)
        with (target / "risk_decisions.jsonl").open("w") as output:
            for decision in audit["decisions"]:
                output.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
        print(experiment, json.dumps(result, ensure_ascii=False), flush=True)
    pd.DataFrame(results).to_csv(args.output_dir / "results.csv", index=False)
    manifest = {
        "engine": "vcp_context_v1", "experiments": args.experiments,
        "start_date": args.start_date, "end_date": args.end_date,
        "registry_sha256": registry["registry_sha256"],
        "context_config_sha256": context_config.sha256,
        "risk_config_sha256": risk_config.sha256,
        "slippage_bps": 25.0, "outcome_analysis_after_preregistration": True,
        "research_only": True,
    }
    (args.output_dir / "experiment_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
