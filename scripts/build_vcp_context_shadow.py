#!/usr/bin/env python3
"""Attach frozen M0A context to VCP intents without reading P&L or outcomes."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuVCPContext import (  # noqa: E402
    evaluate_market_context, load_vcp_context_config,
    load_vcp_context_registry,
)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _metadata_value(value, key):
    try:
        payload = ast.literal_eval(value) if isinstance(value, str) else dict(value)
    except (ValueError, SyntaxError, TypeError):
        return np.nan
    try:
        result = float(payload.get(key, np.nan))
    except (TypeError, ValueError):
        return np.nan
    return result if np.isfinite(result) else np.nan


def _stable_group_rank(frame, group_columns, value_column, output_column):
    ranks = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, index in frame.groupby(group_columns, sort=True).groups.items():
        subset = frame.loc[index]
        order = subset.assign(
            _missing=subset[value_column].isna(),
            _value=subset[value_column].fillna(-np.inf),
        ).sort_values(
            ["_missing", "_value", "score", "symbol"],
            ascending=[True, False, False, True], kind="mergesort")
        ranks.loc[order.index] = np.arange(1, len(order) + 1, dtype=float)
    frame[output_column] = ranks


def attach_context_frames(intents, breadth, industry, leaders, config):
    result = intents.copy()
    result["signal_asof"] = pd.to_numeric(result.signal_asof).astype(int)
    result["industry_asof"] = pd.to_numeric(
        result.industry_asof, errors="coerce").fillna(-1).astype(int)
    market = breadth[breadth.universe_scope.eq(config.universe_scope)].copy()
    market_columns = [
        "trade_date", "universe_scope", "coverage_ratio",
        "breadth_above_ma120", "breadth_above_ma60",
        "equal_weight_return_5d", "new_low_20d_ratio",
    ]
    market = market[market_columns].drop_duplicates("trade_date")
    result = result.merge(
        market, left_on="signal_asof", right_on="trade_date", how="left",
        validate="many_to_one", suffixes=("", "_market"))

    decisions = {}
    for row in market.to_dict("records"):
        decisions[int(row["trade_date"])] = evaluate_market_context(row, config)
    unknown = evaluate_market_context(None, config)
    result["market_state"] = result.signal_asof.map(
        lambda value: decisions.get(int(value), unknown).state)
    result["market_new_risk_multiplier"] = result.signal_asof.map(
        lambda value: decisions.get(int(value), unknown).new_risk_multiplier)
    result["market_shadow_action"] = result.apply(
        lambda row: "ALLOW_EXIT" if row.side == "sell" else
        decisions.get(int(row.signal_asof), unknown).action, axis=1)

    industry_columns = [
        "trade_date", "industry_id", "coverage_ratio", config.industry_rank_field,
    ]
    industry_frame = industry[industry_columns].rename(columns={
        "coverage_ratio": "industry_context_coverage",
        config.industry_rank_field: "industry_rank_value",
    })
    result = result.merge(
        industry_frame, left_on=["signal_asof", "industry_asof"],
        right_on=["trade_date", "industry_id"], how="left",
        validate="many_to_one", suffixes=("", "_industry"))
    invalid_industry = (
        result.industry_context_coverage.isna() |
        (result.industry_context_coverage < config.industry_minimum_coverage))
    result.loc[invalid_industry, "industry_rank_value"] = np.nan

    leader_columns = [
        "trade_date", "symbol", "industry_id", "coverage_ratio",
        config.leader_rank_field,
    ]
    for optional in ("residual_momentum", "ma120_slope"):
        if optional in leaders.columns:
            leader_columns.append(optional)
    leader_frame = leaders[leader_columns].rename(columns={
        "coverage_ratio": "leader_context_coverage",
        config.leader_rank_field: "leader_rank_value",
        "residual_momentum": "leader_residual_momentum",
        "ma120_slope": "ma120_slope",
    })
    result = result.merge(
        leader_frame, left_on=["signal_asof", "symbol", "industry_asof"],
        right_on=["trade_date", "symbol", "industry_id"], how="left",
        validate="many_to_one", suffixes=("", "_leader"))
    invalid_leader = (
        result.leader_context_coverage.isna() |
        (result.leader_context_coverage < config.leader_minimum_coverage))
    result.loc[invalid_leader, "leader_rank_value"] = np.nan

    _stable_group_rank(
        result, ["signal_asof"], "industry_rank_value", "industry_shadow_rank")
    _stable_group_rank(
        result, ["signal_asof", "industry_asof"], "leader_rank_value",
        "leader_within_industry_shadow_rank")
    reasons = []
    for row in result.itertuples(index=False):
        decision = decisions.get(int(row.signal_asof), unknown)
        codes = list(decision.reason_codes)
        if not np.isfinite(row.industry_rank_value):
            codes.append("INDUSTRY_CONTEXT_MISSING")
        if not np.isfinite(row.leader_rank_value):
            codes.append("INDUSTRY_LEADER_CONTEXT_MISSING")
        reasons.append("|".join(dict.fromkeys(codes)))
    result["context_reason_codes"] = reasons
    return result.drop(columns=[column for column in result.columns
                                if column.startswith("trade_date") or
                                column in ("industry_id",)])


def _read_relevant_leaders(path, dates, symbols, chunksize=200_000):
    usecols = [
        "trade_date", "symbol", "industry_id", "coverage_ratio",
        "residual_momentum_rank_within_industry", "residual_momentum",
        "ma120_slope",
    ]
    selected = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
        keep = chunk.trade_date.isin(dates) & chunk.symbol.astype(str).isin(symbols)
        if keep.any():
            selected.append(chunk[keep])
    return pd.concat(selected, ignore_index=True) if selected else \
        pd.DataFrame(columns=usecols)


def _vcp_price_components(intents, signal_dir):
    """Recompute signal-day tightness and breakout strength, never outcomes."""
    tightness = pd.Series(np.nan, index=intents.index, dtype=float)
    breakout_strength = pd.Series(np.nan, index=intents.index, dtype=float)
    signal_dir = Path(signal_dir)
    for symbol, indices in intents.groupby("symbol", sort=True).groups.items():
        matches = list(signal_dir.glob(str(symbol) + "_*"))
        if not matches:
            continue
        path = max(matches, key=lambda item: item.stat().st_mtime)
        header = pd.read_csv(path, nrows=0).columns
        required = {"date", "close", "high", "low"}
        if not required.issubset(header):
            continue
        price = pd.read_csv(path, usecols=sorted(required))
        price["date"] = pd.to_numeric(price.date, errors="coerce").fillna(0).astype(int)
        price = price.drop_duplicates("date", keep="last").sort_values("date")
        locations = {value: position for position, value in enumerate(price.date)}
        high = pd.to_numeric(price.high, errors="coerce").to_numpy(dtype=float)
        low = pd.to_numeric(price.low, errors="coerce").to_numpy(dtype=float)
        close = pd.to_numeric(price.close, errors="coerce").to_numpy(dtype=float)
        previous = np.roll(close, 1)
        previous[0] = np.nan
        true_range = np.maximum.reduce((
            high - low, np.abs(high - previous), np.abs(low - previous)))
        atr = pd.Series(true_range).rolling(21, min_periods=21).mean().to_numpy()
        for index in indices:
            location = locations.get(int(intents.at[index, "signal_asof"]))
            if location is None or location < 80:
                continue
            contraction_high = high[location - 20:location]
            contraction_low = low[location - 20:location]
            control_high = high[location - 80:location - 20]
            control_low = low[location - 80:location - 20]
            arrays = (contraction_high, contraction_low, control_high, control_low)
            if not all(np.isfinite(values).all() for values in arrays):
                continue
            contraction_range = contraction_high.max() / contraction_low.min() - 1
            control_range = control_high.max() / control_low.min() - 1
            if control_range > 0:
                tightness.at[index] = 1 - contraction_range / control_range
            breakout = _metadata_value(intents.at[index, "metadata"], "breakout_level")
            if np.isfinite(breakout) and np.isfinite(atr[location]) and atr[location] > 0:
                breakout_strength.at[index] = (
                    close[location] - breakout) / atr[location]
    return tightness, breakout_strength


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument(
        "--feature-dir", type=Path,
        default=Path("/Users/wjy/abu/data/selection_research/shortline_features/m0a"))
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("/Users/wjy/abu/backtests/vcp_context_shadow_v1"))
    parser.add_argument(
        "--signal-dir", type=Path,
        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "configs/selection/vcp_context_overlay_v1.json")
    parser.add_argument(
        "--registry", type=Path,
        default=ROOT / "configs/selection/vcp_context_experiment_registry_v1.json")
    args = parser.parse_args()

    config = load_vcp_context_config(args.config)
    registry = load_vcp_context_registry(args.registry)
    intents = pd.read_csv(args.intents, dtype={"symbol": str})
    if not set(intents.strategy_id.astype(str)).issubset({config.base_strategy_version}):
        raise ValueError("intent file contains a strategy outside the frozen base")
    dates = set(pd.to_numeric(intents.signal_asof).astype(int))
    symbols = set(intents.symbol.astype(str))
    breadth_path = args.feature_dir / "market_breadth_asof_close.csv.gz"
    industry_path = args.feature_dir / "industry_strength_daily.csv.gz"
    leader_path = args.feature_dir / "industry_trend_leader_daily.csv.gz"
    breadth = pd.read_csv(breadth_path)
    industry = pd.read_csv(industry_path)
    industry = industry[industry.trade_date.isin(dates)]
    leaders = _read_relevant_leaders(leader_path, dates, symbols)
    shadow = attach_context_frames(intents, breadth, industry, leaders, config)
    shadow["residual_momentum"] = shadow.metadata.map(
        lambda value: _metadata_value(value, "residual_momentum"))
    if "ma120_slope" not in shadow:
        shadow["ma120_slope"] = np.nan
    tightness, breakout_strength = _vcp_price_components(shadow, args.signal_dir)
    shadow["contraction_tightness"] = tightness
    shadow["breakout_strength"] = breakout_strength

    correlation_columns = [
        "industry_rank_value", "leader_rank_value", "score",
        "residual_momentum", "ma120_slope", "contraction_tightness",
        "breakout_strength",
    ]
    correlations = shadow[correlation_columns].corr(method="spearman", min_periods=20)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    shadow.to_csv(args.output_dir / "context_shadow_intents.csv", index=False)
    correlations.to_csv(args.output_dir / "context_component_spearman.csv")
    state = shadow.groupby(
        ["market_state", "market_shadow_action"], dropna=False
    ).size().rename("intent_count").reset_index()
    state.to_csv(args.output_dir / "market_shadow_summary.csv", index=False)
    summary = {
        "milestone": "M8A",
        "status": "shadow_only_no_order_or_fill_changes",
        "intent_count": int(len(shadow)),
        "signal_dates": int(shadow.signal_asof.nunique()),
        "market_context_coverage": float(shadow.market_state.ne("UNKNOWN").mean()),
        "industry_context_coverage": float(shadow.industry_rank_value.notna().mean()),
        "leader_context_coverage": float(shadow.leader_rank_value.notna().mean()),
        "market_state_counts": shadow.market_state.value_counts().sort_index().to_dict(),
        "market_action_counts": shadow.market_shadow_action.value_counts().sort_index().to_dict(),
        "context_config_sha256": config.sha256,
        "registry_sha256": registry["registry_sha256"],
        "baseline_intents_sha256": _sha256(args.intents),
        "feature_files": {
            path.name: _sha256(path) for path in
            (breadth_path, industry_path, leader_path)
        },
        "outcome_columns_read": [],
    }
    (args.output_dir / "shadow_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
