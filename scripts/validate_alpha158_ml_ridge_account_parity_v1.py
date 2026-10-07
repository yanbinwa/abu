#!/usr/bin/env python3
"""Replay ML Ridge scores and compare account business artifacts."""
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
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_event_exit_only_2025_2026 import (  # noqa: E402
    save_audit,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402


def compare_frame(actual_path, reference_path, money_columns=(),
                  exact_columns=()):
    actual = pd.read_csv(actual_path)
    reference = pd.read_csv(reference_path)
    if list(actual.columns) != list(reference.columns):
        raise AssertionError("artifact columns differ: {}".format(
            actual_path.name))
    if len(actual) != len(reference):
        raise AssertionError("artifact row count differs: {}".format(
            actual_path.name))
    max_money_error = 0.0
    for column in money_columns:
        left = actual[column].to_numpy(dtype=float)
        right = reference[column].to_numpy(dtype=float)
        delta = np.abs(left-right)
        value = float(np.nanmax(delta)) if len(delta) else 0.0
        max_money_error = max(max_money_error, value)
        if value > 0.01:
            raise AssertionError("money tolerance exceeded: {}.{}".format(
                actual_path.name, column))
    for column in exact_columns:
        if not actual[column].fillna("<NA>").equals(
                reference[column].fillna("<NA>")):
            raise AssertionError("business field differs: {}.{}".format(
                actual_path.name, column))
    pd.testing.assert_frame_equal(
        actual, reference, check_dtype=False, check_exact=False,
        atol=1e-12, rtol=0)
    return {"rows": int(len(actual)),
            "max_money_error": max_money_error}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--reference-account", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(str(args.output_dir))
    args.output_dir.mkdir(parents=True)

    config_dir = ROOT/"configs/selection"
    source = load_alpha158_lite_config(config_dir/"alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        config_dir/"alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(config_dir/"risk_v1.json")
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", "alpha_score",
                 "train_end"], dtype={"symbol": str})
    if not (predictions.train_end < predictions.signal_asof).all():
        raise ValueError("predictions are not OOS")
    scores = rank_frame(
        predictions, "alpha_score",
        max(policy.entry_rank_limit, policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    result, audit = run_low_turnover(
        panel, scores, source, policy, risk, args.end_date,
        review_overlay=CostAwareReview(suppress_rank_exits=True))
    account_dir = args.output_dir/"account"
    save_audit(account_dir, audit)

    checks = {
        "daily_nav": compare_frame(
            account_dir/"daily_nav.csv",
            args.reference_account/"daily_nav.csv",
            money_columns=("cash", "stocks", "capital", "reserved_cash",
                           "liquidation_nav_1_limit",
                           "liquidation_nav_3_limits",
                           "liquidation_nav_5_limits",
                           "liquidation_nav_zero_stale"),
            exact_columns=("date", "holdings")),
        "orders": compare_frame(
            account_dir/"orders.csv", args.reference_account/"orders.csv",
            exact_columns=("order_id", "intent_id", "symbol", "side",
                           "quantity", "created_asof", "valid_session")),
        "fills": compare_frame(
            account_dir/"fills.csv", args.reference_account/"fills.csv",
            money_columns=("commission", "transfer_fee", "stamp_tax",
                           "slippage_cost", "actual_initial_r_cash"),
            exact_columns=("order_id", "intent_id", "date", "symbol",
                           "side", "status", "quantity", "fill_id")),
        "selection": compare_frame(
            account_dir/"selection_decisions.csv",
            args.reference_account/"selection_decisions.csv",
            exact_columns=("signal_asof", "symbol", "daily_rank",
                           "risk_decision", "order_created")),
    }
    report = {"status": "PASS", "checks": checks, "metrics": result}
    (args.output_dir/"account_parity_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
