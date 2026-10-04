#!/usr/bin/env python3
"""Cash-isolated Alpha158 + TurtleATR diagnostic experiment.

The total capital is partitioned before the run.  The Alpha158 base strategy
cannot consume the ADD sleeve and the sleeve cannot consume base cash.  ADD
signals and exits are generated from the frozen base path, so the comparison
measures the incremental use of otherwise idle sleeve cash without changing
which base trades are executed.
"""
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

from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
from abupy.AlphaBu.ABuPositionAddPolicy import (
    MarketTrendGatePolicy, TurtleAtrPolicy, load_market_trend_gate_config,
    load_turtle_atr_config,
)
from abupy.AlphaBu.ABuPositionAddResearch import replay_isolated_add_sleeve
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from scripts.backtest_alpha158_lite_low_turnover_v3 import (
    load_alpha158_lite_low_turnover_config, rank_frame, run_low_turnover,
)
from scripts.backtest_position_add_v1 import _write_audit


def _metrics(curve, initial_cash):
    capital = np.r_[float(initial_cash), curve.capital.to_numpy(dtype=float)]
    peak = np.maximum.accumulate(capital)
    return {
        "return_pct": float((capital[-1]/initial_cash-1)*100),
        "max_drawdown_pct": float((capital/peak-1).min()*100),
        "average_exposure_pct": float(curve.exposure.mean()*100),
        "ending_capital": float(capital[-1]),
    }


def _combine(base_curve, sleeve_curve, sleeve_initial_cash):
    left = base_curve[["date", "cash", "stocks", "capital"]].copy()
    right = sleeve_curve[["date", "cash", "stocks", "capital"]].copy()
    merged = left.merge(right, on="date", suffixes=("_base", "_sleeve"),
                        how="inner", validate="one_to_one")
    if len(merged) != len(base_curve) or len(merged) != len(sleeve_curve):
        raise ValueError("base and sleeve curves do not cover identical dates")
    merged["cash"] = merged.cash_base+merged.cash_sleeve
    merged["stocks"] = merged.stocks_base+merged.stocks_sleeve
    merged["capital"] = merged.capital_base+merged.capital_sleeve
    merged["exposure"] = merged.stocks/merged.capital
    merged["idle_control_capital"] = merged.capital_base+sleeve_initial_cash
    merged["idle_control_cash"] = merged.cash_base+sleeve_initial_cash
    merged["idle_control_stocks"] = merged.stocks_base
    merged["idle_control_exposure"] = (
        merged.idle_control_stocks/merged.idle_control_capital)
    return merged


