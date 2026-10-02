# -*- encoding: utf-8 -*-
"""Causal regime attribution, path risk and research admission statistics."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


def classify_market_regimes(panel, volatility_lookback=20,
                            threshold_lookback=252, quantile=0.70):
    returns = np.asarray(panel.benchmark_returns, dtype=float)
    realized = pd.Series(returns).rolling(
        volatility_lookback, min_periods=volatility_lookback
    ).std(ddof=1).to_numpy() * np.sqrt(252)
    threshold = np.full(len(returns), np.nan)
    for day in range(threshold_lookback, len(returns)):
        history = realized[day-threshold_lookback:day]
        if np.isfinite(history).sum() == threshold_lookback:
            threshold[day] = np.quantile(history, quantile, method="linear")
    labels = np.full(len(returns), "UNKNOWN", dtype=object)
    known = np.isfinite(realized) & np.isfinite(threshold) & np.isfinite(
        panel.market_ma200)
    trend = np.where(panel.benchmark_close > panel.market_ma200, "UP", "DOWN")
    volatility = np.where(realized > threshold, "HIGH_VOL", "LOW_VOL")
    labels[known] = np.char.add(np.char.add(trend[known], "_"), volatility[known])
    return pd.DataFrame({
        "date": panel.dates, "realized_vol20": realized,
        "vol70_threshold": threshold, "market_state": labels,
    })


def attribute_daily_pnl(curve, regimes, initial_capital=1_000_000.0):
    frame = pd.DataFrame(curve).merge(regimes[["date", "market_state"]],
                                      on="date", how="left")
    capital = np.r_[initial_capital, frame.capital.to_numpy(dtype=float)]
    frame["daily_return"] = capital[1:] / capital[:-1] - 1
    frame["daily_pnl"] = np.diff(capital)
    summary = frame.groupby("market_state", dropna=False).agg(
        sessions=("date", "size"), pnl=("daily_pnl", "sum"),
        mean_daily_return=("daily_return", "mean"),
        volatility=("daily_return", "std"),
    ).reset_index()
    return frame, summary


def attribute_entry_clusters(trades, regimes):
    frame = pd.DataFrame(trades).copy()
    date_column = "entry_date" if "entry_date" in frame else "date"
    result = frame.merge(regimes[["date", "market_state"]],
                         left_on=date_column, right_on="date", how="left")
    result["market_state"] = result.market_state.fillna("UNKNOWN")
    value = "r_multiple" if "r_multiple" in result else "return_pct"
    summary = result.groupby("market_state").agg(
        trades=(value, "size"), mean_outcome=(value, "mean"),
        median_outcome=(value, "median"),
    ).reset_index()
    return result, summary


def closed_trades_from_fills(fills):
    """Pair full-position buy/sell fills into auditable closed trade rows."""
    frame = pd.DataFrame(fills).copy()
    if frame.empty:
        return pd.DataFrame(columns=[
            "symbol", "entry_date", "exit_date", "profit", "return_pct",
            "r_multiple", "entry_cluster",
        ])
    frame = frame[frame.status == "filled"].sort_values(
        ["date", "side", "symbol", "order_id"])
    active = {}; rows = []
    for fill in frame.itertuples(index=False):
        fees = float(fill.commission + fill.transfer_fee + fill.stamp_tax)
        if fill.side == "buy":
            active[fill.symbol] = {
                "entry_date": int(fill.date), "quantity": int(fill.quantity),
                "cost": float(fill.quantity * fill.fill_price_raw + fees),
                "initial_r_cash": float(fill.actual_initial_r_cash),
            }
        elif fill.side == "sell" and fill.symbol in active:
            item = active.pop(fill.symbol)
            proceeds = float(fill.quantity * fill.fill_price_raw - fees)
            profit = proceeds - item["cost"]
            rows.append({
                "symbol": fill.symbol, "entry_date": item["entry_date"],
                "exit_date": int(fill.date), "profit": profit,
                "return_pct": profit / item["cost"] * 100,
                "r_multiple": (profit / item["initial_r_cash"]
                               if item["initial_r_cash"] > 0 else np.nan),
                "entry_cluster": item["entry_date"],
            })
    return pd.DataFrame(rows)


def _path_metrics(returns):
    nav = np.cumprod(1 + returns)
    peaks = np.maximum.accumulate(np.r_[1.0, nav])
    drawdown = np.r_[1.0, nav] / peaks - 1
    underwater = drawdown < -1e-15
    longest = current = 0
    for item in underwater:
        current = current + 1 if item else 0
        longest = max(longest, current)
    return {
        "terminal_return": float(nav[-1] - 1),
        "max_drawdown": float(drawdown.min()),
        "longest_underwater": int(longest),
        "recovery_censored": bool(underwater[-1]),
        "annualized_volatility": float(np.std(returns, ddof=1) * np.sqrt(252)),
    }


def _circular_path(values, block, rng):
    n = len(values); output = []
    while len(output) < n:
        start = int(rng.integers(0, n))
        output.extend(values[(start + offset) % n] for offset in range(block))
    return np.asarray(output[:n], dtype=float)


def _stationary_path(values, average_block, rng):
    n = len(values); output = np.empty(n, dtype=float)
    position = int(rng.integers(0, n)); restart = 1.0 / average_block
    for row in range(n):
        if row and rng.random() < restart:
            position = int(rng.integers(0, n))
        output[row] = values[position]
        position = (position + 1) % n
    return output


def kaplan_meier(durations, censored):
    durations = np.asarray(durations, dtype=int)
    censored = np.asarray(censored, dtype=bool)
    at_risk = len(durations); survival = 1.0; rows = []
    for time in sorted(set(durations.tolist())):
        events = int(((durations == time) & ~censored).sum())
        withdrawals = int(((durations == time) & censored).sum())
        if at_risk and events:
            survival *= 1 - events / at_risk
        rows.append({"duration": int(time), "at_risk": int(at_risk),
                     "events": events, "right_censored": withdrawals,
                     "survival_probability": survival})
        at_risk -= events + withdrawals
    return pd.DataFrame(rows)


def block_bootstrap(daily_returns, paths=5000, seed=20261002,
                    methods=("circular_5", "circular_10", "circular_20",
                             "stationary_10")):
    values = np.asarray(daily_returns, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all() or np.any(values <= -1):
        raise ValueError("daily returns must be finite, greater than -1 and have length >= 2")
    if paths <= 0:
        raise ValueError("paths must be positive")
    all_rows = []; summaries = []; km_tables = {}
    for method_index, method in enumerate(methods):
        rng = np.random.default_rng(seed + method_index * 1_000_003)
        if method.startswith("circular_"):
            block = int(method.rsplit("_", 1)[1])
            sampler = lambda: _circular_path(values, block, rng)
        elif method.startswith("stationary_"):
            block = int(method.rsplit("_", 1)[1])
            sampler = lambda: _stationary_path(values, block, rng)
        else:
            raise ValueError("unknown bootstrap method: {}".format(method))
        rows = []
        for path in range(paths):
            row = _path_metrics(sampler())
            row.update({"method": method, "path": path})
            rows.append(row)
        frame = pd.DataFrame(rows)
        terminals = frame.terminal_return
        cutoff = terminals.quantile(0.05)
        summaries.append({
            "method": method, "paths": paths,
            "terminal_return_median": float(terminals.median()),
            "terminal_return_p05": float(cutoff),
            "expected_shortfall_95": float(terminals[terminals <= cutoff].mean()),
            "max_drawdown_median": float(frame.max_drawdown.median()),
            "max_drawdown_p05": float(frame.max_drawdown.quantile(0.05)),
            "loss_probability": float((terminals < 0).mean()),
            "longest_underwater_median": float(frame.longest_underwater.median()),
            "right_censored_fraction": float(frame.recovery_censored.mean()),
            "interpretation": "path-risk-only; simulated paths are not independent markets",
        })
        km_tables[method] = kaplan_meier(
            frame.longest_underwater, frame.recovery_censored)
        all_rows.extend(rows)
    return pd.DataFrame(summaries), pd.DataFrame(all_rows), km_tables


def cluster_bootstrap_mean(values, clusters, samples=5000, seed=20261002,
                           confidence=0.95):
    frame = pd.DataFrame({"value": values, "cluster": clusters}).dropna()
    grouped = frame.groupby("cluster").value.apply(list)
    keys = grouped.index.to_numpy()
    if not len(keys):
        return {"clusters": 0, "mean": np.nan, "lower": np.nan, "upper": np.nan}
    rng = np.random.default_rng(seed); estimates = np.empty(samples)
    for sample in range(samples):
        chosen = rng.choice(keys, size=len(keys), replace=True)
        observations = [value for key in chosen for value in grouped.loc[key]]
        estimates[sample] = np.mean(observations)
    alpha = 1 - confidence
    return {"clusters": int(len(keys)), "mean": float(frame.value.mean()),
            "lower": float(np.quantile(estimates, alpha/2)),
            "upper": float(np.quantile(estimates, 1-alpha/2)),
            "p_nonpositive": float((np.sum(estimates <= 0) + 1) /
                                   (len(estimates) + 1))}


def benjamini_hochberg(p_values):
    values = np.asarray(p_values, dtype=float)
    if not len(values):
        return values
    if np.any(~np.isfinite(values)) or np.any((values < 0) | (values > 1)):
        raise ValueError("p-values must be finite and in [0,1]")
    order = np.argsort(values, kind="mergesort")
    ranked = values[order]
    adjusted = ranked * len(values) / np.arange(1, len(values)+1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    output = np.empty(len(values)); output[order] = np.minimum(adjusted, 1.0)
    return output


def top_profit_concentration(profits, top=5):
    values = np.asarray(profits, dtype=float)
    positive_total = values[values > 0].sum()
    if positive_total <= 0:
        return np.nan
    return float(np.sort(values[values > 0])[-top:].sum() / positive_total)


@dataclass(frozen=True)
class AdmissionDecision:
    status: str
    reasons: tuple[str, ...]
    metrics: dict


def evaluate_admission(closed_trades, entry_clusters, known_states,
                       cluster_ci_lower, placebo_percentile, annual_blocks_above_median,
                       top5_contribution, q_value, liquidation_drawdown_ok,
                       forward_candidate=False):
    reasons = []
    sample_ok = (closed_trades >= 100 or
                 (entry_clusters >= 40 and known_states >= 3))
    if not sample_ok: reasons.append("INSUFFICIENT_SAMPLE")
    if not cluster_ci_lower > 0: reasons.append("CLUSTER_CI_NOT_POSITIVE")
    threshold = 0.95 if forward_candidate else 0.90
    if not placebo_percentile >= threshold: reasons.append("PLACEBO_PERCENTILE")
    if annual_blocks_above_median < 3: reasons.append("INSUFFICIENT_TIME_BLOCKS")
    if not np.isfinite(top5_contribution) or top5_contribution > 0.50:
        reasons.append("PROFIT_CONCENTRATION")
    if not q_value <= 0.10: reasons.append("FDR")
    if not liquidation_drawdown_ok: reasons.append("LIQUIDATION_DRAWDOWN")
    return AdmissionDecision(
        status="candidate" if not reasons else "research_only",
        reasons=tuple(reasons),
        metrics={"closed_trades": closed_trades, "entry_clusters": entry_clusters,
                 "known_states": known_states, "cluster_ci_lower": cluster_ci_lower,
                 "placebo_percentile": placebo_percentile,
                 "annual_blocks_above_median": annual_blocks_above_median,
                 "top5_contribution": top5_contribution, "q_value": q_value,
                 "liquidation_drawdown_ok": liquidation_drawdown_ok},
    )


def write_forward_registration(path, strategy, hashes, daily_command,
                               data_requirements, exception_policy,
                               start_after="2026-10-02"):
    payload = {
        "status": "FROZEN_FORWARD_OBSERVATION",
        "strategy": strategy, "config_hashes": dict(sorted(hashes.items())),
        "data_requirements": list(data_requirements),
        "daily_command": daily_command, "exception_policy": exception_policy,
        "start_after": start_after, "minimum_calendar_months": 6,
        "minimum_independent_entry_clusters": 30,
        "parameter_changes_during_forward_period": "prohibited",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    payload["registration_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return payload
