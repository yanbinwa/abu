"""Causal industry-first overlays for the frozen Alpha158 A0 research arm."""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuCostAwareAlpha import CostAwareReview


def _percentile(values):
    values = np.asarray(values, dtype=float)
    result = np.full(values.shape, np.nan, dtype=float)
    valid = np.isfinite(values)
    if valid.any():
        result[valid] = pd.Series(values[valid]).rank(
            method="average", pct=True).to_numpy(dtype=float)
    return result


@dataclass(frozen=True)
class IndustryHierarchyConfig:
    strategy_version: str = "alpha158_industry_hierarchical_v1"
    base_strategy_version: str = "alpha158_price_only_no_rank_exit_v1"
    minimum_industry_members: int = 5
    minimum_member_coverage: float = .80
    minimum_industry_feature_count: int = 5
    minimum_leader_feature_count: int = 4
    bottom_industry_percentile: float = .20
    top_industry_percentile: float = .70
    leader_percentile: float = .70
    leader_persistence_reviews: int = 2
    maximum_positions_per_industry: int = 2
    industry_features: tuple = (
        "return_20d", "return_60d", "breadth_above_ma20",
        "breadth_above_ma60", "new_high_20d_ratio", "amount_expansion")
    leader_features: tuple = (
        "relative_return_20d", "relative_return_60d", "ma120_slope_20d",
        "distance_to_120d_high", "liquidity_20d_median")
    variants: dict | None = None
    research_only: bool = True
    paper_admitted: bool = False
    live_admitted: bool = False

    def __post_init__(self):
        if self.minimum_industry_members < 2:
            raise ValueError("minimum_industry_members must be at least two")
        if not 0 < self.minimum_member_coverage <= 1:
            raise ValueError("minimum_member_coverage must be in (0, 1]")
        for value in (self.bottom_industry_percentile,
                      self.top_industry_percentile, self.leader_percentile):
            if not 0 <= value <= 1:
                raise ValueError("percentiles must be in [0, 1]")
        if self.bottom_industry_percentile >= self.top_industry_percentile:
            raise ValueError("bottom percentile must be below top percentile")
        if self.leader_persistence_reviews < 1 or \
                self.maximum_positions_per_industry < 1:
            raise ValueError("persistence and industry cap must be positive")
        if self.minimum_industry_feature_count > len(self.industry_features) or \
                self.minimum_leader_feature_count > len(self.leader_features):
            raise ValueError("minimum feature count exceeds configured features")


def load_industry_hierarchy_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(IndustryHierarchyConfig)}
    if set(payload) != expected:
        raise ValueError("IndustryHierarchyConfig fields mismatch")
    payload["industry_features"] = tuple(payload["industry_features"])
    payload["leader_features"] = tuple(payload["leader_features"])
    return IndustryHierarchyConfig(**payload)


