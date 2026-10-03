#!/usr/bin/env python3
"""Test volatility-standardized residual momentum without threshold search."""
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

from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuScoreToIntent import rerank_intents  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from scripts.backtest_vcp_context_v1 import (  # noqa: E402
    load_frozen_intents, run_experiment,
)
from scripts.backtest_vcp_quality_rank_v1 import _save_audit  # noqa: E402


FEATURES = (
    "residual_momentum_standardized", "ma120_slope",
    "contraction_tightness", "breakout_strength",
)


def _zero_one_rank(frame, value):
    output = pd.Series(np.nan, index=frame.index, dtype=float)
    valid = frame[value].notna()
    ordered = frame.loc[valid].sort_values(
        [value, "symbol"], ascending=[True, True], kind="mergesort")
    if len(ordered) == 1:
        output.loc[ordered.index] = 1.0
    elif len(ordered):
        output.loc[ordered.index] = np.arange(len(ordered))/(len(ordered)-1)
    return output


def standardized_residual_scores(frame, weights):
    rows = []
    for _, group in frame.groupby("signal_asof", sort=True):
        work = group.copy()
        score = pd.Series(0.0, index=work.index)
        valid = pd.Series(True, index=work.index)
        for feature in FEATURES:
            ranked = _zero_one_rank(work, feature)
            score += float(weights[feature+"_weight"])*ranked.fillna(0)
            valid &= ranked.notna()
        work["standardized_residual_score"] = score.where(valid)
        rows.append(work)
    return pd.concat(rows, ignore_index=True) if rows else frame.copy()


def _register(path, trial_id, hypothesis, configuration, status="REGISTERED",
              observed_metrics=None):
    existing = {item["trial_id"]: item for item in read_trial_registry(path)}
    if trial_id in existing:
        return existing[trial_id]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-intents", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--features", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_quality_rank_v1/feature_snapshots.csv.gz"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/vcp_residual_standardized_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_residual_standardized_v1"))
    parser.add_argument("--start-date", type=int, default=20220104)
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    if set(config) != {"strategy_version", *[name+"_weight" for name in FEATURES]}:
        raise ValueError("standardized residual config fields mismatch")
    if not np.isclose(sum(config[name+"_weight"] for name in FEATURES), 1.0):
        raise ValueError("standardized residual weights must sum to one")
    shadow = pd.read_csv(args.shadow_intents, dtype={"symbol": str})
    features = pd.read_csv(args.features, dtype={"symbol": str})
    score_input = shadow[[
        "intent_id", "signal_asof", "symbol", "ma120_slope",
        "contraction_tightness", "breakout_strength",
    ]].merge(
        features[["intent_id", "residual_momentum_standardized"]],
        on="intent_id", how="inner",
        validate="one_to_one")
    scored = standardized_residual_scores(score_input, config)
    score_map = scored.set_index("intent_id").standardized_residual_score.to_dict()
    source = shadow[shadow.intent_id.isin(set(scored.intent_id))].copy()
    source_intents = load_frozen_intents(source)
    ranked_intents = rerank_intents(
        source_intents, score_map, config["strategy_version"])
    ranked = pd.DataFrame([asdict(item) for item in ranked_intents])
    if ranked.empty:
        raise RuntimeError("standardized residual ranking produced no intents")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    registration = _register(
        args.output_dir / "trial_registry.jsonl",
        "vcp-residual-standardized-v1-frozen-20261003",
        "Risk-standardized residual momentum improves candidate ordering over "
        "the frozen unstandardized residual sum.",
        {"config": config, "source_strategy": "vcp_residual_v2",
         "parameter_search": False, "risk_config_sha256": risk.sha256})
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    rows = []
    for name, frame in (("legacy_full_period", source),
                        (config["strategy_version"], ranked)):
        result, audit = run_experiment(
            panel, frame, name, risk, args.start_date, args.end_date,
            ranking_column="score")
        rows.append(result)
        _save_audit(args.output_dir / name, audit)
        print(name, json.dumps(result, ensure_ascii=False), flush=True)
    results = pd.DataFrame(rows)
    results.to_csv(args.output_dir / "results.csv", index=False)
    scored.to_csv(args.output_dir / "candidate_scores.csv.gz", index=False,
                  compression="gzip")
    indexed = results.set_index("experiment")
    baseline = indexed.loc["legacy_full_period"]
    candidate = indexed.loc[config["strategy_version"]]
    comparison = {
        "strategy_version": config["strategy_version"],
        "registration_sha256": registration["record_sha256"],
        "candidate_intents": len(ranked),
        "return_delta_pct_points": float(
            candidate.return_pct-baseline.return_pct),
        "max_drawdown_delta_pct_points": float(
            candidate.max_drawdown_pct-baseline.max_drawdown_pct),
        "mean_r_delta": float(candidate.mean_r-baseline.mean_r),
        "parameter_search_performed": False, "research_only": True,
    }
    (args.output_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    _register(args.output_dir / "trial_registry.jsonl",
              "vcp-residual-standardized-v1-result",
              "Frozen result for standardized residual ranking",
              {"config": config}, status="OBSERVED",
              observed_metrics=comparison)
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
