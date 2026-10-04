# -*- encoding: utf-8 -*-
"""Attribution and matched-placebo utilities for position-add experiments."""
from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd


def build_trade_attribution(logical_trades, lots, dispositions):
    """Build lot facts and five-level summaries from disposition truth."""
    trades = {item.trade_id: item for item in logical_trades}
    lot_map = {item.lot_id: item for item in lots}
    rows = []
    for item in dispositions:
        lot = lot_map[item.lot_id]
        trade = trades[item.trade_id]
        rows.append({
            "allocation_id": item.allocation_id,
            "selection_strategy_id": trade.selection_strategy_id,
            "trade_id": item.trade_id,
            "source_policy_id": lot.source_policy_id or "BASE_ENTRY",
            "lot_id": item.lot_id,
            "symbol": item.symbol,
            "position_effect": lot.position_effect,
            "disposed_quantity": item.disposed_quantity,
            "gross_proceeds_cash": item.allocated_gross_proceeds_cash,
            "disposed_book_cost_cash": item.disposed_book_cost_cash,
            "sell_fees_cash": (item.allocated_sell_commission_cash +
                               item.allocated_sell_transfer_fee_cash +
                               item.allocated_stamp_tax_cash),
            "realized_pnl_cash": item.realized_pnl_cash,
            "fill_date": item.fill_date,
        })
    facts = pd.DataFrame(rows)
    summaries = {}
    if facts.empty:
        return facts, summaries
    levels = {
        "allocation": ["allocation_id"],
        "selection_strategy": ["selection_strategy_id"],
        "logical_trade": ["trade_id"],
        "add_policy": ["source_policy_id"],
        "lot": ["lot_id"],
    }
    for name, keys in levels.items():
        summaries[name] = facts.groupby(keys, dropna=False, as_index=False).agg(
            disposed_quantity=("disposed_quantity", "sum"),
            realized_pnl_cash=("realized_pnl_cash", "sum"),
            gross_proceeds_cash=("gross_proceeds_cash", "sum"),
            disposed_book_cost_cash=("disposed_book_cost_cash", "sum"),
            sell_fees_cash=("sell_fees_cash", "sum"),
        )
    total = float(facts.realized_pnl_cash.sum())
    for frame in summaries.values():
        if abs(float(frame.realized_pnl_cash.sum())-total) > 1e-8:
            raise AssertionError("attribution PnL conservation failed")
    return facts, summaries


def holm_adjust(p_values):
    values = np.asarray(p_values, dtype=float)
    order = np.argsort(values)
    adjusted = np.empty(len(values), dtype=float)
    running = 0.0
    for rank, index in enumerate(order):
        candidate = min(1.0, (len(values)-rank)*values[index])
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


MATCH_COLUMNS = (
    "signal_asof", "industry_asof", "market_state",
    "holding_age_bucket", "floating_r_bucket", "liquidity_bucket",
)


def partition_matchable_actual_ids(candidates, actual_ids):
    """Return actual IDs with at least one non-actual exact PIT peer."""
    lookup = candidates.set_index("candidate_id", drop=False)
    actual_set = set(actual_ids)
    matched, unmatched = [], []
    for candidate_id in actual_ids:
        row = lookup.loc[candidate_id]
        mask = np.ones(len(candidates), dtype=bool)
        for column in MATCH_COLUMNS:
            mask &= candidates[column].astype(str).eq(str(row[column])).to_numpy()
        pool = candidates.loc[mask & ~candidates.candidate_id.isin(actual_set)]
        (matched if len(pool) else unmatched).append(candidate_id)
    return matched, unmatched


def run_matched_add_placebos(candidates, actual_ids, paths=1000, seed=20261004):
    """Sample outcomes using only signal-time match columns.

    Candidate outcome columns are used only after sampling. Future exit date is
    deliberately absent from the selection predicate.
    """
    required = set(MATCH_COLUMNS) | {"candidate_id", "outcome_pnl_cash"}
    missing = required-set(candidates.columns)
    if missing:
        raise ValueError("placebo candidates missing {}".format(sorted(missing)))
    if paths <= 0:
        raise ValueError("paths must be positive")
    candidates = candidates.copy()
    lookup = candidates.set_index("candidate_id", drop=False)
    actual = lookup.loc[list(actual_ids)]
    pools = []
    for row in actual.itertuples(index=False):
        mask = np.ones(len(candidates), dtype=bool)
        for column in MATCH_COLUMNS:
            mask &= candidates[column].astype(str).eq(
                str(getattr(row, column))).to_numpy()
        pool = candidates.loc[mask & ~candidates.candidate_id.isin(actual_ids),
                              "outcome_pnl_cash"].to_numpy(dtype=float)
        if len(pool) == 0:
            raise ValueError("empty PIT match pool for {}".format(row.candidate_id))
        pools.append(pool)
    random = np.random.RandomState(int(seed))
    distribution = np.asarray([
        sum(float(random.choice(pool)) for pool in pools)
        for _ in range(int(paths))], dtype=float)
    actual_pnl = float(actual.outcome_pnl_cash.sum())
    return {
        "seed": int(seed), "paths": int(paths),
        "actual_pnl_cash": actual_pnl,
        "placebo_mean_pnl_cash": float(distribution.mean()),
        "placebo_p05_pnl_cash": float(np.quantile(distribution, 0.05)),
        "placebo_p95_pnl_cash": float(np.quantile(distribution, 0.95)),
        "actual_percentile": float((distribution <= actual_pnl).mean()),
        "distribution": distribution,
    }