class IndustryHierarchyHistory(object):
    """Build immutable signal-close snapshots using only data through ``day``."""

    def __init__(self, panel, config):
        self.panel = panel
        self.config = config
        self.cache = {}
        self.strict = panel.signal_eligible(
            min_history=120, unknown_st_policy="exclude")

    @staticmethod
    def _endpoint(close, day, window):
        denominator = np.asarray(close[day-window], dtype=float)
        current = np.asarray(close[day], dtype=float)
        return np.divide(
            current, denominator, out=np.full(current.shape, np.nan),
            where=np.isfinite(current) & np.isfinite(denominator) &
            (denominator > 0))-1

    def _amount_expansion(self, day, industry):
        if day < 25:
            return np.nan
        totals = []
        for current in range(day-24, day+1):
            members = ((self.panel.industry[current] == industry) &
                       self.panel.universe_mask[current] &
                       self.panel.st_status_known[current] &
                       ~self.panel.st_status[current])
            amount = np.asarray(self.panel.amount[current], dtype=float)
            valid = members & np.isfinite(amount) & (amount > 0)
            coverage = float(valid.sum()) / float(members.sum()) \
                if members.any() else 0.
            totals.append(float(amount[valid].sum())
                          if coverage >= self.config.minimum_member_coverage
                          else np.nan)
        totals = np.asarray(totals, dtype=float)
        short, control = totals[-5:], totals[:-5]
        if np.isfinite(short).mean() < self.config.minimum_member_coverage or \
                np.isfinite(control).mean() < self.config.minimum_member_coverage:
            return np.nan
        baseline = float(np.nanmedian(control))
        return (float(np.nanmedian(short))/baseline
                if np.isfinite(baseline) and baseline > 0 else np.nan)

    def snapshot(self, day):
        if day in self.cache:
            return self.cache[day]
        if day < 120:
            raise ValueError("industry hierarchy requires 120 prior sessions")
        panel = self.panel
        config = self.config
        size = len(panel.symbols)
        close = np.asarray(panel.close, dtype=float)
        ret20 = self._endpoint(close, day, 20)
        ret60 = self._endpoint(close, day, 60)
        ma20 = np.nanmean(close[day-19:day+1], axis=0)
        ma60 = np.asarray(panel.ma60[day], dtype=float)
        prior_high20 = np.nanmax(
            np.asarray(panel.high[day-20:day], dtype=float), axis=0)
        prior_high120 = np.nanmax(
            np.asarray(panel.high[day-120:day], dtype=float), axis=0)
        liquidity = np.nanmedian(
            np.asarray(panel.amount[day-19:day+1], dtype=float), axis=0)
        slope = np.divide(
            np.log(np.asarray(panel.ma120[day], dtype=float) /
                   np.asarray(panel.ma120[day-19], dtype=float)), 19.,
            out=np.full(size, np.nan),
            where=(np.asarray(panel.ma120[day], dtype=float) > 0) &
                  (np.asarray(panel.ma120[day-19], dtype=float) > 0))
        high_distance = np.divide(
            close[day], prior_high120, out=np.full(size, np.nan),
            where=np.isfinite(prior_high120) & (prior_high120 > 0))-1
        memberships = np.asarray(panel.industry[day], dtype=int)
        strict = np.asarray(self.strict[day], dtype=bool)
        industry_rows = []
        member_lookup = {}
        for industry in sorted(int(value) for value in
                               np.unique(memberships[strict & (memberships >= 0)])):
            members = strict & (memberships == industry)
            count = int(members.sum())
            if count < config.minimum_industry_members:
                continue
            member_lookup[industry] = members
            values = {}
            for name, source in (("return_20d", ret20), ("return_60d", ret60)):
                valid = members & np.isfinite(source)
                values[name] = (float(np.mean(source[valid]))
                                if valid.sum()/count >= config.minimum_member_coverage
                                else np.nan)
            for name, average in (("breadth_above_ma20", ma20),
                                  ("breadth_above_ma60", ma60)):
                valid = members & np.isfinite(average) & np.isfinite(close[day])
                values[name] = (float(np.mean(close[day, valid] > average[valid]))
                                if valid.sum()/count >= config.minimum_member_coverage
                                else np.nan)
            valid = members & np.isfinite(prior_high20) & np.isfinite(close[day])
            values["new_high_20d_ratio"] = (
                float(np.mean(close[day, valid] > prior_high20[valid]))
                if valid.sum()/count >= config.minimum_member_coverage else np.nan)
            values["amount_expansion"] = self._amount_expansion(day, industry)
            industry_rows.append({"industry": industry, **values})
        industry_score = {}
        if industry_rows:
            frame = pd.DataFrame(industry_rows).set_index("industry")
            ranked = frame.rank(method="average", pct=True)
            counts = frame.notna().sum(axis=1)
            score = ranked.mean(axis=1, skipna=True).where(
                counts >= config.minimum_industry_feature_count)
            industry_score = score.to_dict()
        industry_pct = np.full(size, np.nan)
        leader_pct = np.full(size, np.nan)
        for industry, members in member_lookup.items():
            industry_pct[members] = float(industry_score.get(industry, np.nan))
            columns = np.flatnonzero(members)
            industry20 = np.nanmean(ret20[columns])
            industry60 = np.nanmean(ret60[columns])
            leader = np.column_stack([
                ret20[columns]-industry20,
                ret60[columns]-industry60,
                slope[columns], high_distance[columns], liquidity[columns]])
            ranks = np.column_stack([
                _percentile(leader[:, column])
                for column in range(leader.shape[1])])
            count = np.isfinite(leader).sum(axis=1)
            score = np.nanmean(ranks, axis=1)
            score[count < config.minimum_leader_feature_count] = np.nan
            leader_pct[columns] = score
        for array in (industry_pct, leader_pct):
            array.setflags(write=False)
        result = {
            "industry": memberships.copy(),
            "industry_percentile": industry_pct,
            "leader_percentile": leader_pct,
        }
        for array in result.values():
            array.setflags(write=False)
        self.cache[day] = result
        return result


