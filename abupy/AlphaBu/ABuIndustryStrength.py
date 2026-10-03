# -*- encoding: utf-8 -*-
"""PIT industry strength and VCP trend-leader features."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .ABuMarketBreadth import (
    MarketIndustryContextConfig, _prior_rolling_extreme, _rolling_mean,
)


def _endpoint_return(close, window):
    result = np.full(close.shape, np.nan, dtype=np.float64)
    if window >= len(close):
        return result
    denominator = close[:-window]
    valid = np.isfinite(close[window:]) & np.isfinite(denominator) & (denominator > 0)
    values = np.divide(
        close[window:], denominator,
        out=np.full(denominator.shape, np.nan), where=valid,
    ) - 1
    result[window:] = values
    return result


def _mad(values):
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if not len(values):
        return np.nan
    median = np.median(values)
    return float(np.median(np.abs(values - median)))


def _rank(values, ascending=True):
    return pd.Series(values).rank(
        method="average", pct=True, ascending=ascending
    ).to_numpy(dtype=float)


def _available_at(date):
    value = str(int(date))
    return "{}-{}-{}T15:05:00+08:00".format(value[:4], value[4:6], value[6:])


class IndustryStrengthBuilder(object):
    """Build daily industry aggregates from historical PIT memberships."""

    RANK_COLUMNS = (
        "return_20d", "return_60d", "excess_return_20d_vs_market",
        "breadth_above_ma20", "breadth_above_ma60", "breadth_above_ma120",
        "new_high_20d_ratio", "positive_member_ratio", "amount_expansion",
    )

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or MarketIndustryContextConfig()

    def _industry_amount_totals(self, industry_ids):
        count = len(self.panel.dates)
        totals = np.full((count, len(industry_ids)), np.nan, dtype=np.float64)
        coverage = np.zeros_like(totals)
        id_position = {value: position for position, value in enumerate(industry_ids)}
        for day in range(count):
            memberships = self.panel.industry[day]
            eligible = (self.panel.universe_mask[day] &
                        self.panel.st_status_known[day] &
                        ~self.panel.st_status[day])
            for industry in np.unique(memberships[eligible & (memberships >= 0)]):
                members = eligible & (memberships == industry)
                valid = members & np.isfinite(self.panel.amount[day]) & (
                    self.panel.amount[day] > 0)
                position = id_position[int(industry)]
                coverage[day, position] = (
                    float(valid.sum()) / float(members.sum()) if members.any() else 0)
                if members.any() and coverage[day, position] >= \
                        self.config.minimum_member_coverage:
                    totals[day, position] = float(np.sum(self.panel.amount[day, valid]))
        return totals, coverage, id_position

    def build(self, start_date=None, end_date=None):
        close = np.asarray(self.panel.close, dtype=np.float64)
        returns = {window: _endpoint_return(close, window)
                   for window in self.config.return_windows}
        ma = {window: _rolling_mean(close, window)
              for window in self.config.ma_windows}
        prior_high = _prior_rolling_extreme(
            np.asarray(self.panel.high, dtype=np.float64),
            self.config.new_high_window, "max")
        industry_ids = sorted(int(value) for value in np.unique(self.panel.industry)
                              if int(value) >= 0)
        amount_totals, _, id_position = self._industry_amount_totals(industry_ids)
        labels = getattr(self.panel, "industry_labels", {})
        rows = []
        for day, date in enumerate(self.panel.dates):
            date = int(date)
            if start_date is not None and date < int(start_date):
                continue
            if end_date is not None and date > int(end_date):
                continue
            memberships = self.panel.industry[day]
            eligible = (self.panel.universe_mask[day] &
                        self.panel.st_status_known[day] &
                        ~self.panel.st_status[day])
            benchmark_20d = np.nan
            if day >= 20 and np.isfinite(self.panel.benchmark_close[day]) and \
                    np.isfinite(self.panel.benchmark_close[day - 20]) and \
                    self.panel.benchmark_close[day - 20] > 0:
                benchmark_20d = (self.panel.benchmark_close[day] /
                                 self.panel.benchmark_close[day - 20] - 1)
            for industry in np.unique(memberships[eligible & (memberships >= 0)]):
                industry = int(industry)
                members = eligible & (memberships == industry)
                member_count = int(members.sum())
                if member_count < self.config.minimum_industry_members:
                    continue
                row = {
                    "trade_date": date,
                    "industry_id": industry,
                    "industry_name": str(labels.get(industry, industry)),
                    "eligible_member_count": member_count,
                    "available_at": _available_at(date),
                    "feature_version": self.config.feature_version,
                    "config_sha256": self.config.sha256,
                }
                coverages = []
                for window in self.config.return_windows:
                    valid = members & np.isfinite(returns[window][day])
                    coverage = float(valid.sum()) / member_count
                    coverages.append(coverage)
                    row["return_{}d_coverage".format(window)] = coverage
                    row["return_{}d".format(window)] = (
                        float(np.mean(returns[window][day, valid]))
                        if coverage >= self.config.minimum_member_coverage else np.nan)
                row["excess_return_20d_vs_market"] = (
                    row["return_20d"] - benchmark_20d
                    if np.isfinite(row["return_20d"]) and np.isfinite(benchmark_20d)
                    else np.nan)
                for window in self.config.ma_windows:
                    valid = members & np.isfinite(ma[window][day]) & np.isfinite(close[day])
                    coverage = float(valid.sum()) / member_count
                    coverages.append(coverage)
                    row["breadth_above_ma{}".format(window)] = (
                        float(np.mean(close[day, valid] > ma[window][day, valid]))
                        if coverage >= self.config.minimum_member_coverage else np.nan)
                high_valid = members & np.isfinite(prior_high[day]) & np.isfinite(close[day])
                high_coverage = float(high_valid.sum()) / member_count
                coverages.append(high_coverage)
                row["new_high_20d_ratio"] = (
                    float(np.mean(close[day, high_valid] > prior_high[day, high_valid]))
                    if high_coverage >= self.config.minimum_member_coverage else np.nan)
                daily_valid = members & np.isfinite(self.panel.returns[day])
                daily_coverage = float(daily_valid.sum()) / member_count
                coverages.append(daily_coverage)
                row["positive_member_ratio"] = (
                    float(np.mean(self.panel.returns[day, daily_valid] > 0))
                    if daily_coverage >= self.config.minimum_member_coverage else np.nan)
                row["median_member_return"] = (
                    float(np.median(self.panel.returns[day, daily_valid]))
                    if daily_coverage >= self.config.minimum_member_coverage else np.nan)
                row["return_dispersion"] = _mad(returns[20][day, members])
                short_start = day - self.config.amount_short_window + 1
                control_end = short_start
                control_start = control_end - self.config.amount_control_window
                amount_expansion = np.nan
                if control_start >= 0:
                    position = id_position[industry]
                    short = amount_totals[short_start:day + 1, position]
                    control = amount_totals[control_start:control_end, position]
                    short_valid = np.isfinite(short)
                    control_valid = np.isfinite(control)
                    if (short_valid.mean() >= self.config.minimum_member_coverage and
                            control_valid.mean() >= self.config.minimum_member_coverage):
                        baseline = float(np.median(control[control_valid]))
                        if baseline > 0:
                            amount_expansion = float(
                                np.median(short[short_valid]) / baseline)
                row["amount_expansion"] = amount_expansion
                row["coverage_ratio"] = float(min(coverages)) if coverages else 0.0
                rows.append(row)
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        for column in self.RANK_COLUMNS:
            frame[column + "_rank"] = frame.groupby("trade_date")[column].rank(
                method="average", pct=True, ascending=True)
        return frame.sort_values(["trade_date", "industry_id"]).reset_index(drop=True)


def _residual_momentum_day(panel, day):
    count_symbols = len(panel.symbols)
    result = np.full(count_symbols, np.nan, dtype=np.float64)
    regression_start = day - 251
    regression_end = day - 20
    formation_start = regression_end - 105
    if regression_start < 0 or formation_start < 0:
        return result
    regression = np.asarray(panel.returns[regression_start:regression_end], dtype=np.float64)
    market = np.asarray(panel.benchmark_returns[regression_start:regression_end],
                        dtype=np.float64)
    valid = np.isfinite(regression) & np.isfinite(market[:, None])
    observations = valid.sum(axis=0)
    market_matrix = market[:, None]
    market_mean = np.divide(
        np.where(valid, market_matrix, 0).sum(axis=0), observations,
        out=np.zeros(count_symbols), where=observations > 0)
    stock_mean = np.divide(
        np.where(valid, regression, 0).sum(axis=0), observations,
        out=np.zeros(count_symbols), where=observations > 0)
    numerator = np.where(
        valid, (market_matrix - market_mean) * (regression - stock_mean), 0
    ).sum(axis=0)
    denominator = np.where(
        valid, (market_matrix - market_mean) ** 2, 0
    ).sum(axis=0)
    beta = np.divide(numerator, denominator, out=np.full(count_symbols, np.nan),
                     where=denominator > 0)
    formation = np.asarray(panel.returns[formation_start:regression_end],
                           dtype=np.float64)
    formation_market = np.asarray(
        panel.benchmark_returns[formation_start:regression_end, None],
        dtype=np.float64)
    formation_valid = np.isfinite(formation) & np.isfinite(formation_market)
    score = np.nansum(formation - beta[None, :] * formation_market, axis=0)
    usable = ((observations >= 200) &
              (formation_valid.sum(axis=0) >= 95) &
              np.isfinite(beta) & np.isfinite(score))
    result[usable] = score[usable]
    return result


class IndustryTrendLeaderBuilder(object):
    """Build stock-level ranks inside each PIT industry."""

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or MarketIndustryContextConfig()

    def _industry_daily_returns(self, industry_ids):
        result = np.full((len(self.panel.dates), len(industry_ids)), np.nan)
        positions = {value: position for position, value in enumerate(industry_ids)}
        for day in range(len(self.panel.dates)):
            memberships = self.panel.industry[day]
            eligible = (self.panel.universe_mask[day] &
                        self.panel.st_status_known[day] &
                        ~self.panel.st_status[day] &
                        np.isfinite(self.panel.returns[day]))
            for industry in np.unique(memberships[eligible & (memberships >= 0)]):
                values = self.panel.returns[day, eligible & (memberships == industry)]
                if len(values):
                    result[day, positions[int(industry)]] = float(np.mean(values))
        return result, positions

    def build(self, start_date=None, end_date=None):
        close = np.asarray(self.panel.close, dtype=np.float64)
        high = np.asarray(self.panel.high, dtype=np.float64)
        return20 = _endpoint_return(close, 20)
        return60 = _endpoint_return(close, 60)
        ma120 = _rolling_mean(close, 120)
        prior_high20 = _prior_rolling_extreme(
            high, self.config.breakout_window, "max")
        prior_high120 = _prior_rolling_extreme(
            high, self.config.leader_high_window, "max")
        liquidity = pd.DataFrame(np.asarray(self.panel.amount, dtype=np.float64)).rolling(
            20, min_periods=16).median().to_numpy()
        breakout = np.isfinite(close) & np.isfinite(prior_high20) & (close > prior_high20)
        industry_ids = sorted(int(value) for value in np.unique(self.panel.industry)
                              if int(value) >= 0)
        industry_daily, industry_positions = self._industry_daily_returns(industry_ids)
        labels = getattr(self.panel, "industry_labels", {})
        strict = self.panel.signal_eligible(
            min_history=120, unknown_st_policy="exclude")
        rows = []
        slope_window = self.config.leader_slope_window
        x = np.arange(slope_window, dtype=float)
        centered = x - x.mean()
        slope_denominator = float(np.sum(centered ** 2))
        for day, date in enumerate(self.panel.dates):
            date = int(date)
            if start_date is not None and date < int(start_date):
                continue
            if end_date is not None and date > int(end_date):
                continue
            if day < max(251, self.config.leader_high_window, slope_window - 1):
                continue
            residual = _residual_momentum_day(self.panel, day)
            memberships = self.panel.industry[day]
            for industry in np.unique(memberships[strict[day] & (memberships >= 0)]):
                industry = int(industry)
                members = strict[day] & (memberships == industry)
                if members.sum() < self.config.minimum_industry_members:
                    continue
                columns = np.flatnonzero(members)
                industry_return20 = np.nanmean(return20[day, columns])
                industry_return60 = np.nanmean(return60[day, columns])
                values = ma120[day - slope_window + 1:day + 1, columns]
                slope_valid = np.isfinite(values).all(axis=0) & (values > 0).all(axis=0)
                slopes = np.full(len(columns), np.nan)
                if slope_valid.any():
                    logged = np.log(values[:, slope_valid])
                    slopes[slope_valid] = np.sum(
                        (logged - logged.mean(axis=0)) * centered[:, None], axis=0
                    ) / slope_denominator
                distance = np.divide(
                    close[day, columns], prior_high120[day, columns],
                    out=np.full(len(columns), np.nan),
                    where=np.isfinite(prior_high120[day, columns]) &
                    (prior_high120[day, columns] > 0),
                ) - 1
                amount = np.asarray(self.panel.amount[day, columns], dtype=float)
                valid_amount = np.isfinite(amount) & (amount > 0)
                total_amount = np.sum(amount[valid_amount])
                shares = np.divide(
                    amount, total_amount, out=np.full(len(columns), np.nan),
                    where=valid_amount & (total_amount > 0))
                capture_start = day - self.config.capture_window + 1
                market_slice = self.panel.benchmark_returns[capture_start:day + 1]
                industry_slice = industry_daily[
                    capture_start:day + 1, industry_positions[industry]]
                stock_slice = self.panel.returns[capture_start:day + 1, columns]
                up = np.isfinite(market_slice) & (market_slice > 0)
                down = np.isfinite(market_slice) & (market_slice < 0)
                up_capture = np.full(len(columns), np.nan)
                down_resilience = np.full(len(columns), np.nan)
                for position in range(len(columns)):
                    valid = np.isfinite(stock_slice[:, position]) & np.isfinite(industry_slice)
                    up_valid = valid & up
                    down_valid = valid & down
                    if up_valid.sum() >= self.config.minimum_capture_sessions:
                        up_capture[position] = np.mean(
                            stock_slice[up_valid, position] - industry_slice[up_valid])
                    if down_valid.sum() >= self.config.minimum_capture_sessions:
                        down_resilience[position] = np.mean(
                            stock_slice[down_valid, position] - industry_slice[down_valid])
                history_start = max(0, day - self.config.breakout_order_lookback + 1)
                breakout_history = breakout[history_start:day + 1, columns]
                first_dates = np.full(len(columns), np.nan)
                for position in range(len(columns)):
                    hits = np.flatnonzero(breakout_history[:, position])
                    if len(hits):
                        first_dates[position] = history_start + int(hits[0])
                synchronous = int(np.sum(breakout[day, columns]))
                data = {
                    "relative_return_20d_within_industry": return20[day, columns] - industry_return20,
                    "relative_return_60d_within_industry": return60[day, columns] - industry_return60,
                    "residual_momentum": residual[columns],
                    "ma120_slope": slopes,
                    "distance_to_120d_high": distance,
                    "up_market_capture": up_capture,
                    "down_market_resilience": down_resilience,
                    "amount_share_within_industry": shares,
                    "liquidity_20d_median": liquidity[day, columns],
                    "first_breakout_day_index": first_dates,
                }
                ranks = {
                    "relative_return_20d_rank_within_industry": _rank(data["relative_return_20d_within_industry"]),
                    "relative_return_60d_rank_within_industry": _rank(data["relative_return_60d_within_industry"]),
                    "residual_momentum_rank_within_industry": _rank(data["residual_momentum"]),
                    "ma120_slope_rank_within_industry": _rank(data["ma120_slope"]),
                    "distance_to_120d_high_rank_within_industry": _rank(data["distance_to_120d_high"]),
                    "up_market_capture_rank_within_industry": _rank(data["up_market_capture"]),
                    "down_market_resilience_rank_within_industry": _rank(data["down_market_resilience"]),
                    "amount_share_rank_within_industry": _rank(data["amount_share_within_industry"]),
                    "liquidity_rank_within_industry": _rank(data["liquidity_20d_median"]),
                    "breakout_order_rank_within_industry": _rank(-data["first_breakout_day_index"]),
                }
                coverage = np.mean(np.column_stack([
                    np.isfinite(value) for value in data.values()
                ]), axis=1)
                for position, column in enumerate(columns):
                    row = {
                        "trade_date": date,
                        "symbol": self.panel.symbols[column],
                        "industry_id": industry,
                        "industry_name": str(labels.get(industry, industry)),
                        "synchronous_breakout_count": synchronous,
                        "is_breakout_today": bool(breakout[day, column]),
                        "coverage_ratio": float(coverage[position]),
                        "available_at": _available_at(date),
                        "feature_version": self.config.feature_version,
                        "config_sha256": self.config.sha256,
                    }
                    row.update({name: (float(value[position])
                                      if np.isfinite(value[position]) else np.nan)
                                for name, value in data.items()})
                    row.update({name: (float(value[position])
                                      if np.isfinite(value[position]) else np.nan)
                                for name, value in ranks.items()})
                    rows.append(row)
        return pd.DataFrame(rows).sort_values(
            ["trade_date", "industry_id", "symbol"]
        ).reset_index(drop=True) if rows else pd.DataFrame()


__all__ = ["IndustryStrengthBuilder", "IndustryTrendLeaderBuilder"]
