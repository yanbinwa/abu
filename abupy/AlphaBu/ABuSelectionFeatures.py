# -*- encoding: utf-8 -*-
"""Point-in-time feature snapshots for cross-sectional stock selection."""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import asdict

import numpy as np
import pandas as pd


VCP_QUALITY_FEATURE_VERSION = "vcp_quality_features_v1"
VCP_QUALITY_FEATURE_VERSION_V2 = "vcp_quality_features_v2"

VCP_QUALITY_FEATURES = (
    "legacy_score", "residual_momentum", "residual_momentum_standardized",
    "contraction_tightness", "breakout_strength", "ma120_slope",
    "k_body", "k_range", "close_location", "upper_shadow",
    "lower_shadow", "gap", "return_5d", "return_10d", "return_20d",
    "return_60d", "return_120d", "high_60_nearness",
    "high_252_nearness", "trend_slope_20d", "trend_r2_20d",
    "trend_slope_60d", "trend_r2_60d", "realized_vol_20d",
    "realized_vol_60d", "downside_vol_20d", "max_drawdown_60d",
    "atr_fraction", "amount_ratio_20d", "amount_cv_20d",
    "turnover_ratio_20d", "turnover_cv_20d", "price_volume_corr_20d",
    "amihud_20d", "market_return_5d", "market_return_20d",
    "market_breadth_ma60", "market_breadth_ma120", "industry_return_5d",
    "industry_return_20d", "industry_return_60d",
    "stock_excess_industry_20d", "industry_breadth_ma60",
)


