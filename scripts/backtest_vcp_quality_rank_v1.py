#!/usr/bin/env python3
"""Replay purged OOS quality scores through the common risk/execution engine."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuScoreToIntent import rerank_intents  # noqa: E402
from abupy.AlphaBu.ABuSelectionModel import (  # noqa: E402
    load_vcp_quality_model_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_vcp_context_v1 import (  # noqa: E402
    load_frozen_intents, run_experiment,
)


def quality_intent_frame(shadow, predictions, strategy_id):
    predictions = predictions.drop_duplicates("intent_id", keep="last")
    source = shadow[shadow.intent_id.isin(set(predictions.intent_id))].copy()
    intents = load_frozen_intents(source)
    scores = predictions.set_index("intent_id").quality_score.to_dict()
    ranked = rerank_intents(intents, scores, strategy_id)
    return pd.DataFrame([asdict(item) for item in ranked])


def _save_audit(directory, audit):
    directory.mkdir(parents=True, exist_ok=True)
    audit["curve"].to_csv(directory / "daily_nav.csv", index=False)
    audit["fills"].to_csv(directory / "fills.csv", index=False)
    pd.DataFrame(audit["exits"]).to_csv(
        directory / "exit_reasons.csv", index=False)
    pd.DataFrame(audit["overlay"]).to_csv(
        directory / "selection_decisions.csv", index=False)
    pd.DataFrame([asdict(item) for item in audit["orders"]]).to_csv(
        directory / "orders.csv", index=False)
    pd.DataFrame([asdict(item) for item in audit["reservations"]]).to_csv(
        directory / "reservations.csv", index=False)
    with (directory / "risk_decisions.jsonl").open("w", encoding="utf-8") as output:
        for decision in audit["decisions"]:
            output.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-intents", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_quality_rank_v1/oos_predictions.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_quality_rank_v1/portfolio"))
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/vcp_quality_rank_v1.json")
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    shadow = pd.read_csv(args.shadow_intents, dtype={"symbol": str})
    predictions = pd.read_csv(args.predictions, dtype={"symbol": str})
    config = load_vcp_quality_model_config(args.config)
    ids = set(predictions.intent_id)
    baseline = shadow[shadow.intent_id.isin(ids)].copy()
    if len(baseline) != len(predictions):
        raise ValueError("prediction/source intent coverage mismatch")
    quality = quality_intent_frame(
        baseline, predictions, config.strategy_version)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    start_date = int(predictions.signal_asof.min())
    experiments = [
        ("legacy_score_same_oos", baseline),
        (config.strategy_version, quality),
    ]
    rows = []
    for name, frame in experiments:
        result, audit = run_experiment(
            panel, frame, name, risk, start_date, args.end_date,
            ranking_column="score")
        rows.append(result)
        _save_audit(args.output_dir / name, audit)
        print(name, json.dumps(result, ensure_ascii=False), flush=True)
    results = pd.DataFrame(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(args.output_dir / "results.csv", index=False)
    indexed = results.set_index("experiment")
    baseline_result = indexed.loc["legacy_score_same_oos"]
    quality_result = indexed.loc[config.strategy_version]
    comparison = {
        "strategy_version": config.strategy_version,
        "model_config_sha256": config.sha256,
        "evaluation_scope": "purged_walk_forward_prediction_dates_only",
        "start_date": start_date, "end_date": args.end_date,
        "return_delta_pct_points": float(
            quality_result.return_pct-baseline_result.return_pct),
        "max_drawdown_delta_pct_points": float(
            quality_result.max_drawdown_pct-baseline_result.max_drawdown_pct),
        "mean_r_delta": float(quality_result.mean_r-baseline_result.mean_r),
        "filled_buy_delta": int(
            quality_result.filled_buys-baseline_result.filled_buys),
        "research_only": True,
    }
    (args.output_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

