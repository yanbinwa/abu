#!/usr/bin/env python3
"""Diagnose VCP timing and portfolio approvals without changing the strategy."""
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


FORWARD_SESSIONS = (5, 10, 20, 40)


def maximum_drawdown(values):
    values = np.asarray(values, dtype=float)
    peaks = np.maximum.accumulate(values)
    return float(np.min(values / peaks - 1)) if len(values) else np.nan


def benchmark_ma200_backtest(frame, start_date, end_date, slippage_bps=25.0):
    """Trade a synthetic CSI 300 unit at next open using prior-close MA200."""
    work = frame.copy().sort_values("date").reset_index(drop=True)
    work["ma200"] = work.close.rolling(200, min_periods=200).mean()
    slip = float(slippage_bps) / 10_000.0
    cash, units, held = 1_000_000.0, 0.0, False
    slippage_cost = 0.0
    entries, exits = 0, 0
    rows = []
    for position, row in work.iterrows():
        if not int(start_date) <= int(row.date) <= int(end_date):
            continue
        prior = work.iloc[position - 1] if position > 0 else None
        desired = bool(
            prior is not None and np.isfinite(prior.ma200) and
            float(prior.close) > float(prior.ma200)
        )
        open_price = float(row.open)
        if desired and not held:
            execution_price = open_price * (1 + slip)
            units = cash / execution_price
            slippage_cost += units * open_price * slip
            cash, held = 0.0, True
            entries += 1
        elif not desired and held:
            slippage_cost += units * open_price * slip
            cash = units * open_price * (1 - slip)
            units, held = 0.0, False
            exits += 1
        capital = cash + units * float(row.close)
        rows.append({
            "date": int(row.date), "capital": capital,
            "exposure": float(held), "signal_above_ma200": desired,
        })
    curve = pd.DataFrame(rows)
    if curve.empty:
        raise ValueError("benchmark period contains no sessions")
    initial = 1_000_000.0
    years = curve.date // 10000
    annual = []
    previous = initial
    for year, group in curve.groupby(years):
        ending = float(group.iloc[-1].capital)
        annual.append({
            "year": int(year), "return_pct": (ending / previous - 1) * 100,
            "ending_capital": ending,
        })
        previous = ending
    first = work[work.date >= int(start_date)].iloc[0]
    last = work[work.date <= int(end_date)].iloc[-1]
    summary = {
        "baseline": "csi300_prior_close_above_ma200_next_open",
        "start": int(curve.iloc[0].date), "end": int(curve.iloc[-1].date),
        "return_pct": (float(curve.iloc[-1].capital) / initial - 1) * 100,
        "max_drawdown_pct": maximum_drawdown(
            np.r_[initial, curve.capital.to_numpy()]) * 100,
        "average_exposure_pct": float(curve.exposure.mean() * 100),
        "slippage_cost": slippage_cost,
        "slippage_bps_per_side": float(slippage_bps),
        "entries": entries, "exits": exits,
        "buy_hold_return_pct": (
            float(last.close) / float(first.open) - 1) * 100,
        "ending_in_market": bool(held),
        "execution_note": (
            "Synthetic index timing baseline; prior close decides next open. "
            "Only execution slippage is charged because the index is not directly tradable."
        ),
    }
    return summary, curve, pd.DataFrame(annual)


def cluster_bootstrap(values, paths=10_000, seed=20261005):
    clean = np.asarray(values, dtype=float)
    clean = clean[np.isfinite(clean)]
    if not len(clean):
        return {"mean": np.nan, "ci_2_5": np.nan, "ci_97_5": np.nan,
                "clusters": 0}
    rng = np.random.default_rng(seed)
    simulated = np.empty(int(paths), dtype=float)
    for index in range(int(paths)):
        simulated[index] = rng.choice(clean, len(clean), replace=True).mean()
    return {
        "mean": float(clean.mean()),
        "ci_2_5": float(np.quantile(simulated, 0.025)),
        "ci_97_5": float(np.quantile(simulated, 0.975)),
        "clusters": int(len(clean)),
    }


