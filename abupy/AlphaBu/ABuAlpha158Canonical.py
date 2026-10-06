# -*- encoding: utf-8 -*-
"""PIT-safe Alpha158 canonical-style technical feature families.

The frozen Alpha158-lite model remains unchanged.  This module is research
only: it adds one pre-registered family at a time and uses the same-day
cross-sectional rank convention as :mod:`ABuAlpha158Lite`.
"""
from __future__ import annotations

import numpy as np

from .ABuAlpha158Lite import Alpha158LiteFeatureEngine, _rank, _safe_divide


CANONICAL_WINDOWS = (5, 10, 20, 30, 60)
KBAR_FEATURES = (
    "KMID", "KLEN", "KMID2", "KUP", "KUP2",
    "KLOW", "KLOW2", "KSFT", "KSFT2",
)
VWAP_FEATURES = ("VWAP0",)


def _names(prefixes):
    return tuple("{}{}".format(prefix, window)
                 for prefix in prefixes for window in CANONICAL_WINDOWS)


ALPHA158_CANONICAL_FAMILIES = {
    "regression_trend": _names(("BETA", "RSQR", "RESI")),
    "price_position": _names(
        ("QTLU", "QTLD", "RANK", "RSV", "IMAX", "IMIN", "IMXD")),
    "volume_structure": _names(
        ("VMA", "VSTD", "WVMA", "VSUMP", "VSUMN", "VSUMD")),
    "price_volume_persistence": _names(
        ("CORR", "CORD", "CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD")),
    "kbar_shape": KBAR_FEATURES,
    "vwap_price": VWAP_FEATURES,
}


def _minimum_count(window):
    return max(3, int(np.ceil(.8 * window)))


def _valid_column_count(values):
    return np.isfinite(values).sum(axis=0)


def _window_std(values, minimum):
    values = np.asarray(values, dtype=float)
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    mean = _safe_divide(np.where(valid, values, 0).sum(axis=0), count)
    centered = np.where(valid, values - mean, 0)
    result = np.sqrt(_safe_divide((centered ** 2).sum(axis=0), count - 1))
    result[count < minimum] = np.nan
    return result


def _window_corr(left, right, minimum):
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    valid = np.isfinite(left) & np.isfinite(right)
    count = valid.sum(axis=0)
    left_mean = _safe_divide(np.where(valid, left, 0).sum(axis=0), count)
    right_mean = _safe_divide(np.where(valid, right, 0).sum(axis=0), count)
    left_centered = np.where(valid, left - left_mean, 0)
    right_centered = np.where(valid, right - right_mean, 0)
    scale = np.sqrt((left_centered ** 2).sum(axis=0) *
                    (right_centered ** 2).sum(axis=0))
    result = _safe_divide(
        (left_centered * right_centered).sum(axis=0), scale)
    result[(count < minimum) | np.isclose(scale, 0)] = np.nan
    return result


def _rolling_ols(values, minimum):
    """Return slope, R-squared and last residual for each security."""
    values = np.asarray(values, dtype=float)
    x = np.arange(1, len(values) + 1, dtype=float)[:, None]
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    x_mean = _safe_divide(np.where(valid, x, 0).sum(axis=0), count)
    y_mean = _safe_divide(np.where(valid, values, 0).sum(axis=0), count)
    x_centered = np.where(valid, x - x_mean, 0)
    y_centered = np.where(valid, values - y_mean, 0)
    xx = (x_centered ** 2).sum(axis=0)
    yy = (y_centered ** 2).sum(axis=0)
    xy = (x_centered * y_centered).sum(axis=0)
    slope = _safe_divide(xy, xx)
    rsquare = _safe_divide(xy ** 2, xx * yy)
    intercept = y_mean - slope * x_mean
    residual = values[-1] - (intercept + slope * float(len(values)))
    invalid = (count < minimum) | np.isclose(xx, 0)
    slope[invalid] = np.nan
    residual[invalid | ~np.isfinite(values[-1])] = np.nan
    rsquare[invalid | np.isclose(yy, 0)] = np.nan
    return slope, rsquare, residual


def _time_rank(window, minimum):
    values = np.asarray(window, dtype=float)
    current = values[-1]
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    less = (valid & (values < current[None, :])).sum(axis=0)
    equal = (valid & (values == current[None, :])).sum(axis=0)
    result = _safe_divide(less + (equal + 1) / 2.0, count)
    result[(count < minimum) | ~np.isfinite(current)] = np.nan
    return result


def _extreme_index(window, mode, minimum):
    """Qlib-compatible 1-based position, with NaNs handled fail-closed."""
    values = np.asarray(window, dtype=float)
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    replacement = -np.inf if mode == "max" else np.inf
    safe = np.where(valid, values, replacement)
    index = (np.argmax(safe, axis=0) if mode == "max"
             else np.argmin(safe, axis=0)).astype(float) + 1.0
    index[count < minimum] = np.nan
    return index


