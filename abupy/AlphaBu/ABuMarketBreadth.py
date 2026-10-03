# -*- encoding: utf-8 -*-
"""Point-in-time market breadth features derived only from frozen daily data."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MarketIndustryContextConfig:
    feature_version: str = "market_industry_context_v1"
    signal_price_space: str = "frozen_qfq"
    market_benchmark: str = "sh000300"
    return_windows: tuple = (5, 20, 60)
    ma_windows: tuple = (20, 60, 120)
    new_high_window: int = 20
    leader_high_window: int = 120
    leader_slope_window: int = 20
    capture_window: int = 60
    minimum_capture_sessions: int = 10
    breakout_window: int = 20
    breakout_order_lookback: int = 20
    amount_short_window: int = 5
    amount_control_window: int = 20
    minimum_industry_members: int = 5
    minimum_member_coverage: float = 0.80
    rank_method: str = "percentile_average_ties"

    def __post_init__(self):
        if self.signal_price_space != "frozen_qfq":
            raise ValueError("signal_price_space must be frozen_qfq")
        if self.market_benchmark != "sh000300":
            raise ValueError("market_benchmark must be sh000300")
        if tuple(self.return_windows) != (5, 20, 60):
            raise ValueError("return_windows must be [5, 20, 60]")
        if tuple(self.ma_windows) != (20, 60, 120):
            raise ValueError("ma_windows must be [20, 60, 120]")
        positive = (
            self.new_high_window, self.leader_high_window,
            self.leader_slope_window, self.capture_window,
            self.minimum_capture_sessions, self.breakout_window,
            self.breakout_order_lookback, self.amount_short_window,
            self.amount_control_window, self.minimum_industry_members,
        )
        if any(int(value) <= 0 for value in positive):
            raise ValueError("context windows and minimum counts must be positive")
        if not 0 < float(self.minimum_member_coverage) <= 1:
            raise ValueError("minimum_member_coverage must be in (0, 1]")
        if self.rank_method != "percentile_average_ties":
            raise ValueError("unsupported rank_method")

    @property
    def sha256(self):
        payload = asdict(self)
        payload["return_windows"] = list(self.return_windows)
        payload["ma_windows"] = list(self.ma_windows)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_market_industry_context_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(MarketIndustryContextConfig)}
    if set(payload) != expected:
        raise ValueError("MarketIndustryContextConfig fields mismatch")
    payload["return_windows"] = tuple(payload["return_windows"])
    payload["ma_windows"] = tuple(payload["ma_windows"])
    return MarketIndustryContextConfig(**payload)


def _rolling_mean(values, window):
    """Full-window rolling mean without filling missing observations."""
    values = np.asarray(values, dtype=np.float64)
    frame = pd.DataFrame(values)
    return frame.rolling(window, min_periods=window).mean().to_numpy()


def _prior_rolling_extreme(values, window, operation):
    """Prior-window extreme that excludes the current session."""
    frame = pd.DataFrame(np.asarray(values, dtype=np.float64))
    rolling = getattr(frame.rolling(window, min_periods=window), operation)()
    result = rolling.shift(1).to_numpy()
    return result


def _safe_ratio(numerator, denominator):
    if denominator <= 0:
        return np.nan
    return float(numerator) / float(denominator)


def _available_at(date):
    value = str(int(date))
    return "{}-{}-{}T15:05:00+08:00".format(value[:4], value[4:6], value[6:])


class MarketBreadthBuilder(object):
    """Build separate full-market and strict signal-universe breadth rows."""

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or MarketIndustryContextConfig()
        self._strict_observed = (
            self.panel.signal_eligible(
                min_history=1, unknown_st_policy="exclude"
            ) & ~self.panel.suspended_mask
        )

    def _scope_masks(self, day):
        universe = self.panel.universe_mask[day]
        observed = (universe & self.panel.signal_price_available[day] &
                    ~self.panel.suspended_mask[day])
        strict_universe = (universe & self.panel.st_status_known[day] &
                           ~self.panel.st_status[day])
        strict_observed = self._strict_observed[day]
        return {
            "full_eligible": (universe, observed),
            "signal_eligible": (strict_universe, strict_observed),
        }

    def build(self, start_date=None, end_date=None):
        close = np.asarray(self.panel.close, dtype=np.float64)
        high = np.asarray(self.panel.high, dtype=np.float64)
        ma = {window: _rolling_mean(close, window)
              for window in self.config.ma_windows}
        prior_high = _prior_rolling_extreme(
            high, self.config.new_high_window, "max")
        prior_low = _prior_rolling_extreme(
            np.asarray(self.panel.low, dtype=np.float64),
            self.config.new_high_window, "min")
        rows = []
        for day, date in enumerate(self.panel.dates):
            date = int(date)
            if start_date is not None and date < int(start_date):
                continue
            if end_date is not None and date > int(end_date):
                continue
            for scope, (eligible, observed) in self._scope_masks(day).items():
                row = {
                    "trade_date": date,
                    "decision_time": "CLOSE",
                    "universe_scope": scope,
                    "eligible_universe_count": int(eligible.sum()),
                    "observed_count": int(observed.sum()),
                    "known_st_count": int((eligible & self.panel.st_status_known[day] &
                                           self.panel.st_status[day]).sum()),
                    "unknown_st_count": int((eligible &
                                             ~self.panel.st_status_known[day]).sum()),
                    "suspended_count": int((eligible &
                                            self.panel.suspended_mask[day]).sum()),
                    "missing_price_count": int((eligible &
                                                ~self.panel.signal_price_available[day]).sum()),
                    "coverage_ratio": _safe_ratio(observed.sum(), eligible.sum()),
                    "available_at": _available_at(date),
                    "feature_version": self.config.feature_version,
                    "config_sha256": self.config.sha256,
                }
                previous_valid = np.zeros(len(self.panel.symbols), dtype=bool)
                if day > 0:
                    previous_valid = np.isfinite(close[day - 1]) & (close[day - 1] > 0)
                    return_mask = observed & previous_valid
                    returns = np.divide(
                        close[day], close[day - 1],
                        out=np.full(close.shape[1], np.nan),
                        where=previous_valid,
                    ) - 1
                else:
                    return_mask = np.zeros(len(self.panel.symbols), dtype=bool)
                    returns = np.full(len(self.panel.symbols), np.nan)
                values = returns[return_mask]
                row.update({
                    "return_denominator": int(return_mask.sum()),
                    "advance_count": int(np.sum(values > 0)),
                    "decline_count": int(np.sum(values < 0)),
                    "unchanged_count": int(np.sum(values == 0)),
                    "positive_return_ratio": _safe_ratio(
                        np.sum(values > 0), len(values)),
                    "cross_section_return_median": (
                        float(np.median(values)) if len(values) else np.nan),
                    "equal_weight_return_1d": (
                        float(np.mean(values)) if len(values) else np.nan),
                })
                for window in self.config.ma_windows:
                    valid = observed & np.isfinite(ma[window][day])
                    row["ma{}_denominator".format(window)] = int(valid.sum())
                    row["breadth_above_ma{}".format(window)] = _safe_ratio(
                        np.sum(close[day, valid] > ma[window][day, valid]),
                        valid.sum(),
                    )
                high_valid = observed & np.isfinite(prior_high[day])
                low_valid = observed & np.isfinite(prior_low[day])
                row["new_high_20d_denominator"] = int(high_valid.sum())
                row["new_low_20d_denominator"] = int(low_valid.sum())
                row["new_high_20d_ratio"] = _safe_ratio(
                    np.sum(close[day, high_valid] > prior_high[day, high_valid]),
                    high_valid.sum(),
                )
                row["new_low_20d_ratio"] = _safe_ratio(
                    np.sum(close[day, low_valid] < prior_low[day, low_valid]),
                    low_valid.sum(),
                )
                window = 5
                if day >= window:
                    endpoint = np.isfinite(close[day - window]) & (close[day - window] > 0)
                    valid = observed & endpoint
                    returns_5d = np.divide(
                        close[day], close[day - window],
                        out=np.full(close.shape[1], np.nan), where=endpoint,
                    ) - 1
                    row["return_5d_denominator"] = int(valid.sum())
                    row["equal_weight_return_5d"] = (
                        float(np.mean(returns_5d[valid])) if valid.any() else np.nan)
                else:
                    row["return_5d_denominator"] = 0
                    row["equal_weight_return_5d"] = np.nan
                rows.append(row)
        return pd.DataFrame(rows)


__all__ = [
    "MarketBreadthBuilder", "MarketIndustryContextConfig",
    "load_market_industry_context_config",
]
