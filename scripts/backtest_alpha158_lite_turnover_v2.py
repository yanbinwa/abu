#!/usr/bin/env python3
"""Diagnose Alpha158-lite with a frozen one-position daily dropout budget."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from scripts.backtest_alpha158_lite_v1 import (  # noqa: E402
    _save_audit, rank_frame, run_rank_portfolio,
)


def _register_once(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None):
    matches = [item for item in read_trial_registry(path)
               if item["trial_id"] == trial_id]
    if matches:
        if (matches[0]["hypothesis"] != hypothesis or
                matches[0]["configuration"] != configuration):
            raise ValueError("registered turnover trial differs")
        return matches[0]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1/oos_predictions.csv.gz"))
    parser.add_argument("--source-results", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1/portfolio/results.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--source-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--turnover-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_turnover_v2.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT/"configs/selection/risk_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_turnover_v2"))
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    source = load_alpha158_lite_config(args.source_config)
    turnover = load_alpha158_lite_turnover_config(args.turnover_config)
    if source.sha256 != turnover.source_config_sha256:
        raise ValueError("turnover experiment source config hash mismatch")
    if source.strategy_version != turnover.source_strategy_version:
        raise ValueError("turnover experiment source strategy mismatch")
    risk = load_risk_config(args.risk_config)
    registration_config = {
        "turnover": asdict(turnover), "source_config": asdict(source),
        "risk_config_sha256": risk.sha256,
        "execution": {"slippage_bps": 25.0, "mode": "pit_corrected"},
        "parameter_search": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    registry = args.output_dir/"trial_registry.jsonl"
    trial_id = "alpha158-lite-turnover-v2-frozen-20261003"
    hypothesis = (
        "Limiting rank-driven replacement to one position per day reduces "
        "friction enough to improve the frozen Alpha158-lite OOS portfolio "
        "without delaying event risk exits.")
    registration = _register_once(
        registry, trial_id, hypothesis, registration_config)

    predictions = pd.read_csv(
        args.predictions, usecols=["signal_asof", "symbol", "column",
                                   turnover.score_column],
        dtype={"symbol": str})
    scores = rank_frame(
        predictions, turnover.score_column, turnover.entry_candidate_depth)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    strategy_config = replace(
        source, strategy_version=turnover.strategy_version,
        entry_top_k=turnover.target_positions,
        portfolio_score_depth=turnover.entry_candidate_depth)
    result, audit = run_rank_portfolio(
        panel, scores, turnover.score_column, strategy_config, risk,
        args.end_date, rank_exit_mode="dropout",
        max_rank_replacements_per_day=
        turnover.max_rank_replacements_per_day,
        entry_candidate_depth=turnover.entry_candidate_depth,
        experiment_name=turnover.strategy_version)
    _save_audit(args.output_dir/turnover.strategy_version, audit)
    pd.DataFrame([result]).to_csv(args.output_dir/"results.csv", index=False)
    source_results = pd.read_csv(args.source_results).set_index("experiment")
    v1 = source_results.loc[turnover.score_column]
    comparison = {
        "strategy_version": turnover.strategy_version,
        "source_strategy_version": source.strategy_version,
        "registration_sha256": registration["record_sha256"],
        "return_delta_vs_v1_pct_points": float(
            result["return_pct"]-v1.return_pct),
        "max_drawdown_delta_vs_v1_pct_points": float(
            result["max_drawdown_pct"]-v1.max_drawdown_pct),
        "friction_delta_vs_v1_pct_points": float(
            result["total_friction_pct_initial"]-
            v1.total_friction_pct_initial),
        "filled_buy_delta_vs_v1": int(
            result["filled_buys"]-v1.filled_buys),
        "post_hoc_diagnostic": True,
        "research_only": True,
    }
    (args.output_dir/"comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    _register_once(
        registry, trial_id+"-result",
        "Observed diagnostic result for "+trial_id,
        registration_config, status="OBSERVED",
        observed_metrics={"result": result, "comparison": comparison})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