def candidate_forward_outcomes(panel, intents, decisions):
    """Calculate signal-time outcomes for every risk-reviewed candidate."""
    columns = [
        "intent_id", "signal_asof", "symbol", "score",
        "signal_price_adjusted", "initial_stop_adjusted",
    ]
    merged = decisions.merge(
        intents[columns], on=["intent_id", "signal_asof", "symbol"],
        how="inner", validate="one_to_one",
    )
    date_index = {int(day): position for position, day in enumerate(panel.dates)}
    rows = []
    for record in merged.to_dict("records"):
        day = date_index.get(int(record["signal_asof"]))
        column = panel.symbol_index.get(str(record["symbol"]))
        if day is None or column is None:
            continue
        close = float(panel.close[day, column])
        stop = float(record["initial_stop_adjusted"])
        planned_r = close - stop
        row = dict(record)
        row["approval_group"] = (
            "allowed" if record["decision"] in ("approved", "reduced")
            else "rejected"
        )
        for sessions in FORWARD_SESSIONS:
            target = day + sessions
            value = (float(panel.close[target, column]) / close - 1
                     if target < len(panel.dates) and close > 0 and
                     np.isfinite(panel.close[target, column]) else np.nan)
            row["return_{}d".format(sessions)] = value
        end = min(len(panel.dates), day + 21)
        future_high = panel.high[day + 1:end, column].astype(float)
        future_low = panel.low[day + 1:end, column].astype(float)
        row["mfe_20d"] = (
            float(np.nanmax(future_high) / close - 1)
            if len(future_high) and np.isfinite(future_high).any() else np.nan)
        row["mae_20d"] = (
            float(np.nanmin(future_low) / close - 1)
            if len(future_low) and np.isfinite(future_low).any() else np.nan)
        row["hit_plus_1r_20d"] = (
            float(np.nanmax(future_high) >= close + planned_r)
            if len(future_high) and planned_r > 0 and
            np.isfinite(future_high).any() else np.nan)
        row["hit_initial_stop_20d"] = (
            float(np.nanmin(future_low) <= stop)
            if len(future_low) and planned_r > 0 and
            np.isfinite(future_low).any() else np.nan)
        rows.append(row)
    return pd.DataFrame(rows)


def approval_diagnostics(outcomes, paths=10_000, seed=20261005):
    metrics = ["return_{}d".format(item) for item in FORWARD_SESSIONS] + [
        "mfe_20d", "mae_20d", "hit_plus_1r_20d", "hit_initial_stop_20d"]
    grouped = outcomes.groupby("approval_group")[metrics].agg(
        ["count", "mean", "median"])
    grouped.columns = ["{}_{}".format(left, right)
                       for left, right in grouped.columns]
    grouped = grouped.reset_index()

    uplift = []
    for offset, metric in enumerate(metrics):
        pivot = outcomes.pivot_table(
            index="signal_asof", columns="approval_group", values=metric,
            aggfunc="mean")
        if {"allowed", "rejected"}.issubset(pivot.columns):
            differences = (pivot.allowed - pivot.rejected).dropna()
        else:
            differences = pd.Series(dtype=float)
        result = cluster_bootstrap(
            differences, paths=paths, seed=seed + offset)
        uplift.append({"metric": metric, **result})
    annual = outcomes.assign(
        year=outcomes.signal_asof.astype(int) // 10000
    ).groupby(["year", "approval_group"]).agg(
        candidates=("intent_id", "size"),
        signal_days=("signal_asof", "nunique"),
        mean_return_5d=("return_5d", "mean"),
        mean_return_20d=("return_20d", "mean"),
        hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
        hit_initial_stop_20d=("hit_initial_stop_20d", "mean"),
    ).reset_index()
    return grouped, pd.DataFrame(uplift), annual