def _positive_negative_ratio(changes, minimum):
    valid = np.isfinite(changes)
    count = valid.sum(axis=0)
    positive = np.where(valid, np.maximum(changes, 0), 0).sum(axis=0)
    negative = np.where(valid, np.maximum(-changes, 0), 0).sum(axis=0)
    total = positive + negative
    pos_ratio = _safe_divide(positive, total)
    neg_ratio = _safe_divide(negative, total)
    diff_ratio = _safe_divide(positive - negative, total)
    invalid = (count < minimum) | np.isclose(total, 0)
    pos_ratio[invalid] = np.nan
    neg_ratio[invalid] = np.nan
    diff_ratio[invalid] = np.nan
    return pos_ratio, neg_ratio, diff_ratio


class Alpha158CanonicalFeatureEngine(object):
    """Append one canonical-style feature family to the frozen base frame."""

    def __init__(self, panel, source, family):
        if family not in ALPHA158_CANONICAL_FAMILIES:
            raise ValueError("unknown Alpha158 feature family: {}".format(family))
        self.panel = panel
        self.source = source
        self.family = family
        self.feature_names = ALPHA158_CANONICAL_FAMILIES[family]
        self.base = Alpha158LiteFeatureEngine(panel, source)

    def _regression_trend(self, day):
        output = {}
        current = np.asarray(self.panel.close[day], dtype=float)
        for window in CANONICAL_WINDOWS:
            values = self.panel.close[day-window+1:day+1]
            slope, rsquare, residual = _rolling_ols(
                values, _minimum_count(window))
            output["BETA{}".format(window)] = _safe_divide(slope, current)
            output["RSQR{}".format(window)] = rsquare
            output["RESI{}".format(window)] = _safe_divide(residual, current)
        return output

    def _price_position(self, day):
        output = {}
        current = np.asarray(self.panel.close[day], dtype=float)
        for window in CANONICAL_WINDOWS:
            close = np.asarray(
                self.panel.close[day-window+1:day+1], dtype=float)
            high = np.asarray(
                self.panel.high[day-window+1:day+1], dtype=float)
            low = np.asarray(
                self.panel.low[day-window+1:day+1], dtype=float)
            minimum = _minimum_count(window)
            close_count = _valid_column_count(close)
            high_count = _valid_column_count(high)
            low_count = _valid_column_count(low)
            q80 = np.nanquantile(close, .8, axis=0)
            q20 = np.nanquantile(close, .2, axis=0)
            low_value = np.nanmin(low, axis=0)
            high_value = np.nanmax(high, axis=0)
            q80[close_count < minimum] = np.nan
            q20[close_count < minimum] = np.nan
            low_value[low_count < minimum] = np.nan
            high_value[high_count < minimum] = np.nan
            imax = _extreme_index(high, "max", minimum)
            imin = _extreme_index(low, "min", minimum)
            output["QTLU{}".format(window)] = _safe_divide(q80, current)
            output["QTLD{}".format(window)] = _safe_divide(q20, current)
            output["RANK{}".format(window)] = _time_rank(close, minimum)
            output["RSV{}".format(window)] = _safe_divide(
                current - low_value, high_value - low_value)
            output["IMAX{}".format(window)] = imax / float(window)
            output["IMIN{}".format(window)] = imin / float(window)
            output["IMXD{}".format(window)] = (imax - imin) / float(window)
        return output

    def _volume_structure(self, day):
        output = {}
        volume = self.panel.volume
        for window in CANONICAL_WINDOWS:
            values = np.asarray(volume[day-window+1:day+1], dtype=float)
            minimum = _minimum_count(window)
            count = _valid_column_count(values)
            mean = np.nanmean(values, axis=0)
            mean[count < minimum] = np.nan
            std = _window_std(values, minimum)
            current = values[-1]
            returns = np.abs(np.asarray(
                self.panel.returns[day-window+1:day+1], dtype=float))
            weighted = returns * values
            weighted_mean = np.nanmean(weighted, axis=0)
            weighted_mean[_valid_column_count(weighted) < minimum] = np.nan
            weighted_std = _window_std(weighted, minimum)
            changes = np.diff(np.asarray(
                volume[day-window:day+1], dtype=float), axis=0)
            positive, negative, difference = _positive_negative_ratio(
                changes, minimum)
            output["VMA{}".format(window)] = _safe_divide(mean, current)
            output["VSTD{}".format(window)] = _safe_divide(std, current)
            output["WVMA{}".format(window)] = _safe_divide(
                weighted_std, weighted_mean)
            output["VSUMP{}".format(window)] = positive
            output["VSUMN{}".format(window)] = negative
            output["VSUMD{}".format(window)] = difference
        return output

    def _price_volume_persistence(self, day):
        output = {}
        for window in CANONICAL_WINDOWS:
            close = np.asarray(
                self.panel.close[day-window+1:day+1], dtype=float)
            volume = np.asarray(
                self.panel.volume[day-window+1:day+1], dtype=float)
            minimum = _minimum_count(window)
            log_volume = np.log(np.where(volume >= 0, volume + 1, np.nan))
            close_ratio = _safe_divide(
                self.panel.close[day-window+1:day+1],
                self.panel.close[day-window:day])
            volume_ratio = _safe_divide(
                self.panel.volume[day-window+1:day+1],
                self.panel.volume[day-window:day])
            log_volume_ratio = np.log(np.where(
                np.isfinite(volume_ratio) & (volume_ratio >= 0),
                volume_ratio + 1, np.nan))
            changes = np.asarray(
                self.panel.close[day-window+1:day+1], dtype=float) - \
                np.asarray(self.panel.close[day-window:day], dtype=float)
            valid = np.isfinite(changes)
            count = valid.sum(axis=0)
            up = _safe_divide((valid & (changes > 0)).sum(axis=0), count)
            down = _safe_divide((valid & (changes < 0)).sum(axis=0), count)
            invalid = count < minimum
            up[invalid] = np.nan
            down[invalid] = np.nan
            positive, negative, difference = _positive_negative_ratio(
                changes, minimum)
            output["CORR{}".format(window)] = _window_corr(
                close, log_volume, minimum)
            output["CORD{}".format(window)] = _window_corr(
                close_ratio, log_volume_ratio, minimum)
            output["CNTP{}".format(window)] = up
            output["CNTN{}".format(window)] = down
            output["CNTD{}".format(window)] = up - down
            output["SUMP{}".format(window)] = positive
            output["SUMN{}".format(window)] = negative
            output["SUMD{}".format(window)] = difference
        return output

    def _kbar_shape(self, day):
        """Return the nine canonical single-session candlestick features.

        All inputs are from the adjusted signal-price space.  The family does
        not consume amount or raw execution prices, so corporate-action scale
        changes cannot leak into the shapes.
        """
        open_price = np.asarray(self.panel.open[day], dtype=float)
        high = np.asarray(self.panel.high[day], dtype=float)
        low = np.asarray(self.panel.low[day], dtype=float)
        close = np.asarray(self.panel.close[day], dtype=float)
        price_range = high - low
        upper_body = np.maximum(open_price, close)
        lower_body = np.minimum(open_price, close)
        middle = close - open_price
        upper_shadow = high - upper_body
        lower_shadow = lower_body - low
        shift = 2.0 * close - high - low
        return {
            "KMID": _safe_divide(middle, open_price),
            "KLEN": _safe_divide(price_range, open_price),
            "KMID2": _safe_divide(middle, price_range),
            "KUP": _safe_divide(upper_shadow, open_price),
            "KUP2": _safe_divide(upper_shadow, price_range),
            "KLOW": _safe_divide(lower_shadow, open_price),
            "KLOW2": _safe_divide(lower_shadow, price_range),
            "KSFT": _safe_divide(shift, open_price),
            "KSFT2": _safe_divide(shift, price_range),
        }

    def _vwap_price(self, day):
        """Return same-session VWAP in adjusted signal-price scale.

        Amount and volume describe raw execution prices.  Mapping by the
        same-day close adjustment ratio makes the scale explicit; the final
        VWAP-to-close ratio is algebraically identical to raw VWAP/raw close.
        Missing amount remains NaN and therefore fails closed in ranking.
        """
        raw_vwap = _safe_divide(
            np.asarray(self.panel.amount[day], dtype=float),
            np.asarray(self.panel.exec_volume[day], dtype=float))
        adjustment = _safe_divide(
            np.asarray(self.panel.close[day], dtype=float),
            np.asarray(self.panel.exec_close[day], dtype=float))
        adjusted_vwap = raw_vwap * adjustment
        return {"VWAP0": _safe_divide(
            adjusted_vwap, np.asarray(self.panel.close[day], dtype=float))}

    def raw_family(self, day):
        return getattr(self, "_" + self.family)(day)

    def snapshot(self, day, include_labels=True):
        frame = self.base.snapshot(day, include_labels=include_labels)
        if frame.empty:
            return frame
        columns = frame.column.to_numpy(dtype=int)
        raw = self.raw_family(day)
        for name in self.feature_names:
            frame[name] = _rank(np.asarray(raw[name])[columns])
        return frame