class IndustryHierarchyOverlay(CostAwareReview):
    """Apply one frozen hierarchy variant after A0 persistence selection."""

    VARIANTS = {
        "H1_bottom20_gate", "H2_top30_industry_first",
        "H3_persistent_leader", "H4_industry_cap"}

    def __init__(self, history, variant):
        if variant not in self.VARIANTS:
            raise ValueError("unknown industry hierarchy variant")
        super().__init__(suppress_rank_exits=True)
        self.history = history
        self.config = history.config
        self.variant = variant
        self.leader_streak = {}
        self.entry_decisions = []

    def filter_review(self, panel, executor, day, rank_exits, entry_symbols):
        if panel is not self.history.panel:
            raise ValueError("industry hierarchy belongs to a different panel")
        rank_exits, entry_symbols = super().filter_review(
            panel, executor, day, rank_exits, entry_symbols)
        snapshot = self.history.snapshot(day)
        leader_now = {
            panel.symbols[column]
            for column in np.flatnonzero(
                snapshot["leader_percentile"] >= self.config.leader_percentile)}
        self.leader_streak = {
            symbol: self.leader_streak.get(symbol, 0)+1
            for symbol in leader_now}
        base_order = {symbol: position for position, symbol in enumerate(entry_symbols)}
        candidates = []
        for symbol in entry_symbols:
            column = panel.symbol_index[symbol]
            industry = int(snapshot["industry"][column])
            industry_pct = float(snapshot["industry_percentile"][column])
            leader_pct = float(snapshot["leader_percentile"][column])
            allowed = np.isfinite(industry_pct)
            reason = "ALLOWED"
            if self.variant == "H1_bottom20_gate":
                allowed &= industry_pct >= self.config.bottom_industry_percentile
                reason = "BOTTOM_INDUSTRY" if not allowed else reason
            else:
                allowed &= industry_pct >= self.config.top_industry_percentile
                reason = "NOT_TOP_INDUSTRY" if not allowed else reason
            if allowed and self.variant in (
                    "H3_persistent_leader", "H4_industry_cap"):
                allowed = bool(np.isfinite(leader_pct) and
                               leader_pct >= self.config.leader_percentile and
                               self.leader_streak.get(symbol, 0) >=
                               self.config.leader_persistence_reviews)
                reason = "LEADER_NOT_PERSISTENT" if not allowed else reason
            row = dict(signal_asof=int(panel.dates[day]), symbol=symbol,
                       variant=self.variant, industry_id=industry,
                       industry_percentile=industry_pct,
                       leader_percentile=leader_pct,
                       leader_streak=self.leader_streak.get(symbol, 0),
                       allowed=bool(allowed), reason=reason)
            if allowed:
                candidates.append(row)
            self.entry_decisions.append(row)
        if self.variant == "H2_top30_industry_first":
            candidates.sort(key=lambda row: (
                -row["industry_percentile"],
                base_order[row["symbol"]], row["symbol"]))
        elif self.variant in ("H3_persistent_leader", "H4_industry_cap"):
            candidates.sort(key=lambda row: (
                -row["industry_percentile"],
                -(row["leader_percentile"]
                  if np.isfinite(row["leader_percentile"]) else -np.inf),
                base_order[row["symbol"]], row["symbol"]))
        if self.variant == "H4_industry_cap":
            counts = {}
            for symbol in executor.positions:
                column = panel.symbol_index[symbol]
                industry = int(snapshot["industry"][column])
                counts[industry] = counts.get(industry, 0)+1
            limited = []
            for row in candidates:
                industry = row["industry_id"]
                if counts.get(industry, 0) >= \
                        self.config.maximum_positions_per_industry:
                    row["allowed"] = False
                    row["reason"] = "INDUSTRY_POSITION_CAP"
                    continue
                counts[industry] = counts.get(industry, 0)+1
                limited.append(row)
            candidates = limited
        return rank_exits, [row["symbol"] for row in candidates]


__all__ = [
    "IndustryHierarchyConfig", "IndustryHierarchyHistory",
    "IndustryHierarchyOverlay", "load_industry_hierarchy_config",
]
