#!/usr/bin/env python3
"""Diagnose PIT signals that may support confidence-weighted position sizing.

This is an exploratory attribution tool.  It never changes orders or strategy
configuration.  Thresholds and directions are learned on 2023-2024 trades and
reported separately on the later 2025-2026 trades.
"""
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
    ALPHA158_LITE_FEATURES, Alpha158LiteFeatureEngine,
    load_alpha158_lite_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402


def _entry_outcomes(run_dir: Path, predictions: Path) -> pd.DataFrame:
    trades = pd.read_csv(run_dir / "logical_trades.csv")
    orders = pd.read_csv(run_dir / "logical_orders.csv")
    attribution = pd.read_csv(run_dir / "trade_attribution.csv")

    entry_orders = orders[
        orders.side.eq("buy") & orders.source_policy_id.isna()
    ][["intent_id", "created_asof", "symbol", "portfolio_equity_asof"]]
    entry_orders = entry_orders.drop_duplicates("intent_id")
    entries = trades[[
        "trade_id", "entry_intent_id", "opened_at", "closed_at",
        "initial_r_cash_frozen", "add_count",
    ]].merge(
        entry_orders, left_on="entry_intent_id", right_on="intent_id",
        how="left", validate="one_to_one",
    )

    all_lots = attribution.groupby("trade_id", as_index=False).agg(
        realized_pnl_cash=("realized_pnl_cash", "sum"),
        disposed_book_cost_cash=("disposed_book_cost_cash", "sum"),
    )
    base_lots = attribution[
        attribution.source_policy_id.eq("BASE_ENTRY")
    ].groupby("trade_id", as_index=False).agg(
        base_realized_pnl_cash=("realized_pnl_cash", "sum"),
        base_book_cost_cash=("disposed_book_cost_cash", "sum"),
    )
    entries = entries.merge(all_lots, on="trade_id", how="inner")
    entries = entries.merge(base_lots, on="trade_id", how="inner")
    entries["trade_return"] = (
        entries.realized_pnl_cash / entries.disposed_book_cost_cash
    )
    entries["base_return"] = (
        entries.base_realized_pnl_cash / entries.base_book_cost_cash
    )
    entries["year"] = entries.created_asof.astype(int) // 10000

    scores = pd.read_csv(predictions)
    scores["daily_rank"] = scores.groupby("signal_asof").alpha_score.rank(
        method="first", ascending=False)
    scores["score_z"] = scores.groupby("signal_asof").alpha_score.transform(
        lambda values: (values - values.mean()) / values.std())
    scores = scores.sort_values(["symbol", "signal_asof"], kind="mergesort")
    scores["prior_5_rank"] = scores.groupby("symbol").daily_rank.shift(5)
    scores["rank_improvement_5"] = scores.prior_5_rank - scores.daily_rank
    score_columns = [
        "signal_asof", "symbol", "alpha_score", "baseline_score",
        "excess_return_20d", "daily_rank", "score_z", "prior_5_rank",
        "rank_improvement_5",
    ]
    entries = entries.merge(
        scores[score_columns], left_on=["created_asof", "symbol"],
        right_on=["signal_asof", "symbol"], how="left",
        validate="many_to_one",
    )
    if entries.alpha_score.isna().any():
        raise ValueError("entry trades do not map completely to OOS scores")
    return entries


