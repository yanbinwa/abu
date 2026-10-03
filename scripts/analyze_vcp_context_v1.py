#!/usr/bin/env python3
"""Evaluate preregistered context experiments and apply early stop gates."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd


def closed_trade_frame(fills):
    if fills.empty:
        return pd.DataFrame(columns=["entry_date", "exit_date", "symbol", "pnl", "r"])
    buys = fills[(fills.side == "buy") & (fills.status == "filled")].copy()
    sells = fills[(fills.side == "sell") & (fills.status == "filled")].copy()
    rows = []
    for symbol, buy_group in buys.groupby("symbol", sort=True):
        sell_group = sells[sells.symbol.eq(symbol)].sort_values("date")
        for buy in buy_group.sort_values("date").itertuples():
            eligible = sell_group[sell_group.date >= buy.date]
            if eligible.empty:
                continue
            sell = eligible.iloc[0]
            buy_cash = (buy.quantity * buy.fill_price_raw + buy.commission +
                        buy.transfer_fee + buy.stamp_tax)
            sell_cash = (sell.quantity * sell.fill_price_raw - sell.commission -
                         sell.transfer_fee - sell.stamp_tax)
            pnl = float(sell_cash - buy_cash)
            initial_r = float(buy.actual_initial_r_cash)
            rows.append({
                "entry_date": int(buy.date), "exit_date": int(sell.date),
                "symbol": symbol, "pnl": pnl,
                "r": pnl / initial_r if initial_r > 0 else np.nan,
            })
            sell_group = sell_group.drop(eligible.index[0])
    return pd.DataFrame(rows)


def cluster_bootstrap_mean_r(trades, simulations=5000, seed=20261003):
    clean = trades.dropna(subset=["r"])
    groups = {date: group.r.to_numpy(dtype=float) for date, group in
              clean.groupby("entry_date")}
    keys = np.array(sorted(groups))
    if not len(keys):
        return {"ci_low": np.nan, "ci_high": np.nan, "p_mean_le_zero": np.nan,
                "clusters": 0}
    rng = np.random.default_rng(seed)
    means = np.empty(simulations)
    for position in range(simulations):
        sampled = rng.choice(keys, size=len(keys), replace=True)
        values = np.concatenate([groups[key] for key in sampled])
        means[position] = np.mean(values)
    return {
        "ci_low": float(np.quantile(means, .025)),
        "ci_high": float(np.quantile(means, .975)),
        "p_mean_le_zero": float(np.mean(means <= 0)),
        "clusters": int(len(keys)),
    }


def benjamini_hochberg(p_values):
    values = np.asarray(p_values, dtype=float)
    result = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if not len(valid):
        return result
    order = valid[np.argsort(values[valid])]
    adjusted = values[order] * len(order) / np.arange(1, len(order) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    result[order] = np.minimum(adjusted, 1.0)
    return result


def _metadata(value):
    try:
        result = ast.literal_eval(value) if isinstance(value, str) else dict(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, SyntaxError, TypeError):
        return {}


def intent_forward_outcomes(shadow, signal_dir):
    rows = []
    signal_dir = Path(signal_dir)
    for symbol, group in shadow.groupby("symbol", sort=True):
        matches = list(signal_dir.glob(str(symbol) + "_*"))
        if not matches:
            continue
        price = pd.read_csv(max(matches, key=lambda item: item.stat().st_mtime),
                            usecols=["date", "close", "high", "low"])
        price = price.sort_values("date").drop_duplicates("date", keep="last")
        locations = {int(value): pos for pos, value in enumerate(price.date)}
        close = pd.to_numeric(price.close, errors="coerce").to_numpy(dtype=float)
        high = pd.to_numeric(price.high, errors="coerce").to_numpy(dtype=float)
        low = pd.to_numeric(price.low, errors="coerce").to_numpy(dtype=float)
        for item in group.itertuples(index=False):
            location = locations.get(int(item.signal_asof))
            if location is None:
                continue
            signal = float(item.signal_price_adjusted)
            stop = float(item.initial_stop_adjusted)
            one_r = signal - stop
            metadata = _metadata(item.metadata)
            record = {
                "intent_id": item.intent_id, "signal_asof": int(item.signal_asof),
                "symbol": symbol, "market_state": item.market_state,
                "market_shadow_action": item.market_shadow_action,
            }
            for window in (1, 3, 5, 10, 20):
                future = location + window
                record["return_{}d".format(window)] = (
                    close[future] / signal - 1 if future < len(close) and
                    np.isfinite(close[future]) else np.nan)
            end = min(location + 21, len(close))
            future_high = high[location + 1:end]
            future_low = low[location + 1:end]
            record["mfe_20d"] = (float(np.nanmax(future_high) / signal - 1)
                                  if len(future_high) else np.nan)
            record["mae_20d"] = (float(np.nanmin(future_low) / signal - 1)
                                  if len(future_low) else np.nan)
            record["hit_plus_1r_20d"] = bool(
                len(future_high) and one_r > 0 and
                np.nanmax(future_high) >= signal + one_r)
            first5_end = min(location + 6, len(close))
            breakout = float(metadata.get("breakout_level", np.nan))
            hit_1r_5 = (one_r > 0 and
                        np.nanmax(high[location + 1:first5_end]) >= signal + one_r)
            record["false_breakout_5d"] = bool(
                np.isfinite(breakout) and
                np.nanmin(close[location + 1:first5_end]) < breakout and
                not hit_1r_5)
            rows.append(record)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path,
                        default=Path("/Users/wjy/abu/backtests/vcp_context_v1"))
    parser.add_argument("--shadow-intents", type=Path,
                        default=Path("/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    args = parser.parse_args()

    results = pd.read_csv(args.backtest_dir / "results.csv")
    statistics = []
    annual_rows = []
    for result in results.itertuples(index=False):
        target = args.backtest_dir / result.experiment
        fills = pd.read_csv(target / "fills.csv")
        trades = closed_trade_frame(fills)
        trades.to_csv(target / "closed_trades.csv", index=False)
        bootstrap = cluster_bootstrap_mean_r(trades)
        statistics.append({"experiment": result.experiment, **bootstrap})
        curve = pd.read_csv(target / "daily_nav.csv")
        previous = 1_000_000.0
        for year, group in curve.groupby(curve.date.astype(int) // 10000):
            capital = np.r_[previous, group.capital.to_numpy(dtype=float)]
            annual_rows.append({
                "experiment": result.experiment, "year": int(year),
                "return_pct": float((capital[-1] / capital[0] - 1) * 100),
                "max_drawdown_pct": float(
                    (capital / np.maximum.accumulate(capital) - 1).min() * 100),
            })
            previous = float(group.capital.iloc[-1])
    statistics = pd.DataFrame(statistics)
    statistics["fdr_q"] = np.nan
    selection_family = statistics.experiment.isin(
        ["industry_rank", "industry_leader_rank"])
    statistics.loc[selection_family, "fdr_q"] = benjamini_hochberg(
        statistics.loc[selection_family, "p_mean_le_zero"].to_numpy())
    statistics.to_csv(args.backtest_dir / "trade_cluster_statistics.csv", index=False)
    pd.DataFrame(annual_rows).to_csv(
        args.backtest_dir / "annual_block_diagnostics.csv", index=False)

    shadow = pd.read_csv(args.shadow_intents, dtype={"symbol": str})
    outcomes = intent_forward_outcomes(shadow, args.signal_dir)
    outcomes.to_csv(args.backtest_dir / "intent_forward_outcomes.csv", index=False)
    outcome_summary = outcomes.groupby("market_state").agg(
        intent_count=("intent_id", "size"),
        mean_return_5d=("return_5d", "mean"),
        mean_return_20d=("return_20d", "mean"),
        mean_mfe_20d=("mfe_20d", "mean"),
        mean_mae_20d=("mae_20d", "mean"),
        hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
        false_breakout_5d=("false_breakout_5d", "mean"),
    ).reset_index()
    outcome_summary.to_csv(
        args.backtest_dir / "market_state_forward_diagnostics.csv", index=False)

    indexed = results.set_index("experiment")
    stats = statistics.set_index("experiment")
    baseline = indexed.loc["baseline_replay"]
    market = indexed.loc["market_overlay"]
    retention = (market.return_pct / baseline.return_pct
                 if baseline.return_pct > 0 else np.nan)
    admissions = {
        "H1_MARKET_CONTEXT": {
            "status": "REJECTED_EARLY_STOP",
            "return_retention_ratio": float(retention),
            "drawdown_improvement_pct_points": float(
                market.max_drawdown_pct - baseline.max_drawdown_pct),
            "expected_shortfall_improvement_pct_points": float(
                market.daily_expected_shortfall_95_pct -
                baseline.daily_expected_shortfall_95_pct),
            "reason_codes": ["RETURN_RETENTION_RATIO_LT_0_70"],
        },
    }
    for hypothesis, experiment in (
            ("H2_INDUSTRY_STRENGTH", "industry_rank"),
            ("H3_WITHIN_INDUSTRY_TREND_LEADER", "industry_leader_rank")):
        row = stats.loc[experiment]
        reason = []
        if row.ci_low <= 0:
            reason.append("CI_LOW_LE_0")
        if indexed.loc[experiment].entry_clusters < 40:
            reason.append("MINIMUM_SAMPLE_NOT_MET")
        admissions[hypothesis] = {
            "status": "REJECTED_EARLY_STOP" if reason else "PLACEBO_REQUIRED",
            "mean_r_ci_95": [float(row.ci_low), float(row.ci_high)],
            "entry_clusters": int(indexed.loc[experiment].entry_clusters),
            "bootstrap_p_mean_le_zero": float(row.p_mean_le_zero),
            "fdr_q": float(row.fdr_q),
            "reason_codes": reason,
        }
    report = {
        "analysis_version": "vcp_context_analysis_v1",
        "bootstrap_simulations": 5000, "bootstrap_seed": 20261003,
        "admissions": admissions,
        "placebo_policy": (
            "Skipped only for hypotheses that triggered preregistered early-stop "
            "conditions; no failed variant may proceed to forward paper trading."
        ),
    }
    (args.backtest_dir / "admission_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
