# -*- encoding: utf-8 -*-
"""Paired account evaluation for frozen ML research experiments.

The evaluator deliberately keeps the candidate and baseline return curves
separate.  Moving-block paths share indices, but each account's equity and
non-linear risk metrics are rebuilt before differences are taken.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .ABuMLContracts import MLContractError


@dataclass(frozen=True)
class AccountMetrics:
    cumulative_return: float
    annualized_return: float
    max_drawdown: float
    es95: float
    calmar: float

    def payload(self):
        return asdict(self)


@dataclass(frozen=True)
class MetricDifference:
    cumulative_return: float
    annualized_return: float
    max_drawdown: float
    es95: float
    calmar: float

    def payload(self):
        return asdict(self)


def account_metrics(returns, sessions_per_year=252):
    values = np.asarray(returns, dtype=float)
    if values.ndim != 1 or not len(values) or not np.isfinite(values).all():
        raise MLContractError("account returns must be a finite non-empty vector")
    if np.any(values <= -1.0):
        raise MLContractError("daily return cannot be less than or equal to -100%")
    equity = np.cumprod(1.0 + values)
    cumulative = float(equity[-1] - 1.0)
    annualized = float(equity[-1] ** (sessions_per_year/len(values)) - 1.0)
    running_peak = np.maximum.accumulate(np.r_[1.0, equity])
    drawdowns = np.r_[1.0, equity]/running_peak - 1.0
    max_drawdown = float(np.min(drawdowns))
    tail_count = max(1, int(np.ceil(0.05*len(values))))
    es95 = float(np.mean(np.sort(values)[:tail_count]))
    calmar = (float(annualized/abs(max_drawdown))
              if max_drawdown < 0 else float("inf"))
    return AccountMetrics(cumulative, annualized, max_drawdown, es95, calmar)


def metric_difference(candidate, baseline):
    return MetricDifference(**{
        name: float(getattr(candidate, name)-getattr(baseline, name))
        for name in MetricDifference.__dataclass_fields__})


def align_account_returns(candidate, baseline, date_column="date",
                          return_column="net_return"):
    """Return a strict one-to-one inner alignment without silently dropping days."""
    for label, frame in (("candidate", candidate), ("baseline", baseline)):
        missing = {date_column, return_column} - set(frame.columns)
        if missing:
            raise MLContractError("{} return frame missing {}".format(
                label, ", ".join(sorted(missing))))
        if frame[date_column].duplicated().any():
            raise MLContractError("{} account has duplicate dates".format(label))
    candidate_dates = set(candidate[date_column])
    baseline_dates = set(baseline[date_column])
    if candidate_dates != baseline_dates:
        raise MLContractError("candidate and baseline account dates differ")
    merged = candidate[[date_column, return_column]].merge(
        baseline[[date_column, return_column]], on=date_column,
        how="inner", validate="one_to_one", suffixes=("_candidate", "_baseline"))
    return merged.sort_values(date_column, kind="mergesort").reset_index(drop=True)


def moving_block_indices(length, block_sessions, paths, seed):
    if length <= 0 or block_sessions <= 0 or block_sessions > length or paths <= 0:
        raise MLContractError("invalid moving-block bootstrap dimensions")
    rng = np.random.default_rng(seed)
    blocks_per_path = int(np.ceil(length/block_sessions))
    last_start = length-block_sessions
    result = np.empty((paths, length), dtype=np.int64)
    offsets = np.arange(block_sessions, dtype=np.int64)
    for path in range(paths):
        starts = rng.integers(0, last_start+1, size=blocks_per_path)
        result[path] = np.concatenate([start+offsets for start in starts])[:length]
    return result


def _interval(values, alpha=0.05):
    array = np.asarray(values, dtype=float)
    return {
        "lower": float(np.quantile(array, alpha/2.0)),
        "median": float(np.quantile(array, 0.5)),
        "upper": float(np.quantile(array, 1.0-alpha/2.0)),
        "width": float(np.quantile(array, 1.0-alpha/2.0) -
                       np.quantile(array, alpha/2.0)),
    }


def paired_account_bootstrap(candidate_returns, baseline_returns,
                             block_sessions=20, paths=5000,
                             seed=20261007):
    candidate = np.asarray(candidate_returns, dtype=float)
    baseline = np.asarray(baseline_returns, dtype=float)
    if candidate.shape != baseline.shape or candidate.ndim != 1:
        raise MLContractError("paired accounts must have equal one-dimensional paths")
    indices = moving_block_indices(len(candidate), block_sessions, paths, seed)
    samples = {name: [] for name in MetricDifference.__dataclass_fields__}
    for path_indices in indices:
        candidate_metrics = account_metrics(candidate[path_indices])
        baseline_metrics = account_metrics(baseline[path_indices])
        difference = metric_difference(candidate_metrics, baseline_metrics)
        for name in samples:
            samples[name].append(getattr(difference, name))
    return {
        "block_sessions": int(block_sessions),
        "paths": int(paths),
        "seed": int(seed),
        "intervals": {name: _interval(values)
                      for name, values in samples.items()},
    }


def top_positive_profit_share(trade_profits, top_n=5):
    profits = np.asarray(trade_profits, dtype=float)
    if profits.ndim != 1 or not np.isfinite(profits).all():
        raise MLContractError("trade profits must be a finite vector")
    positive = np.sort(profits[profits > 0])[::-1]
    if not len(positive):
        return None
    return float(positive[:top_n].sum()/positive.sum())

