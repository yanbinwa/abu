#!/usr/bin/env python3
"""Replay market-gated TurtleATR only from Alpha158 residual resources."""
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
from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine, load_risk_config
from abupy.AlphaBu.ABuPositionAddPolicy import (
    MarketTrendGatePolicy, TurtleAtrPolicy, load_market_trend_gate_config,
    load_turtle_atr_config,
)
from abupy.AlphaBu.ABuPositionAddResearch import replay_residual_add_overlay
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from scripts.backtest_alpha158_lite_low_turnover_v3 import (
    load_alpha158_lite_low_turnover_config, rank_frame, run_low_turnover,
)
from scripts.backtest_position_add_v1 import _write_audit


def _metrics(curve, initial_cash):
    capital = np.r_[float(initial_cash), curve.capital.to_numpy(dtype=float)]
    return {
        "return_pct": float((capital[-1]/initial_cash-1)*100),
        "max_drawdown_pct": float(
            (capital/np.maximum.accumulate(capital)-1).min()*100),
        "average_exposure_pct": float(curve.exposure.mean()*100),
        "ending_capital": float(capital[-1]),
    }


def run(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    expected = {
        "experiment_id", "total_initial_cash", "policy_config",
        "market_gate_config", "parameter_search",
        "historical_result_is_diagnostic", "post_hoc_risk_variant",
        "base_order_priority",
    }
    if set(config) != expected or config["parameter_search"] is not False:
        raise ValueError("residual ADD config is not the frozen v1 schema")
    if config["base_order_priority"] is not True:
        raise ValueError("base order priority must remain enabled")

    source = load_alpha158_lite_config(args.source_config)
    low = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk_config = load_risk_config(args.risk_config)
    turtle = TurtleAtrPolicy(load_turtle_atr_config(
        args.config.parent/config["policy_config"]))
    policy = MarketTrendGatePolicy(
        turtle, load_market_trend_gate_config(
            args.config.parent/config["market_gate_config"]))
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", low.score_column],
        dtype={"symbol": str})
    scores = rank_frame(
        predictions, low.score_column,
        max(low.entry_rank_limit, low.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    initial = float(config["total_initial_cash"])

    # Shadow policy produces proposals but cannot alter this base path.
    base_result, audit = run_low_turnover(
        panel, scores, source, low, risk_config, args.end_date,
        sync_dynamic_stops=True, position_add_policy=policy,
        position_add_execution_mode="shadow", initial_cash=initial)
    if any(getattr(fill, "position_effect", "") == "INCREASE"
           for fill in audit["fills"].itertuples(index=False)):
        raise AssertionError("frozen base path contains an ADD fill")

    execution = ExecutionConfig(
        initial_cash=initial, slippage_bps=source.label_slippage_bps,
        mode="pit_corrected")
    replay = replay_residual_add_overlay(
        panel, audit["add_proposals"], audit["lot_dispositions"],
        audit["curve"], audit["orders"], audit["risk_positions_daily"],
        execution, PortfolioRiskEngine(panel, risk_config),
        start_date=int(audit["curve"].date.iloc[0]),
        end_date=int(audit["curve"].date.iloc[-1]))
    candidate = _metrics(replay["curve"], initial)
    return_delta = candidate["return_pct"]-base_result["return_pct"]
    drawdown_delta = candidate["max_drawdown_pct"]-base_result["max_drawdown_pct"]
    reasons = (replay["rejections"].reason.value_counts().to_dict()
               if not replay["rejections"].empty else {})
    forced = sum(item.exit_reason == "BASE_PRIORITY_CASH_RELEASE"
                 for item in replay["dispositions"])
    closed = len(replay["dispositions"])
    historical_gate = {
        "return_improved": return_delta > 0,
        "drawdown_degradation_within_1pp": drawdown_delta >= -1.0,
        "minimum_30_closed_adds": closed >= 30,
        "base_path_unchanged": True,
        "placebo_gate_passed": False,
    }
    summary = {
        "experiment_id": config["experiment_id"],
        "research_only": True, "parameters_frozen": True,
        "post_hoc_risk_variant": True,
        "base_order_priority": True,
        "base": base_result, "candidate": candidate,
        "incremental_return_pct_points": return_delta,
        "max_drawdown_change_pct_points": drawdown_delta,
        "add": {
            "triggered_proposals": len(audit["add_proposals"]),
            "approved_orders": len(replay["approvals"]),
            "filled_adds": len(replay["entries"]),
            "closed_adds": closed,
            "open_adds_end": len(replay["open_lots"]),
            "forced_cash_release_exits": forced,
            "rejections_by_reason": reasons,
            "realized_pnl_cash": float(sum(
                item.realized_pnl_cash for item in replay["dispositions"])),
        },
        "historical_gate": historical_gate,
        "historical_gate_passed": all(historical_gate.values()),
        "decision": "RETAIN_RESEARCH_ONLY_PENDING_PLACEBO_AND_FORWARD_DATA",
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_audit(args.output_dir/"base", base_result, audit["curve"],
                 audit["fills"], audit)
    replay["curve"].to_csv(args.output_dir/"combined_daily_nav.csv", index=False)
    replay["approvals"].to_csv(args.output_dir/"add_approvals.csv", index=False)
    replay["rejections"].to_csv(args.output_dir/"add_rejections.csv", index=False)
    pd.DataFrame([asdict(item) for item in replay["entries"]]).to_csv(
        args.output_dir/"add_entries.csv", index=False)
    pd.DataFrame([asdict(item) for item in replay["dispositions"]]).to_csv(
        args.output_dir/"add_dispositions.csv", index=False)
    pd.DataFrame([asdict(item) for item in replay["open_lots"]]).to_csv(
        args.output_dir/"add_open_lots.csv", index=False)
    (args.output_dir/"summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=
                        ROOT/"configs/selection/residual_turtle_atr_market_gate_v1.json")
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
        "/Users/wjy/abu/backtests/position_add_residual_v1_20261004"))
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
