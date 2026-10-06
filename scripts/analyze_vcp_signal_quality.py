#!/usr/bin/env python3
"""Diagnose frozen VCP rank factors and losing-entry characteristics."""
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

from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.analyze_vcp_baseline_diagnostics import (  # noqa: E402
    candidate_forward_outcomes, cluster_bootstrap,
)


FACTORS = (
    "score", "residual_momentum", "ma120_slope",
    "contraction_tightness", "breakout_strength",
)
OUTCOMES = (
    "return_5d", "return_20d", "return_40d",
    "hit_plus_1r_20d", "hit_initial_stop_20d",
)


def _rank_correlation(left, right):
    x = pd.Series(left).rank(method="average").to_numpy(dtype=float)
    y = pd.Series(right).rank(method="average").to_numpy(dtype=float)
    x -= x.mean()
    y -= y.mean()
    denominator = np.sqrt(np.dot(x, x) * np.dot(y, y))
    return float(np.dot(x, y) / denominator) if denominator > 0 else np.nan


def within_date_ic(frame, factor, outcome, minimum_candidates=5):
    rows = []
    columns = ["signal_asof", factor, outcome]
    for day, group in frame[columns].dropna().groupby("signal_asof"):
        if (len(group) < minimum_candidates or group[factor].nunique() < 2 or
                group[outcome].nunique() < 2):
            continue
        value = _rank_correlation(group[factor], group[outcome])
        if np.isfinite(value):
            rows.append({"signal_asof": int(day), "ic": value,
                         "candidates": len(group)})
    return pd.DataFrame(rows, columns=["signal_asof", "ic", "candidates"])


def permutation_p_value(frame, factor, outcome, paths=5_000, seed=20261005,
                        minimum_candidates=5):
    groups = []
    columns = ["signal_asof", factor, outcome]
    for _, group in frame[columns].dropna().groupby("signal_asof"):
        if (len(group) < minimum_candidates or group[factor].nunique() < 2 or
                group[outcome].nunique() < 2):
            continue
        x = pd.Series(group[factor]).rank(method="average").to_numpy(dtype=float)
        y = pd.Series(group[outcome]).rank(method="average").to_numpy(dtype=float)
        x -= x.mean(); y -= y.mean()
        denominator = np.sqrt(np.dot(x, x) * np.dot(y, y))
        if denominator > 0:
            groups.append((x, y, denominator))
    if not groups:
        return np.nan
    observed = np.mean([np.dot(x, y) / d for x, y, d in groups])
    rng = np.random.default_rng(seed)
    simulated = np.empty(int(paths), dtype=float)
    for path in range(int(paths)):
        simulated[path] = np.mean([
            np.dot(rng.permutation(x), y) / d for x, y, d in groups])
    return float((1 + np.sum(simulated >= observed)) / (len(simulated) + 1))