def _metadata(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = ast.literal_eval(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (SyntaxError, ValueError, TypeError):
        return {}


def _finite_array(values):
    values = np.asarray(values, dtype=float)
    return values[np.isfinite(values)]


def _safe_ratio(numerator, denominator):
    if not np.isfinite(numerator) or not np.isfinite(denominator) or denominator == 0:
        return np.nan
    return float(numerator / denominator)


def _endpoint_return(values, day, sessions):
    if day < sessions:
        return np.nan
    current, previous = float(values[day]), float(values[day-sessions])
    return _safe_ratio(current, previous) - 1 if previous > 0 else np.nan


def _trend(values):
    values = np.asarray(values, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all() or np.any(values <= 0):
        return np.nan, np.nan
    y = np.log(values)
    x = np.arange(len(y), dtype=float)
    centered = x - x.mean()
    slope = float(np.sum(centered * (y-y.mean())) / np.sum(centered**2))
    fitted = y.mean() + slope * centered
    total = float(np.sum((y-y.mean())**2))
    r2 = 1.0 - float(np.sum((y-fitted)**2)) / total if total > 0 else 0.0
    return slope, r2


def _coefficient_of_variation(values):
    values = _finite_array(values)
    if len(values) < 2:
        return np.nan
    mean = float(np.mean(values))
    return float(np.std(values, ddof=1) / mean) if mean > 0 else np.nan


def _max_drawdown(values):
    values = np.asarray(values, dtype=float)
    if not len(values) or not np.isfinite(values).all() or np.any(values <= 0):
        return np.nan
    return float(np.min(values / np.maximum.accumulate(values) - 1))


class VCPFeatureSnapshotBuilder(object):
    """Build immutable features using bars no later than ``signal_asof``."""

    def __init__(self, panel, feature_version=VCP_QUALITY_FEATURE_VERSION):
        self.panel = panel
        self.feature_version = str(feature_version)
        self._date_index = {int(value): position
                            for position, value in enumerate(panel.dates)}
        self._breadth_denominator = panel.breadth_denominator(
            min_history=120, unknown_st_policy="exclude")
        self._industry_cache = {}
        self.config_sha256 = hashlib.sha256(json.dumps({
            "feature_version": self.feature_version,
            "features": VCP_QUALITY_FEATURES,
            "asof_semantics": "signal_close_inclusive_future_exclusive",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _standardized_residual(self, day, column):
        regression_start, regression_end = day - 251, day - 20
        formation_start = regression_end - 105
        if regression_start < 0 or formation_start < 0:
            return np.nan
        stock = np.asarray(
            self.panel.returns[regression_start:regression_end, column], float)
        market = np.asarray(
            self.panel.benchmark_returns[regression_start:regression_end], float)
        valid = np.isfinite(stock) & np.isfinite(market)
        if valid.sum() < 200 or np.var(market[valid]) <= 0:
            return np.nan
        beta = float(np.cov(stock[valid], market[valid], ddof=0)[0, 1] /
                     np.var(market[valid]))
        formation = np.asarray(
            self.panel.returns[formation_start:regression_end, column], float)
        formation_market = np.asarray(
            self.panel.benchmark_returns[formation_start:regression_end], float)
        valid = np.isfinite(formation) & np.isfinite(formation_market)
        if valid.sum() < 95:
            return np.nan
        residual = formation[valid] - beta * formation_market[valid]
        volatility = float(np.std(residual, ddof=1))
        return float(np.mean(residual) / volatility) if volatility > 0 else np.nan

    def _market_features(self, day):
        denominator = self._breadth_denominator[day]
        count = int(denominator.sum())
        breadth60 = (float(((self.panel.close[day] > self.panel.ma60[day]) &
                            denominator).sum()) / count if count else np.nan)
        breadth120 = (float(((self.panel.close[day] > self.panel.ma120[day]) &
                             denominator).sum()) / count if count else np.nan)
        return {
            "market_return_5d": _endpoint_return(
                self.panel.benchmark_close, day, 5),
            "market_return_20d": _endpoint_return(
                self.panel.benchmark_close, day, 20),
            "market_breadth_ma60": breadth60,
            "market_breadth_ma120": breadth120,
        }

    def _industry_features(self, day, industry):
        key = (int(day), int(industry))
        if key in self._industry_cache:
            return self._industry_cache[key]
        output = {"industry_return_5d": np.nan,
                  "industry_return_20d": np.nan,
                  "industry_return_60d": np.nan,
                  "industry_breadth_ma60": np.nan}
        if industry < 0:
            self._industry_cache[key] = output
            return output
        members = ((self.panel.industry[day] == industry) &
                   self._breadth_denominator[day])
        columns = np.flatnonzero(members)
        if not len(columns):
            self._industry_cache[key] = output
            return output
        for sessions in (5, 20, 60):
            if day < sessions:
                continue
            current = np.asarray(self.panel.close[day, columns], float)
            previous = np.asarray(self.panel.close[day-sessions, columns], float)
            valid = np.isfinite(current) & np.isfinite(previous) & (previous > 0)
            if valid.any():
                output["industry_return_{}d".format(sessions)] = float(
                    np.mean(current[valid] / previous[valid] - 1))
        ma_valid = np.isfinite(self.panel.ma60[day, columns])
        if ma_valid.any():
            output["industry_breadth_ma60"] = float(np.mean(
                self.panel.close[day, columns][ma_valid] >
                self.panel.ma60[day, columns][ma_valid]))
        self._industry_cache[key] = output
        return output

    def _row(self, intent):
        day = self._date_index.get(int(intent.signal_asof))
        column = self.panel.symbol_index.get(str(intent.symbol))
        if day is None or column is None:
            return None
        close = np.asarray(self.panel.close[:, column], float)
        opening = np.asarray(self.panel.open[:, column], float)
        high = np.asarray(self.panel.high[:, column], float)
        low = np.asarray(self.panel.low[:, column], float)
        returns = np.asarray(self.panel.returns[:, column], float)
        amount = np.asarray(self.panel.amount[:, column], float)
        turnover = np.asarray(self.panel.turnover[:, column], float)
        metadata = _metadata(intent.metadata)
        previous_close = close[day-1] if day > 0 else np.nan
        day_range = high[day] - low[day]
        scale = previous_close if np.isfinite(previous_close) and previous_close > 0 else np.nan
        trend20 = _trend(close[day-19:day+1]) if day >= 19 else (np.nan, np.nan)
        trend60 = _trend(close[day-59:day+1]) if day >= 59 else (np.nan, np.nan)
        ret20 = returns[day-19:day+1] if day >= 19 else np.array([])
        ret60 = returns[day-59:day+1] if day >= 59 else np.array([])
        negative = ret20[np.isfinite(ret20) & (ret20 < 0)]
        amount20 = amount[day-19:day+1] if day >= 19 else np.array([])
        turnover20 = turnover[day-19:day+1] if day >= 19 else np.array([])
        valid_ret20 = _finite_array(ret20)
        volatility20 = (float(np.std(valid_ret20, ddof=1) * np.sqrt(252))
                        if len(valid_ret20) >= 16 else np.nan)
        valid_ret60 = _finite_array(ret60)
        volatility60 = (float(np.std(valid_ret60, ddof=1) * np.sqrt(252))
                        if len(valid_ret60) >= 48 else np.nan)
        amount_median = np.nanmedian(amount20) if len(amount20) else np.nan
        turnover_median = np.nanmedian(turnover20) if len(turnover20) else np.nan
        price_volume_corr = np.nan
        if day >= 20:
            volume_changes = np.diff(np.log(np.where(
                amount[day-20:day+1] > 0, amount[day-20:day+1], np.nan)))
            valid = np.isfinite(ret20) & np.isfinite(volume_changes)
            if valid.sum() >= 16 and np.std(ret20[valid]) > 0 and \
                    np.std(volume_changes[valid]) > 0:
                price_volume_corr = float(np.corrcoef(
                    ret20[valid], volume_changes[valid])[0, 1])
        amihud = np.nan
        valid = np.isfinite(ret20) & np.isfinite(amount20) & (amount20 > 0)
        if valid.sum() >= 16:
            amihud = float(np.mean(np.abs(ret20[valid]) / amount20[valid]) * 1e9)
        industry = int(self.panel.industry[day, column])
        industry_features = self._industry_features(day, industry)
        stock_return20 = _endpoint_return(close, day, 20)
        ma120_slope = float(metadata.get("ma120_slope", np.nan))
        contraction_tightness = float(
            metadata.get("contraction_tightness", np.nan))
        breakout_strength = float(metadata.get("breakout_strength", np.nan))
        if self.feature_version == VCP_QUALITY_FEATURE_VERSION_V2:
            ma_window = np.asarray(
                self.panel.ma120[day-19:day+1, column], dtype=float)
            ma120_slope = _trend(ma_window)[0]
            contraction_high = high[day-20:day]
            contraction_low = low[day-20:day]
            control_high = high[day-80:day-20]
            control_low = low[day-80:day-20]
            if (len(contraction_high) == 20 and len(control_high) == 60 and
                    np.isfinite(contraction_high).all() and
                    np.isfinite(contraction_low).all() and
                    np.isfinite(control_high).all() and
                    np.isfinite(control_low).all()):
                contraction_range = (np.max(contraction_high) /
                                     np.min(contraction_low) - 1)
                control_range = np.max(control_high) / np.min(control_low) - 1
                if control_range > 0:
                    contraction_tightness = 1-contraction_range/control_range
            breakout = float(metadata.get("breakout_level", np.nan))
            if np.isfinite(breakout) and np.isfinite(self.panel.atr21[day, column]):
                breakout_strength = _safe_ratio(
                    close[day]-breakout, self.panel.atr21[day, column])
        row = {
            "intent_id": intent.intent_id,
            "signal_asof": int(intent.signal_asof),
            "symbol": str(intent.symbol),
            "feature_version": self.feature_version,
            "feature_config_sha256": self.config_sha256,
            "legacy_score": float(intent.score),
            "residual_momentum": float(metadata.get("residual_momentum", np.nan)),
            "residual_momentum_standardized": self._standardized_residual(day, column),
            "contraction_tightness": contraction_tightness,
            "breakout_strength": breakout_strength,
            "ma120_slope": ma120_slope,
            "k_body": _safe_ratio(close[day]-opening[day], opening[day]),
            "k_range": _safe_ratio(day_range, scale),
            "close_location": _safe_ratio(close[day]-low[day], day_range),
            "upper_shadow": _safe_ratio(high[day]-max(opening[day], close[day]), scale),
            "lower_shadow": _safe_ratio(min(opening[day], close[day])-low[day], scale),
            "gap": _safe_ratio(opening[day]-previous_close, previous_close),
            "return_5d": _endpoint_return(close, day, 5),
            "return_10d": _endpoint_return(close, day, 10),
            "return_20d": stock_return20,
            "return_60d": _endpoint_return(close, day, 60),
            "return_120d": _endpoint_return(close, day, 120),
            "high_60_nearness": _safe_ratio(close[day], np.nanmax(
                high[day-59:day+1])) if day >= 59 else np.nan,
            "high_252_nearness": _safe_ratio(close[day], np.nanmax(
                high[day-251:day+1])) if day >= 251 else np.nan,
            "trend_slope_20d": trend20[0], "trend_r2_20d": trend20[1],
            "trend_slope_60d": trend60[0], "trend_r2_60d": trend60[1],
            "realized_vol_20d": volatility20,
            "realized_vol_60d": volatility60,
            "downside_vol_20d": (float(np.std(negative, ddof=1) * np.sqrt(252))
                                  if len(negative) >= 2 else np.nan),
            "max_drawdown_60d": _max_drawdown(close[day-59:day+1])
            if day >= 59 else np.nan,
            "atr_fraction": _safe_ratio(self.panel.atr21[day, column], close[day]),
            "amount_ratio_20d": _safe_ratio(amount[day], amount_median),
            "amount_cv_20d": _coefficient_of_variation(amount20),
            "turnover_ratio_20d": _safe_ratio(turnover[day], turnover_median),
            "turnover_cv_20d": _coefficient_of_variation(turnover20),
            "price_volume_corr_20d": price_volume_corr,
            "amihud_20d": amihud,
            **self._market_features(day),
            **industry_features,
            "stock_excess_industry_20d": (
                stock_return20-industry_features["industry_return_20d"]
                if np.isfinite(stock_return20) and
                np.isfinite(industry_features["industry_return_20d"])
                else np.nan),
        }
        return row

    def build(self, intents):
        rows = [self._row(intent) for intent in intents]
        rows = [row for row in rows if row is not None]
        columns = ["intent_id", "signal_asof", "symbol", "feature_version",
                   "feature_config_sha256", *VCP_QUALITY_FEATURES]
        return pd.DataFrame(rows, columns=columns).sort_values(
            ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