def run(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected_v1 = {
        "experiment_id", "total_initial_cash", "base_initial_cash_fraction",
        "sleeve_max_gross_exposure", "policy_config", "parameter_search",
        "historical_result_is_diagnostic",
    }
    expected_gated = expected_v1 | {"market_gate_config",
                                    "post_hoc_risk_variant"}
    if set(config) not in (expected_v1, expected_gated):
        raise ValueError("isolated sleeve config fields mismatch")
    if config["parameter_search"] is not False:
        raise ValueError("v1 isolated sleeve forbids parameter search")
    total_initial = float(config["total_initial_cash"])
    base_initial = total_initial*float(config["base_initial_cash_fraction"])
    sleeve_initial = total_initial-base_initial

    source = load_alpha158_lite_config(args.source_config)
    low = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    turtle_config = load_turtle_atr_config(
        args.config.parent/config["policy_config"])
    policy = TurtleAtrPolicy(turtle_config)
    if "market_gate_config" in config:
        policy = MarketTrendGatePolicy(
            policy, load_market_trend_gate_config(
                args.config.parent/config["market_gate_config"]))
    scores_raw = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", low.score_column],
        dtype={"symbol": str})
    scores = rank_frame(
        scores_raw, low.score_column,
        max(low.entry_rank_limit, low.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)

    full_result, full_audit = run_low_turnover(
        panel, scores, source, low, risk, args.end_date,
        sync_dynamic_stops=True, initial_cash=total_initial)

    # Shadow mode records TurtleATR proposals but creates no ADD orders.  The
    # base executor therefore remains the exact partitioned NoAdd control.
    base_result, audit = run_low_turnover(
        panel, scores, source, low, risk, args.end_date,
        sync_dynamic_stops=True, position_add_policy=policy,
        position_add_execution_mode="shadow", initial_cash=base_initial)
    if any(getattr(fill, "position_effect", "") == "INCREASE"
           for fill in audit["fills"].itertuples(index=False)):
        raise AssertionError("shadow base path unexpectedly contains ADD fills")

    execution = ExecutionConfig(
        initial_cash=sleeve_initial,
        slippage_bps=source.label_slippage_bps,
        mode="pit_corrected")
    replay = replay_isolated_add_sleeve(
        panel, audit["add_proposals"], audit["lot_dispositions"], execution,
        sleeve_initial, start_date=int(audit["curve"].date.iloc[0]),
        end_date=int(audit["curve"].date.iloc[-1]),
        max_gross_exposure=float(config["sleeve_max_gross_exposure"]))
    combined = _combine(audit["curve"], replay["curve"], sleeve_initial)
    idle_curve = pd.DataFrame({
        "date": combined.date,
        "capital": combined.idle_control_capital,
        "exposure": combined.idle_control_exposure,
    })
    combined_curve = combined[["date", "capital", "exposure"]]
    sleeve_metrics = _metrics(replay["curve"], sleeve_initial)
    idle_metrics = _metrics(idle_curve, total_initial)
    candidate_metrics = _metrics(combined_curve, total_initial)
    return_delta = candidate_metrics["return_pct"]-idle_metrics["return_pct"]
    drawdown_delta = (candidate_metrics["max_drawdown_pct"]-
                      idle_metrics["max_drawdown_pct"])
    deployment_return_delta = (
        candidate_metrics["return_pct"]-full_result["return_pct"])
    deployment_drawdown_delta = (
        candidate_metrics["max_drawdown_pct"]-
        full_result["max_drawdown_pct"])
    closed = len(replay["dispositions"])
    historical_gate = {
        "incremental_return_positive": return_delta > 0,
        "drawdown_degradation_within_1pp": drawdown_delta >= -1.0,
        "minimum_30_closed_adds": closed >= 30,
        "placebo_gate_passed": False,
    }
    summary = {
        "experiment_id": config["experiment_id"],
        "research_only": True,
        "parameters_frozen": True,
        "post_hoc_risk_variant": bool(
            config.get("post_hoc_risk_variant", False)),
        "base_initial_cash": base_initial,
        "sleeve_initial_cash": sleeve_initial,
        "total_initial_cash": total_initial,
        "full_capital_noadd_reference": full_result,
        "base_result": base_result,
        "partitioned_noadd_idle_control": idle_metrics,
        "partitioned_turtle_candidate": candidate_metrics,
        "isolated_sleeve": {
            **sleeve_metrics,
            "triggered_proposals": len(audit["add_proposals"]),
            "filled_adds": len(replay["entries"]),
            "closed_adds": closed,
            "open_adds_end": len(replay["open_lots"]),
            "rejected_adds": len(replay["rejections"]),
            "realized_pnl_cash": float(sum(
                item.realized_pnl_cash for item in replay["dispositions"])),
        },
        "incremental_return_pct_points": return_delta,
        "max_drawdown_change_pct_points": drawdown_delta,
        "deployment_return_delta_pct_points": deployment_return_delta,
        "deployment_max_drawdown_change_pct_points":
            deployment_drawdown_delta,
        "historical_gate": historical_gate,
        "historical_gate_passed": all(historical_gate.values()),
        "decision": "RETAIN_RESEARCH_ONLY_PENDING_NEW_PLACEBO_AND_FORWARD_DATA",
        "known_statistical_limit": (
            "Existing exact-PIT matched placebo covers only 10 of 232 prior "
            "closed TurtleATR ADDs; this run does not claim significance."),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_audit(args.output_dir/"full_capital_noadd", full_result,
                 full_audit["curve"], full_audit["fills"], full_audit)
    _write_audit(args.output_dir/"base", base_result, audit["curve"],
                 audit["fills"], audit)
    replay["curve"].to_csv(args.output_dir/"sleeve_daily_nav.csv", index=False)
    combined.to_csv(args.output_dir/"combined_daily_nav.csv", index=False)
    pd.DataFrame([asdict(item) for item in replay["entries"]]).to_csv(
        args.output_dir/"sleeve_entries.csv", index=False)
    pd.DataFrame([asdict(item) for item in replay["dispositions"]]).to_csv(
        args.output_dir/"sleeve_dispositions.csv", index=False)
    replay["rejections"].to_csv(
        args.output_dir/"sleeve_rejections.csv", index=False)
    pd.DataFrame([asdict(item) for item in replay["open_lots"]]).to_csv(
        args.output_dir/"sleeve_open_lots.csv", index=False)
    (args.output_dir/"summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=
                        ROOT/"configs/selection/isolated_turtle_atr_sleeve_v1.json")
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
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/"
        "oos_predictions.csv.gz"))
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/position_add_isolated_sleeve_v1_20261004"))
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