def bh_adjust(p_values):
    values = np.asarray(p_values, dtype=float)
    result = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if not len(valid):
        return result
    ordered = valid[np.argsort(values[valid])]
    adjusted = values[ordered] * len(ordered) / np.arange(1, len(ordered) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result[ordered] = np.minimum(adjusted, 1.0)
    return result


def factor_ic_table(frame, bootstrap_paths=10_000, permutation_paths=5_000,
                    seed=20261005):
    rows = []
    for factor_index, factor in enumerate(FACTORS):
        for outcome_index, outcome in enumerate(OUTCOMES):
            values = within_date_ic(frame, factor, outcome)
            summary = cluster_bootstrap(
                values.ic if len(values) else [], paths=bootstrap_paths,
                seed=seed + factor_index * 100 + outcome_index)
            p_value = permutation_p_value(
                frame, factor, outcome, paths=permutation_paths,
                seed=seed + factor_index * 100 + outcome_index)
            rows.append({
                "factor": factor, "outcome": outcome,
                "mean_spearman_ic": summary["mean"],
                "ci_2_5": summary["ci_2_5"],
                "ci_97_5": summary["ci_97_5"],
                "signal_date_clusters": summary["clusters"],
                "positive_direction_p": p_value,
            })
    result = pd.DataFrame(rows)
    primary = result.outcome.eq("return_20d")
    result.loc[primary, "return_20d_fdr_q"] = bh_adjust(
        result.loc[primary, "positive_direction_p"].to_numpy())
    return result


def factor_quintiles(frame):
    rows = []
    for factor in FACTORS:
        pieces = []
        for day, group in frame.dropna(subset=[factor]).groupby("signal_asof"):
            if len(group) < 5 or group[factor].nunique() < 2:
                continue
            work = group.copy()
            percentile = work[factor].rank(method="first", pct=True)
            work["quintile"] = np.ceil(percentile * 5).clip(1, 5).astype(int)
            pieces.append(work)
        if not pieces:
            continue
        ranked = pd.concat(pieces, ignore_index=True)
        daily = ranked.groupby(["signal_asof", "quintile"]).agg(
            return_5d=("return_5d", "mean"),
            return_20d=("return_20d", "mean"),
            return_40d=("return_40d", "mean"),
            hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
            hit_initial_stop_20d=("hit_initial_stop_20d", "mean"),
            candidates=("intent_id", "size"),
        ).reset_index()
        summary = daily.groupby("quintile").agg(
            signal_date_clusters=("signal_asof", "nunique"),
            candidates=("candidates", "sum"),
            mean_return_5d=("return_5d", "mean"),
            mean_return_20d=("return_20d", "mean"),
            mean_return_40d=("return_40d", "mean"),
            hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
            hit_initial_stop_20d=("hit_initial_stop_20d", "mean"),
        ).reset_index()
        summary.insert(0, "factor", factor)
        rows.append(summary)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def signal_geometry(panel, intents):
    date_index = {int(day): position for position, day in enumerate(panel.dates)}
    rows = []
    for record in intents.to_dict("records"):
        day = date_index[int(record["signal_asof"])]
        column = panel.symbol_index[str(record["symbol"])]
        metadata = record["metadata"]
        close = float(panel.close[day, column])
        signal_high = float(panel.high[day, column])
        signal_low = float(panel.low[day, column])
        next_open = (float(panel.open[day + 1, column])
                     if day + 1 < len(panel.dates) else np.nan)
        future_close = panel.close[day + 1:min(day + 6, len(panel.dates)), column]
        breakout = float(metadata.get("breakout_level", np.nan))
        rows.append({
            "intent_id": record["intent_id"],
            "residual_momentum": metadata.get("residual_momentum", np.nan),
            "ma120_slope": metadata.get("ma120_slope", np.nan),
            "contraction_tightness": metadata.get(
                "contraction_tightness", np.nan),
            "breakout_strength": metadata.get("breakout_strength", np.nan),
            "breakout_level": breakout,
            "signal_close_location": (
                (close - signal_low) / (signal_high - signal_low)
                if signal_high > signal_low else np.nan),
            "next_open_gap": next_open / close - 1 if close > 0 else np.nan,
            "stop_distance": (
                1 - float(record["initial_stop_adjusted"]) / close
                if close > 0 else np.nan),
            "false_breakout_5d": (
                float(np.nanmin(future_close) <= breakout)
                if len(future_close) and np.isfinite(future_close).any() and
                np.isfinite(breakout) else np.nan),
        })
    return pd.DataFrame(rows)


def selected_uplift(frame, selected_ids, bootstrap_paths=10_000,
                    seed=20261005):
    work = frame.copy()
    selected = work[work.intent_id.isin(set(selected_ids))]
    rows = []
    for offset, metric in enumerate(OUTCOMES + ("false_breakout_5d",)):
        peers = work.groupby("signal_asof")[metric].mean()
        sample = selected[["signal_asof", metric]].copy()
        sample["peer"] = sample.signal_asof.map(peers)
        clusters = sample.assign(
            difference=sample[metric] - sample.peer
        ).groupby("signal_asof").difference.mean().dropna()
        summary = cluster_bootstrap(
            clusters, paths=bootstrap_paths, seed=seed + offset)
        rows.append({
            "metric": metric, "selected_intents": len(sample),
            "selected_mean": float(sample[metric].mean()),
            "same_day_candidate_mean": float(sample.peer.mean()),
            "cluster_mean_uplift": summary["mean"],
            "ci_2_5": summary["ci_2_5"],
            "ci_97_5": summary["ci_97_5"],
            "entry_clusters": summary["clusters"],
        })
    return pd.DataFrame(rows)


def build_closed_trades(fills, exit_reasons):
    """Pair full-position VCP entries/exits and attach ordered exit reasons."""
    filled = fills[fills.status.eq("filled")].sort_values(
        ["date", "side"], kind="stable")
    reason_queues = {
        symbol: group.sort_values("date").to_dict("records")
        for symbol, group in exit_reasons.groupby("symbol")
    }
    entry_queues = {}
    rows = []
    for record in filled.to_dict("records"):
        symbol = record["symbol"]
        fees = sum(float(record.get(item, 0) or 0) for item in (
            "commission", "transfer_fee", "stamp_tax"))
        if record["side"] == "buy":
            entry_queues.setdefault(symbol, []).append({
                "intent_id": record["intent_id"],
                "entry_date": int(record["date"]),
                "quantity": int(record["quantity"]),
                "remaining": int(record["quantity"]),
                "remaining_cost": (
                    float(record["quantity"]) * float(record["fill_price_raw"]) +
                    fees),
                "entry_price_raw": float(record["fill_price_raw"]),
            })
            continue
        remaining = int(record["quantity"])
        proceeds_per_share = (
            float(record["quantity"]) * float(record["fill_price_raw"]) - fees
        ) / float(record["quantity"])
        reason = (reason_queues.get(symbol, [{}]).pop(0).get("reason", "UNKNOWN")
                  if reason_queues.get(symbol) else "UNKNOWN")
        while remaining > 0:
            lot = entry_queues[symbol][0]
            used = min(remaining, lot["remaining"])
            allocated_cost = lot["remaining_cost"] * used / lot["remaining"]
            rows.append({
                "intent_id": lot["intent_id"], "symbol": symbol,
                "entry_date": lot["entry_date"],
                "exit_date": int(record["date"]), "exit_reason": reason,
                "quantity": used,
                "entry_price_raw": lot["entry_price_raw"],
                "exit_price_raw": float(record["fill_price_raw"]),
                "pnl_ex_dividend": used * proceeds_per_share - allocated_cost,
            })
            lot["remaining_cost"] -= allocated_cost
            lot["remaining"] -= used
            remaining -= used
            if lot["remaining"] == 0:
                entry_queues[symbol].pop(0)
    return pd.DataFrame(rows)


def exit_feature_summary(trades):
    features = [
        "score", "residual_momentum", "ma120_slope",
        "contraction_tightness", "breakout_strength",
        "signal_close_location", "next_open_gap", "stop_distance",
        "false_breakout_5d", "return_5d", "return_20d",
        "mfe_20d", "mae_20d", "hit_plus_1r_20d",
        "hit_initial_stop_20d", "pnl_ex_dividend",
    ]
    result = trades.groupby("exit_reason")[features].agg(["count", "mean", "median"])
    result.columns = ["{}_{}".format(left, right)
                      for left, right in result.columns]
    return result.reset_index()


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()
            if line.strip()]


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--end-date", type=int, default=20241231)
    parser.add_argument("--bootstrap-paths", type=int, default=10_000)
    parser.add_argument("--permutation-paths", type=int, default=5_000)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    intents = pd.DataFrame(_read_jsonl(args.backtest_dir / "intents.jsonl"))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20200101, end_date=args.end_date)
    geometry = signal_geometry(panel, intents)
    pseudo_decisions = intents[["intent_id", "signal_asof", "symbol"]].copy()
    pseudo_decisions["decision"] = "approved"
    pseudo_decisions["reason_codes"] = [[] for _ in range(len(pseudo_decisions))]
    outcomes = candidate_forward_outcomes(panel, intents, pseudo_decisions)
    outcomes = outcomes.merge(geometry, on="intent_id", validate="one_to_one")

    factor_ic = factor_ic_table(
        outcomes, args.bootstrap_paths, args.permutation_paths)
    quintiles = factor_quintiles(outcomes)
    fills = pd.read_csv(args.backtest_dir / "fills.csv")
    selected_ids = fills.loc[
        fills.side.eq("buy") & fills.status.eq("filled"), "intent_id"]
    uplift = selected_uplift(outcomes, selected_ids, args.bootstrap_paths)
    exits = pd.read_csv(args.backtest_dir / "exit_reasons.csv")
    closed = build_closed_trades(fills, exits)
    closed = closed.merge(outcomes, on=["intent_id", "symbol"],
                          validate="many_to_one")
    exit_summary = exit_feature_summary(closed)

    outputs = {
        "all_intent_forward_outcomes.csv": outcomes,
        "factor_within_date_ic.csv": factor_ic,
        "factor_quintiles.csv": quintiles,
        "selected_vs_same_day_candidates.csv": uplift,
        "closed_trade_diagnostics.csv": closed,
        "exit_feature_summary.csv": exit_summary,
    }
    for name, frame in outputs.items():
        frame.to_csv(args.output_dir / name, index=False)

    primary = factor_ic[factor_ic.outcome.eq("return_20d")].set_index("factor")
    selection = uplift.set_index("metric")
    exits_indexed = exit_summary.set_index("exit_reason")
    report = {
        "diagnostic_version": "vcp_signal_quality_v1",
        "evidence_role": "OBSERVED_SAMPLE_DIAGNOSTIC_NOT_NEW_HOLDOUT",
        "strategy_rules_changed": False,
        "intent_count": int(len(outcomes)),
        "signal_date_count": int(outcomes.signal_asof.nunique()),
        "closed_trade_count": int(len(closed)),
        "factor_return_20d": primary[[
            "mean_spearman_ic", "ci_2_5", "ci_97_5",
            "signal_date_clusters", "positive_direction_p",
            "return_20d_fdr_q",
        ]].to_dict("index"),
        "selected_return_20d_uplift": selection.loc["return_20d"].to_dict(),
        "selected_false_breakout_uplift": selection.loc[
            "false_breakout_5d"].to_dict(),
        "exit_feature_means": {
            reason: row.filter(like="_mean").to_dict()
            for reason, row in exits_indexed.iterrows()
        },
        "decision_rule": (
            "No factor or entry feature becomes a trading rule from this "
            "observed-sample diagnostic. A new rule requires preregistration "
            "and independent forward validation."
        ),
    }
    report = _json_safe(report)
    (args.output_dir / "signal_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