def rejection_reason_table(outcomes):
    rows = []
    for record in outcomes[outcomes.approval_group.eq("rejected")].to_dict("records"):
        reasons = record.get("reason_codes") or ["UNSPECIFIED"]
        for reason in reasons:
            rows.append({
                "reason": reason, "intent_id": record["intent_id"],
                "signal_asof": record["signal_asof"],
                "return_5d": record["return_5d"],
                "return_20d": record["return_20d"],
                "hit_plus_1r_20d": record["hit_plus_1r_20d"],
                "hit_initial_stop_20d": record["hit_initial_stop_20d"],
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).groupby("reason").agg(
        candidates=("intent_id", "size"),
        signal_days=("signal_asof", "nunique"),
        mean_return_5d=("return_5d", "mean"),
        mean_return_20d=("return_20d", "mean"),
        hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
        hit_initial_stop_20d=("hit_initial_stop_20d", "mean"),
    ).reset_index().sort_values(["candidates", "reason"], ascending=[False, True])


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
    parser.add_argument("--start-date", type=int, default=20210101)
    parser.add_argument("--end-date", type=int, default=20241231)
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--bootstrap-paths", type=int, default=10_000)
    parser.add_argument("--minimum-paired-clusters", type=int, default=20)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    benchmark_path = next(args.signal_dir.glob("sh000300_*"))
    benchmark = pd.read_csv(benchmark_path, usecols=["date", "open", "close"])
    timing, timing_curve, timing_annual = benchmark_ma200_backtest(
        benchmark, args.start_date, args.end_date, args.slippage_bps)
    timing_curve.to_csv(args.output_dir / "ma200_timing_curve.csv", index=False)
    timing_annual.to_csv(args.output_dir / "ma200_timing_annual.csv", index=False)
    timing_gross, _, timing_gross_annual = benchmark_ma200_backtest(
        benchmark, args.start_date, args.end_date, 0.0)
    timing_gross_annual.to_csv(
        args.output_dir / "ma200_timing_gross_annual.csv", index=False)

    intents = pd.DataFrame(_read_jsonl(args.backtest_dir / "intents.jsonl"))
    decisions = pd.DataFrame(_read_jsonl(
        args.backtest_dir / "risk_decisions.jsonl"))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20200101, end_date=args.end_date)
    outcomes = candidate_forward_outcomes(panel, intents, decisions)
    grouped, uplift, annual = approval_diagnostics(
        outcomes, paths=args.bootstrap_paths)
    reasons = rejection_reason_table(outcomes)
    outcomes.to_csv(args.output_dir / "candidate_forward_outcomes.csv", index=False)
    grouped.to_csv(args.output_dir / "approval_group_summary.csv", index=False)
    uplift.to_csv(args.output_dir / "approved_vs_rejected_uplift.csv", index=False)
    annual.to_csv(args.output_dir / "approval_group_annual.csv", index=False)
    reasons.to_csv(args.output_dir / "rejection_reason_outcomes.csv", index=False)

    return_20d = uplift.set_index("metric").loc["return_20d"]
    paired_clusters = int(return_20d.clusters)
    if paired_clusters < args.minimum_paired_clusters:
        discrimination_gate = "INSUFFICIENT_PAIRED_CLUSTERS"
    elif float(return_20d.ci_2_5) > 0:
        discrimination_gate = "PASS"
    else:
        discrimination_gate = "FAIL"
    report = {
        "diagnostic_version": "vcp_baseline_diagnostics_v1",
        "evidence_role": "OBSERVED_SAMPLE_DIAGNOSTIC_NOT_NEW_HOLDOUT",
        "strategy_rules_changed": False,
        "ma200_timing_with_slippage": timing,
        "ma200_timing_gross": timing_gross,
        "risk_reviewed_candidates": int(len(outcomes)),
        "allowed_candidates": int(outcomes.approval_group.eq("allowed").sum()),
        "rejected_candidates": int(outcomes.approval_group.eq("rejected").sum()),
        "approved_minus_rejected_return_20d": {
            key: (int(value) if key == "clusters" else float(value))
            for key, value in return_20d.to_dict().items()
        },
        "selection_discrimination_gate": discrimination_gate,
        "minimum_paired_clusters": args.minimum_paired_clusters,
        "interpretation_rule": (
            "Do not relax portfolio risk limits unless allowed candidates show "
            "stable positive forward-return uplift over rejected candidates."
        ),
    }
    report = _json_safe(report)
    (args.output_dir / "diagnostic_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
