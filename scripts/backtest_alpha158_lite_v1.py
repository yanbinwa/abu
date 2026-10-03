#!/usr/bin/env python3
"""Replay Alpha158-lite OOS scores through top-k buffer and common ledger."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    Alpha158LiteExitEngine, Alpha158LiteFeatureEngine,
    load_alpha158_lite_config,
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
from scripts.backtest_vcp_context_v1 import _trade_metrics  # noqa: E402


def rank_frame(predictions, score_column, depth):
    frame = predictions.dropna(subset=[score_column]).sort_values(
        ["signal_asof", score_column, "symbol"],
        ascending=[True, False, True], kind="mergesort")
    frame = frame.groupby("signal_asof", sort=False).head(int(depth)).copy()
    frame["daily_rank"] = frame.groupby(
        "signal_asof", sort=False).cumcount()+1
    return frame[["signal_asof", "symbol", "column", score_column,
                  "daily_rank"]]


def exit_reason(event_reason, symbol, retained_symbols):
    if event_reason:
        return event_reason
    if symbol not in retained_symbols:
        return "RANK_EXIT"
    return None


def dropout_rank_exits(held_symbols, daily_scores, maximum, blocked=()):
    """Return at most ``maximum`` worst holdings with a superior replacement."""
    held = set(held_symbols)
    blocked = set(blocked)
    rank_by_symbol = dict(zip(
        daily_scores.symbol.astype(str),
        daily_scores.daily_rank.astype(int))) if len(daily_scores) else {}
    candidates = [
        (int(row.daily_rank), str(row.symbol))
        for row in daily_scores.itertuples()
        if str(row.symbol) not in held
    ]
    candidates.sort(key=lambda item: (item[0], item[1]))
    holdings = [
        (rank_by_symbol.get(symbol, np.inf), symbol)
        for symbol in held if symbol not in blocked
    ]
    holdings.sort(key=lambda item: (-item[0], item[1]))
    result = []
    for holding, candidate in zip(holdings, candidates):
        if len(result) >= int(maximum):
            break
        if candidate[0] < holding[0]:
            result.append(holding[1])
    return result


def fixed_path_cost_attribution(fills, net_return_pct, initial_cash):
    """Remove observed fill frictions without changing quantities or path."""
    filled = fills[fills.status.eq("filled")].copy()
    commission = float(filled.commission.sum())
    transfer = float(filled.transfer_fee.sum())
    stamp = float(filled.stamp_tax.sum())
    slippage = float(filled.slippage_cost.sum())
    friction = commission+transfer+stamp+slippage
    reference_notional = float(
        (filled.quantity*filled.reference_price).sum())
    return {
        "commission_pct_initial": commission/initial_cash*100,
        "transfer_fee_pct_initial": transfer/initial_cash*100,
        "stamp_tax_pct_initial": stamp/initial_cash*100,
        "slippage_pct_initial": slippage/initial_cash*100,
        "total_friction_pct_initial": friction/initial_cash*100,
        "fixed_path_reference_return_pct": (
            float(net_return_pct)+friction/initial_cash*100),
        "round_trip_turnover_multiple": (
            reference_notional/(2*initial_cash)),
    }


def _seed_marks(executor, panel, first):
    previous = np.asarray(panel.exec_close[:first], dtype=float)
    for column in range(previous.shape[1]):
        valid = previous[:, column][np.isfinite(previous[:, column]) &
                                    (previous[:, column] > 0)]
        if len(valid):
            executor.last_close[column] = valid[-1]


def run_rank_portfolio(panel, scores, score_column, config, risk_config,
                       end_date, rank_exit_mode="buffer",
                       max_rank_replacements_per_day=None,
                       entry_candidate_depth=None, experiment_name=None):
    grouped = {int(date): group.sort_values(
        ["daily_rank", "symbol"], kind="mergesort")
        for date, group in scores.groupby("signal_asof", sort=True)}
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    first_signal = int(scores.signal_asof.min())
    last_signal = min(int(scores.signal_asof.max()), int(end_date))
    first = date_index[first_signal]+1
    last = date_index[last_signal]
    executor = PortfolioExecutor(panel, ExecutionConfig(
        slippage_bps=config.label_slippage_bps, mode="pit_corrected",
        max_positions=config.entry_top_k))
    _seed_marks(executor, panel, first)
    risk = PortfolioRiskEngine(panel, risk_config)
    feature_engine = Alpha158LiteFeatureEngine(panel, config)
    exits = Alpha158LiteExitEngine(panel, config)
    entry_intents = {}
    decisions, exit_rows, selection_rows = [], [], []
    pending_exits = []

    for day in range(first, last+1):
        signal_day = day-1
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
                    "alpha158-lite-exit", signal_date, symbol, reason),
                strategy_id=entry.strategy_id, strategy_version="1",
                signal_asof=signal_date, symbol=symbol, side="sell",
                metadata={"exit_reason": reason},
            )
            executor.approve_order(
                sell, position.quantity, int(panel.dates[day]))
            exit_rows.append({"date": signal_date, "symbol": symbol,
                              "reason": reason})

        daily = grouped.get(signal_date, pd.DataFrame())
        entry_depth = (config.entry_top_k if entry_candidate_depth is None
                       else int(entry_candidate_depth))
        entry_candidates = daily[
            daily.daily_rank <= entry_depth] if len(daily) else daily
        held_or_ordered = set(executor.positions) | {
            order.symbol for order in executor.orders if order.side == "buy"}
        if len(entry_candidates):
            entry_candidates = entry_candidates[
                ~entry_candidates.symbol.isin(held_or_ordered)]
        exiting = sum(symbol in executor.positions
                      for symbol, _ in pending_exits)
        slots = max(0, config.entry_top_k-len(executor.positions)+exiting)
        for row in entry_candidates.head(slots).to_dict("records"):
            column = int(row["column"])
            intent = feature_engine.make_intent(
                signal_day, column, float(row[score_column]))
            if intent is None:
                continue
            order, _, decision = risk.approve(
                executor, intent, signal_day, day)
            decisions.append(decision)
            selection_rows.append({
                "signal_asof": signal_date, "symbol": intent.symbol,
                "score": float(row[score_column]),
                "daily_rank": int(row["daily_rank"]),
                "risk_decision": decision.decision,
                "order_created": order is not None,
            })
            entry_intents[intent.symbol] = intent

        fills_today = executor.process_open(day)
        for fill in fills_today:
            if fill.status != "filled":
                continue
            if fill.side == "sell":
                exits.remove(fill.symbol)
                entry_intents.pop(fill.symbol, None)
            else:
                entry = entry_intents[fill.symbol]
                exits.register_entry(entry, fill, day)
        executor.process_close(day)

        pending_exits = []
        if day < last:
            today = grouped.get(int(panel.dates[day]), pd.DataFrame())
            event_symbols = set()
            for symbol in sorted(executor.positions):
                if any(order.side == "sell" and order.symbol == symbol
                       for order in executor.orders):
                    continue
                reason = exits.signal(day, symbol)
                if reason:
                    event_symbols.add(symbol)
                    pending_exits.append((symbol, reason))
            if rank_exit_mode == "buffer":
                retained = set(today[
                    today.daily_rank <= config.retention_top_k
                ].symbol) if len(today) else set()
                for symbol in sorted(set(executor.positions)-event_symbols):
                    reason = exit_reason(None, symbol, retained)
                    if reason:
                        pending_exits.append((symbol, reason))
            elif rank_exit_mode == "dropout":
                if max_rank_replacements_per_day is None:
                    raise ValueError("dropout mode requires replacement limit")
                for symbol in dropout_rank_exits(
                        executor.positions, today,
                        max_rank_replacements_per_day,
                        blocked=event_symbols):
                    pending_exits.append((symbol, "RANK_DROPOUT"))
            else:
                raise ValueError("unknown rank_exit_mode")

    curve = executor.curve_frame()
    fills = executor.fills_frame()
    initial = executor.config.initial_cash
    capital = np.r_[initial, curve.capital.to_numpy()]
    liquidation_capital = np.r_[
        initial, curve.liquidation_nav_3_limits.to_numpy()]
    daily_return = capital[1:]/capital[:-1]-1
    cutoff = np.quantile(daily_return, .05) if len(daily_return) else np.nan
    trades = _trade_metrics(fills)
    benchmark_start = float(panel.benchmark_close[first-1])
    benchmark_end = float(panel.benchmark_close[last])
    result = {
        "experiment": experiment_name or score_column,
        "start": int(panel.dates[first]), "end": int(panel.dates[last]),
        "return_pct": float((capital[-1]/initial-1)*100),
        "benchmark_return_pct": float(
            (benchmark_end/benchmark_start-1)*100),
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
                            (fills.status == "filled")).sum()),
        "filled_sells": int(((fills.side == "sell") &
                             (fills.status == "filled")).sum()),
        "risk_rejected": sum(item.decision == "rejected" for item in decisions),
        "risk_reduced": sum(item.decision == "reduced" for item in decisions),
        "entry_clusters": int(fills[(fills.side == "buy") &
                                     (fills.status == "filled")].date.nunique()),
        "open_positions_end": int(len(executor.positions)),
        "pending_orders_end": int(len(executor.orders)),
        "slippage_bps": float(config.label_slippage_bps),
        **trades,
    }
    result.update(fixed_path_cost_attribution(
        fills, result["return_pct"], initial))
    audit = {
        "curve": curve, "fills": fills, "decisions": decisions,
        "exits": exit_rows, "selection": selection_rows,
        "orders": executor.order_history,
        "reservations": executor.reservation_history,
    }
    return result, audit


def _save_audit(directory, audit):
    directory.mkdir(parents=True, exist_ok=True)
    audit["curve"].to_csv(directory/"daily_nav.csv", index=False)
    audit["fills"].to_csv(directory/"fills.csv", index=False)
    pd.DataFrame(audit["exits"]).to_csv(
        directory/"exit_reasons.csv", index=False)
    pd.DataFrame(audit["selection"]).to_csv(
        directory/"selection_decisions.csv", index=False)
    pd.DataFrame([asdict(item) for item in audit["orders"]]).to_csv(
        directory/"orders.csv", index=False)
    pd.DataFrame([asdict(item) for item in audit["reservations"]]).to_csv(
        directory/"reservations.csv", index=False)
    with (directory/"risk_decisions.jsonl").open("w", encoding="utf-8") as output:
        for item in audit["decisions"]:
            output.write(json.dumps(asdict(item), ensure_ascii=False)+"\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1/oos_predictions.csv.gz"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT/"configs/selection/risk_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1/portfolio"))
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    config = load_alpha158_lite_config(args.config)
    risk_config = load_risk_config(args.risk_config)
    predictions = pd.read_csv(
        args.predictions, usecols=["signal_asof", "symbol", "column",
                                   "alpha_score", "baseline_score"],
        dtype={"symbol": str})
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    rows = []
    for score_column in ("baseline_score", "alpha_score"):
        scores = rank_frame(
            predictions, score_column, config.portfolio_score_depth)
        result, audit = run_rank_portfolio(
            panel, scores, score_column, config, risk_config, args.end_date)
        rows.append(result)
        _save_audit(args.output_dir/score_column, audit)
        print(score_column, json.dumps(result, ensure_ascii=False), flush=True)
    results = pd.DataFrame(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(args.output_dir/"results.csv", index=False)
    indexed = results.set_index("experiment")
    baseline = indexed.loc["baseline_score"]
    alpha = indexed.loc["alpha_score"]
    failure_reasons = []
    if alpha.return_pct <= 0:
        failure_reasons.append("COST_AFTER_RETURN_NON_POSITIVE")
    if alpha.mean_r <= 0:
        failure_reasons.append("MEAN_R_NON_POSITIVE")
    if alpha.liquidation_3_limits_return_pct <= 0:
        failure_reasons.append("LIQUIDATION_RETURN_NON_POSITIVE")
    comparison = {
        "strategy_version": config.strategy_version,
        "config_sha256": config.sha256,
        "evaluation_scope": "purged_walk_forward_dates_common_execution",
        "return_delta_pct_points": float(
            alpha.return_pct-baseline.return_pct),
        "max_drawdown_delta_pct_points": float(
            alpha.max_drawdown_pct-baseline.max_drawdown_pct),
        "mean_r_delta": float(alpha.mean_r-baseline.mean_r),
        "filled_buy_delta": int(alpha.filled_buys-baseline.filled_buys),
        "portfolio_stage_decision": (
            "RETAIN_FOR_ROBUSTNESS_TESTS" if not failure_reasons
            else "REJECT_AT_PORTFOLIO_STAGE"),
        "failure_reasons": failure_reasons,
        "research_only": True,
    }
    (args.output_dir/"comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    registry = args.output_dir.parent/"trial_registry.jsonl"
    trial_id = "alpha158-lite-v1-portfolio-observed-20261003"
    observed = {
        "results": rows, "comparison": comparison,
        "execution": {
            "slippage_bps": config.label_slippage_bps,
            "mode": "pit_corrected",
            "max_positions": config.entry_top_k,
            "risk_config_sha256": risk_config.sha256,
        },
    }
    existing = [item for item in read_trial_registry(registry)
                if item["trial_id"] == trial_id]
    if not existing:
        register_trial(
            registry, trial_id,
            "Observed common-ledger portfolio result for the frozen "
            "Alpha158-lite v1 ranking experiment.",
            {"strategy": asdict(config),
             "risk_config_sha256": risk_config.sha256,
             "score_columns": ["baseline_score", "alpha_score"]},
            status="OBSERVED", observed_metrics=observed)
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
