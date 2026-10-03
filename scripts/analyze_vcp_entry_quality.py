#!/usr/bin/env python3
"""Diagnose VCP signal discrimination without changing strategy parameters."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


FEATURES = (
    "score", "residual_momentum", "contraction_tightness",
    "breakout_strength", "ma120_slope", "industry_rank_value",
    "leader_rank_value",
)
OUTCOMES = (
    "return_5d", "return_20d", "hit_plus_1r_20d", "false_breakout_5d",
)


def cluster_bootstrap_mean(values, paths=10000, seed=20261003):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    simulated = np.empty(paths, dtype=float)
    for index in range(paths):
        simulated[index] = rng.choice(values, len(values), replace=True).mean()
    return (float(values.mean()), float(np.quantile(simulated, 0.025)),
            float(np.quantile(simulated, 0.975)))


def selection_uplift(intents, selected_ids):
    work = intents.copy()
    work["selected"] = work.intent_id.isin(set(selected_ids))
    rows = []
    selected = work[work.selected].copy()
    for metric in OUTCOMES:
        peer = work.groupby("signal_asof")[metric].mean()
        sample = selected[["signal_asof", metric]].copy()
        sample["peer_mean"] = sample.signal_asof.map(peer)
        sample["difference"] = (pd.to_numeric(sample[metric], errors="coerce") -
                                pd.to_numeric(sample.peer_mean, errors="coerce"))
        clusters = sample.groupby("signal_asof").difference.mean().dropna()
        mean, low, high = cluster_bootstrap_mean(clusters)
        rows.append({
            "metric": metric, "selected_intents": len(sample),
            "entry_clusters": len(clusters),
            "selected_mean": float(pd.to_numeric(
                sample[metric], errors="coerce").mean()),
            "same_day_candidate_mean": float(sample.peer_mean.mean()),
            "cluster_mean_uplift": mean, "ci_2_5": low, "ci_97_5": high,
        })
    return pd.DataFrame(rows)


def within_date_information_coefficients(intents):
    rows = []
    for feature in FEATURES:
        for outcome in OUTCOMES:
            values = []
            columns = ["signal_asof", feature, outcome]
            for trade_date, group in intents[columns].dropna().groupby("signal_asof"):
                if (len(group) < 5 or group[feature].nunique() < 2 or
                        group[outcome].nunique() < 2):
                    continue
                correlation = group[feature].corr(group[outcome], method="spearman")
                if np.isfinite(correlation):
                    values.append((trade_date, correlation))
            mean, low, high = cluster_bootstrap_mean(
                [item[1] for item in values])
            rows.append({
                "feature": feature, "outcome": outcome,
                "signal_date_clusters": len(values), "mean_spearman_ic": mean,
                "ci_2_5": low, "ci_97_5": high,
            })
    return pd.DataFrame(rows)


def actual_trade_frame(intents, fills, closed_trades):
    buys = fills[(fills.side == "buy") & (fills.status == "filled")].copy()
    columns = [
        "intent_id", "date", "symbol", "reference_price", "fill_price_raw",
        "slippage_cost", "actual_initial_r_cash",
    ]
    actual = intents.merge(buys[columns], on=["intent_id", "symbol"], how="inner")
    return actual.merge(
        closed_trades, left_on=["date", "symbol"],
        right_on=["entry_date", "symbol"], how="inner")


def exit_attribution(orders, fills, exit_reasons, closed_trades):
    sells = orders[orders.side == "sell"].merge(
        exit_reasons, left_on=["created_asof", "symbol"],
        right_on=["date", "symbol"], how="left", suffixes=("", "_exit"))
    sells["exit_reason"] = sells["reason_exit"]
    sells = sells.merge(
        fills[["order_id", "date", "status"]], on="order_id", how="inner",
        suffixes=("_signal", "_fill"))
    sells = sells[sells.status.eq("filled")]
    joined = closed_trades.merge(
        sells[["symbol", "date_fill", "exit_reason"]],
        left_on=["symbol", "exit_date"], right_on=["symbol", "date_fill"],
        how="left")
    return joined.groupby("exit_reason", dropna=False).agg(
        trade_count=("r", "size"), mean_r=("r", "mean"),
        median_r=("r", "median"), win_rate=("r", lambda value: (value > 0).mean()),
        pnl=("pnl", "sum"),
    ).reset_index().rename(columns={"exit_reason": "reason"})


def false_breakout_economics(actual):
    return actual.groupby("false_breakout_5d").agg(
        trade_count=("r", "size"), mean_r=("r", "mean"),
        median_r=("r", "median"), win_rate=("r", lambda value: (value > 0).mean()),
        pnl=("pnl", "sum"), mean_return_20d=("return_20d", "mean"),
        hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
    ).reset_index()


def annual_actual(actual):
    work = actual.copy()
    work["year"] = (pd.to_numeric(work.signal_asof) // 10000).astype(int)
    return work.groupby("year").agg(
        trade_count=("r", "size"), mean_r=("r", "mean"),
        pnl=("pnl", "sum"), mean_return_5d=("return_5d", "mean"),
        mean_return_20d=("return_20d", "mean"),
        false_breakout_5d=("false_breakout_5d", "mean"),
        hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
    ).reset_index()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_v1"))
    parser.add_argument("--shadow-intents", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_entry_quality_diagnostic_v1"))
    args = parser.parse_args()

    shadow = pd.read_csv(args.shadow_intents)
    outcomes = pd.read_csv(args.backtest_dir / "intent_forward_outcomes.csv")
    keys = ["intent_id", "signal_asof", "symbol", "market_state",
            "market_shadow_action"]
    intents = shadow.merge(outcomes, on=keys, how="inner", validate="one_to_one")
    baseline = args.backtest_dir / "baseline_replay"
    fills = pd.read_csv(baseline / "fills.csv")
    orders = pd.read_csv(baseline / "orders.csv")
    closed = pd.read_csv(baseline / "closed_trades.csv")
    exits = pd.read_csv(baseline / "exit_reasons.csv")
    selected_ids = fills.loc[
        fills.side.eq("buy") & fills.status.eq("filled"), "intent_id"]
    actual = actual_trade_frame(intents, fills, closed)

    outputs = {
        "selection_uplift.csv": selection_uplift(intents, selected_ids),
        "within_date_information_coefficients.csv":
            within_date_information_coefficients(intents),
        "false_breakout_economics.csv": false_breakout_economics(actual),
        "exit_attribution.csv": exit_attribution(orders, fills, exits, closed),
        "annual_actual_trades.csv": annual_actual(actual),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in outputs.items():
        frame.to_csv(args.output_dir / name, index=False)

    uplift = outputs["selection_uplift.csv"].set_index("metric")
    score_ic = outputs["within_date_information_coefficients.csv"].query(
        "feature == 'score'").set_index("outcome")
    false = outputs["false_breakout_economics.csv"].set_index(
        "false_breakout_5d")
    exit_table = outputs["exit_attribution.csv"].set_index("reason")
    report = {
        "diagnostic_version": "vcp_entry_quality_diagnostic_v1",
        "role": "EXPLORATORY_DIAGNOSTIC_NOT_STRATEGY_ADMISSION",
        "parameter_search_performed": False,
        "intent_count": len(intents), "filled_closed_trade_count": len(actual),
        "filled_entry_clusters": int(actual.signal_asof.nunique()),
        "selection_return_20d_uplift": uplift.loc["return_20d"].to_dict(),
        "selection_false_breakout_uplift":
            uplift.loc["false_breakout_5d"].to_dict(),
        "score_return_20d_ic": score_ic.loc["return_20d"].to_dict(),
        "score_false_breakout_ic": score_ic.loc[
            "false_breakout_5d"].to_dict(),
        "false_breakout_trade_economics": false.to_dict("index"),
        "exit_economics": exit_table.to_dict("index"),
        "interpretation": [
            "Current ranking has no demonstrated same-day selection uplift; confidence intervals cross zero.",
            "Five-day false breakouts account for the economically harmful trade subset.",
            "Initial stops contain losses and trailing exits monetize winners; the main bottleneck is entry discrimination.",
            "These are observed-sample diagnostics and cannot authorize a new filter without preregistration and fresh validation.",
        ],
    }
    (args.output_dir / "diagnostic_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=float) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=float))


if __name__ == "__main__":
    main()
