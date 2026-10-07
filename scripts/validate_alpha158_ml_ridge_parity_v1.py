#!/usr/bin/env python3
"""Rebuild frozen Ridge OOS scores through the ML plugin and verify parity."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    Alpha158LiteFeatureEngine, load_alpha158_lite_config,
)
from abupy.AlphaBu.ABuArtifactManifest import sha256_file  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)
from abupy.MLBu.models.ABuRidgePlugin import RidgePlugin  # noqa: E402
from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    walk_forward_predictions,
)


OUTPUT_COLUMNS = [
    "signal_asof", "symbol", "column", "alpha_score", "baseline_score",
    "excess_return_20d", "target_rank", "fold", "train_end", "test_start",
    "test_end",
]


def stable_ranks(frame):
    ranked = frame.sort_values(
        ["signal_asof", "alpha_score", "symbol"],
        ascending=[True, False, True], kind="mergesort").copy()
    ranked["daily_rank"] = ranked.groupby(
        "signal_asof", sort=False).cumcount() + 1
    return ranked[["signal_asof", "symbol", "daily_rank"]].sort_values(
        ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)


def compare_predictions(actual, expected):
    keys = ["signal_asof", "symbol"]
    actual = actual.sort_values(keys, kind="mergesort").reset_index(drop=True)
    expected = expected.sort_values(keys, kind="mergesort").reset_index(drop=True)
    if not actual[keys].equals(expected[keys]):
        raise AssertionError("OOS prediction keys differ")
    exact_columns = ["column", "fold", "train_end"]
    exact_columns.extend(column for column in ("test_start", "test_end")
                         if column in expected.columns)
    for column in exact_columns:
        if not actual[column].equals(expected[column]):
            raise AssertionError("prediction column differs: {}".format(column))
    rank_equal = stable_ranks(actual).equals(stable_ranks(expected))
    if not rank_equal:
        raise AssertionError("daily Ridge ranks differ")
    score_delta = np.abs(
        actual.alpha_score.to_numpy() - expected.alpha_score.to_numpy())
    max_score_delta = float(np.nanmax(score_delta)) if len(score_delta) else 0.0
    if max_score_delta > 1e-12:
        raise AssertionError("Ridge score parity exceeded tolerance")
    return {
        "rows": int(len(actual)),
        "dates": int(actual.signal_asof.nunique()),
        "folds": int(actual.fold.nunique()),
        "max_abs_score_error": max_score_delta,
        "keys_exact": True,
        "ranks_exact": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--reference", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1/oos_predictions.csv.gz"))
    parser.add_argument("--reference-score-column", default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reuse-generated", action="store_true",
                        help="audit an already generated immutable OOS file")
    parser.add_argument("--start-date", type=int, default=20220101)
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    generated_path = args.output_dir/"oos_predictions.csv.gz"
    if (not args.reuse_generated and args.output_dir.exists() and
            any(args.output_dir.iterdir())):
        raise FileExistsError("output directory is not empty")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = load_alpha158_lite_config(args.config)
    if args.reuse_generated:
        if not generated_path.is_file():
            raise FileNotFoundError(str(generated_path))
    else:
        split_config = WalkForwardConfig(
            minimum_train_dates=config.minimum_train_dates,
            validation_dates=config.validation_dates,
            test_dates=config.test_dates,
            label_horizon_sessions=config.label_horizon_sessions,
            embargo_sessions=0)
        panel = SelectionPanelV2.from_research_data(
            args.signal_dir, args.research_dir, start_date=20200101,
            end_date=args.end_date)
        engine = Alpha158LiteFeatureEngine(panel, config)
        signal_days = np.flatnonzero(
            (panel.dates >= args.start_date) & (panel.dates <= args.end_date) &
            (np.arange(len(panel.dates)) >= config.minimum_history_sessions))
        predictions = walk_forward_predictions(
            engine, signal_days, split_config, args.output_dir,
            model_factory=RidgePlugin)
        predictions[OUTPUT_COLUMNS].to_csv(
            generated_path, index=False,
            compression={"method": "gzip", "compresslevel": 3})
    # Compare the immutable representation consumed by downstream portfolio
    # code, not a higher-precision in-memory intermediate absent from either
    # frozen artifact.
    actual = pd.read_csv(generated_path, dtype={"symbol": str})
    expected = pd.read_csv(args.reference, dtype={"symbol": str})
    reference_score = args.reference_score_column
    if reference_score is None:
        reference_score = ("alpha_score" if "alpha_score" in expected.columns
                           else "ridge_score")
    if reference_score not in expected.columns:
        raise ValueError("reference score column is missing")
    if reference_score != "alpha_score":
        expected = expected.rename(columns={reference_score: "alpha_score"})
    result = compare_predictions(actual, expected)
    result.update({
        "status": "PASS",
        "reference_path": str(args.reference.resolve()),
        "reference_sha256": sha256_file(args.reference),
        "actual_sha256": sha256_file(
            generated_path),
        "config_sha256": config.sha256,
    })
    (args.output_dir/"parity_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