def _feature_rows(entries: pd.DataFrame, panel: SelectionPanelV2,
                  source_config: Path) -> pd.DataFrame:
    engine = Alpha158LiteFeatureEngine(
        panel, load_alpha158_lite_config(source_config))
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    pieces = []
    for signal_date, group in entries.groupby("created_asof", sort=True):
        day = date_index.get(int(signal_date))
        if day is None:
            continue
        snapshot = engine.snapshot(day, include_labels=False)
        wanted = set(group.symbol.astype(str))
        pieces.append(snapshot[snapshot.symbol.astype(str).isin(wanted)][
            ["signal_asof", "symbol", *ALPHA158_LITE_FEATURES]
        ])
    if not pieces:
        raise ValueError("no feature rows matched entry trades")
    result = pd.concat(pieces, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise ValueError("duplicate feature rows")
    return result


def _market_features(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=["date", "close"]).sort_values("date")
    returns = frame.close.pct_change()
    for window in (20, 60, 120):
        frame[f"market_return_{window}d"] = frame.close / frame.close.shift(window) - 1
        frame[f"market_above_ma{window}"] = (
            frame.close > frame.close.rolling(window, min_periods=window).mean()
        ).astype(float)
    frame["market_volatility_20d"] = returns.rolling(20).std() * np.sqrt(252)
    frame["market_volatility_60d"] = returns.rolling(60).std() * np.sqrt(252)
    return frame


def _group_metrics(frame: pd.DataFrame, mask: pd.Series) -> dict:
    group = frame[mask]
    return {
        "n": int(len(group)),
        "mean_base_return": float(group.base_return.mean()) if len(group) else np.nan,
        "median_base_return": float(group.base_return.median()) if len(group) else np.nan,
        "win_rate": float((group.base_return > 0).mean()) if len(group) else np.nan,
        "base_realized_pnl_cash": float(group.base_realized_pnl_cash.sum()),
    }


def _diagnose(frame: pd.DataFrame, signals: list[str]) -> pd.DataFrame:
    development = frame[frame.year <= 2024]
    validation = frame[frame.year >= 2025]
    rows = []
    for signal in signals:
        dev = development.dropna(subset=[signal, "base_return"])
        val = validation.dropna(subset=[signal, "base_return"])
        if len(dev) < 40 or len(val) < 40 or dev[signal].nunique() < 2:
            continue
        correlation = float(dev[[signal, "base_return"]].corr(
            method="spearman").iloc[0, 1])
        direction = 1 if correlation >= 0 else -1
        threshold = float(dev[signal].median())

        def selected(values):
            return values >= threshold if direction > 0 else values <= threshold

        dev_selected = selected(dev[signal])
        val_selected = selected(val[signal])
        dev_yes = _group_metrics(dev, dev_selected)
        dev_no = _group_metrics(dev, ~dev_selected)
        val_yes = _group_metrics(val, val_selected)
        val_no = _group_metrics(val, ~val_selected)
        annual_uplifts = {}
        for year, group in frame.dropna(subset=[signal]).groupby("year"):
            mask = selected(group[signal])
            annual_uplifts[str(int(year))] = float(
                group.loc[mask, "base_return"].mean() -
                group.loc[~mask, "base_return"].mean())
        rows.append({
            "signal": signal, "development_spearman": correlation,
            "direction": "high" if direction > 0 else "low",
            "threshold": threshold,
            "development_n_selected": dev_yes["n"],
            "development_return_uplift": (
                dev_yes["mean_base_return"] - dev_no["mean_base_return"]),
            "development_win_rate_uplift": (
                dev_yes["win_rate"] - dev_no["win_rate"]),
            "validation_n_selected": val_yes["n"],
            "validation_return_uplift": (
                val_yes["mean_base_return"] - val_no["mean_base_return"]),
            "validation_win_rate_uplift": (
                val_yes["win_rate"] - val_no["win_rate"]),
            "validation_selected_mean_return": val_yes["mean_base_return"],
            "validation_other_mean_return": val_no["mean_base_return"],
            "validation_selected_pnl_cash": val_yes["base_realized_pnl_cash"],
            "positive_years": int(sum(value > 0 for value in annual_uplifts.values())),
            "annual_uplifts": json.dumps(annual_uplifts, sort_keys=True),
        })
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["stable_candidate"] = (
        (result.development_return_uplift > 0) &
        (result.validation_return_uplift > 0) &
        (result.validation_n_selected >= 40) &
        (result.positive_years >= 3)
    )
    return result.sort_values(
        ["stable_candidate", "validation_return_uplift", "positive_years"],
        ascending=[False, False, False], kind="mergesort")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/position_add_v1_scale_out_2r_50_20261004/alpha158"))
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/oos_predictions.csv.gz"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--source-config", type=Path,
                        default=ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_conviction_signal_diagnostic_20261004"))
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("refusing to overwrite {}".format(args.output_dir))

    entries = _entry_outcomes(args.run_dir, args.predictions)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20210101, end_date=20260930)
    features = _feature_rows(entries, panel, args.source_config)
    entries = entries.merge(
        features, left_on=["created_asof", "symbol"],
        right_on=["signal_asof", "symbol"], how="left",
        suffixes=("", "_feature"), validate="many_to_one")
    benchmark = next(args.signal_dir.glob("sh000300_*"))
    entries = entries.merge(
        _market_features(benchmark), left_on="created_asof", right_on="date",
        how="left", validate="many_to_one")

    signals = [
        "daily_rank", "score_z", "prior_5_rank", "rank_improvement_5",
        *ALPHA158_LITE_FEATURES,
        "market_return_20d", "market_return_60d", "market_return_120d",
        "market_above_ma20", "market_above_ma60", "market_above_ma120",
        "market_volatility_20d", "market_volatility_60d",
    ]
    diagnostics = _diagnose(entries, signals)
    args.output_dir.mkdir(parents=True)
    entries.to_csv(args.output_dir / "entry_signal_dataset.csv", index=False)
    diagnostics.to_csv(args.output_dir / "signal_diagnostics.csv", index=False)
    summary = {
        "status": "EXPLORATORY_ONLY",
        "outcome": "base_entry_realized_return_after_costs",
        "development_period": "2023-2024",
        "validation_period": "2025-2026_through_20260930",
        "closed_trades": int(len(entries)),
        "development_trades": int((entries.year <= 2024).sum()),
        "validation_trades": int((entries.year >= 2025).sum()),
        "signals_tested": int(len(diagnostics)),
        "stable_candidates": diagnostics.loc[
            diagnostics.stable_candidate, "signal"].tolist(),
        "limitations": [
            "all dates have already been observed during strategy research",
            "multiple exploratory signals were compared",
            "this is trade attribution, not an executable sizing replay",
            "a candidate must be frozen before a new forward test",
        ],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not diagnostics.empty:
        print(diagnostics.head(12).to_string(index=False))


if __name__ == "__main__":
    main()
