# -*- encoding: utf-8 -*-
"""PIT Alpha158-lite features, labels, ranking model and position exits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuTradeIntent import TradeIntent, make_record_id


ALPHA158_LITE_FEATURES = (
    "return_1d", "return_5d", "return_10d", "return_20d",
    "return_60d", "return_120d", "ma10_bias", "ma60_bias",
    "ma120_bias", "high_60_nearness", "high_252_nearness",
    "realized_vol_20d", "realized_vol_60d", "downside_vol_20d",
    "max_drawdown_60d", "atr_fraction", "amount_ratio_20d",
    "amount_cv_20d", "price_amount_corr_20d", "amihud_20d",
    "intraday_return", "close_location", "range_fraction",
    "industry_return_20d", "industry_breadth_ma60",
    "stock_excess_industry_20d",
)


@dataclass(frozen=True)
class Alpha158LiteConfig:
    strategy_version: str = "alpha158_lite_v1"
    feature_version: str = "alpha158_lite_features_v1"
    label_version: str = "alpha158_lite_excess20_v1"
    minimum_history_sessions: int = 252
    minimum_median_amount_20d: float = 20_000_000.0
    unknown_st_policy: str = "exclude"
    label_horizon_sessions: int = 20
    minimum_train_dates: int = 252
    validation_dates: int = 63
    test_dates: int = 63
    training_date_stride: int = 5
    ridge_alpha: float = 100.0
    entry_top_k: int = 10
    retention_top_k: int = 20
    portfolio_score_depth: int = 50
    atr_stop_multiple: float = 2.0
    trailing_atr_multiple: float = 3.0
    trailing_activation_r: float = 1.0
    stagnation_sessions: int = 20
    stagnation_mfe_r: float = 0.5
    max_gap_atr: float = 1.0

    def __post_init__(self):
        positive = (
            "minimum_history_sessions", "minimum_median_amount_20d",
            "label_horizon_sessions", "minimum_train_dates",
            "validation_dates", "test_dates", "training_date_stride",
            "ridge_alpha", "entry_top_k", "retention_top_k",
            "portfolio_score_depth", "atr_stop_multiple",
            "trailing_atr_multiple", "trailing_activation_r",
            "stagnation_sessions", "stagnation_mfe_r", "max_gap_atr",
        )
        if any(getattr(self, name) <= 0 for name in positive):
            raise ValueError("alpha158-lite numeric settings must be positive")
        if self.unknown_st_policy not in ("exclude", "include"):
            raise ValueError("unknown_st_policy must be exclude or include")
        if self.retention_top_k < self.entry_top_k:
            raise ValueError("retention_top_k must be >= entry_top_k")
        if self.portfolio_score_depth < self.retention_top_k:
            raise ValueError("portfolio_score_depth must cover retention_top_k")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def load_alpha158_lite_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(Alpha158LiteConfig)}
    if set(payload) != expected:
        raise ValueError("Alpha158LiteConfig fields mismatch")
    return Alpha158LiteConfig(**payload)


def _safe_divide(numerator, denominator):
    numerator = np.asarray(numerator, dtype=float)
    denominator = np.asarray(denominator, dtype=float)
    result = np.full(np.broadcast_shapes(numerator.shape, denominator.shape),
                     np.nan, dtype=float)
    np.divide(numerator, denominator, out=result,
              where=np.isfinite(numerator) & np.isfinite(denominator) &
              (denominator != 0))
    return result


def _rank(values):
    """Deterministic centered percentile rank with average ranks for ties."""
    values = np.asarray(values, dtype=float)
    result = np.full(values.shape, np.nan, dtype=np.float32)
    valid = np.isfinite(values)
    if not valid.any():
        return result
    ranked = pd.Series(values[valid]).rank(method="average", pct=True)
    result[valid] = ranked.to_numpy(dtype=np.float32) - 0.5
    return result


def _column_volatility(values, minimum):
    values = np.asarray(values, dtype=float)
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    mean = _safe_divide(np.where(valid, values, 0).sum(axis=0), count)
    squared = np.where(valid, (values-mean)**2, 0).sum(axis=0)
    result = np.sqrt(_safe_divide(squared, count-1))*np.sqrt(252)
    result[count < minimum] = np.nan
    return result


def _column_std(values, minimum=2):
    values = np.asarray(values, dtype=float)
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    mean = _safe_divide(np.where(valid, values, 0).sum(axis=0), count)
    squared = np.where(valid, (values-mean)**2, 0).sum(axis=0)
    result = np.sqrt(_safe_divide(squared, count-1))
    result[count < minimum] = np.nan
    return result


def _column_correlation(left, right, minimum):
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    valid = np.isfinite(left) & np.isfinite(right)
    count = valid.sum(axis=0)
    left_mean = _safe_divide(np.where(valid, left, 0).sum(axis=0), count)
    right_mean = _safe_divide(np.where(valid, right, 0).sum(axis=0), count)
    left_centered = np.where(valid, left-left_mean, 0)
    right_centered = np.where(valid, right-right_mean, 0)
    covariance = (left_centered*right_centered).sum(axis=0)
    scale = np.sqrt((left_centered**2).sum(axis=0) *
                    (right_centered**2).sum(axis=0))
    result = _safe_divide(covariance, scale)
    result[count < minimum] = np.nan
    return result


class Alpha158LiteFeatureEngine(object):
    """Generate cross-sectional snapshots using data available at close t."""

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or Alpha158LiteConfig()
        self._base_eligible = panel.signal_eligible(
            min_history=self.config.minimum_history_sessions,
            required_fields=("amount",),
            unknown_st_policy=self.config.unknown_st_policy,
        )
        self.feature_config_sha256 = hashlib.sha256(json.dumps({
            "version": self.config.feature_version,
            "features": ALPHA158_LITE_FEATURES,
            "normalization": "same_day_centered_percentile_rank",
            "asof": "signal_close_inclusive_future_exclusive",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.label_config_sha256 = hashlib.sha256(json.dumps({
            "version": self.config.label_version,
            "entry": "t_plus_1_adjusted_open_if_executable_and_gap_allowed",
            "exit": "t_plus_20_adjusted_close",
            "benchmark": "same_period_csi300",
            "training_target": "same_day_centered_percentile_rank",
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def eligible(self, day):
        if day < 19:
            return np.zeros(len(self.panel.symbols), dtype=bool)
        amount = np.asarray(self.panel.amount[day-19:day+1], dtype=float)
        median = np.nanmedian(amount, axis=0)
        return (self._base_eligible[day] & np.isfinite(median) &
                (median >= self.config.minimum_median_amount_20d) &
                np.isfinite(self.panel.atr21[day]) &
                (self.panel.atr21[day] > 0))

    def _industry_features(self, day, stock_return20):
        size = len(self.panel.symbols)
        industry_return = np.full(size, np.nan, dtype=float)
        industry_breadth = np.full(size, np.nan, dtype=float)
        if day < 20:
            return industry_return, industry_breadth
        denominator = self.panel.breadth_denominator(
            min_history=120,
            unknown_st_policy=self.config.unknown_st_policy)[day]
        industries = np.asarray(self.panel.industry[day], dtype=int)
        for industry in np.unique(industries[denominator & (industries >= 0)]):
            members = denominator & (industries == industry)
            returns = stock_return20[members]
            valid_returns = returns[np.isfinite(returns)]
            if len(valid_returns):
                industry_return[members] = float(np.mean(valid_returns))
            valid_ma = members & np.isfinite(self.panel.ma60[day]) & \
                np.isfinite(self.panel.close[day])
            if valid_ma.any():
                value = float(np.mean(
                    self.panel.close[day, valid_ma] >
                    self.panel.ma60[day, valid_ma]))
                industry_breadth[members] = value
        return industry_return, industry_breadth

    def raw_features(self, day):
        if day < self.config.minimum_history_sessions:
            raise ValueError("day does not have required feature history")
        # Keep the panel's float32 backing arrays; copying all symbols to
        # float64 for every signal date would dominate both memory and time.
        close = self.panel.close
        opening = self.panel.open
        high = self.panel.high
        low = self.panel.low
        returns = self.panel.returns
        amount = self.panel.amount
        current = close[day]

        def endpoint(sessions):
            return _safe_divide(current, close[day-sessions])-1

        ret20_window = returns[day-19:day+1]
        ret60_window = returns[day-59:day+1]
        amount20 = amount[day-19:day+1]
        amount_median = np.nanmedian(amount20, axis=0)
        amount_mean = np.nanmean(amount20, axis=0)
        amount_cv = _safe_divide(_column_std(amount20), amount_mean)
        amount_change = np.diff(np.log(np.where(
            amount[day-20:day+1] > 0,
            amount[day-20:day+1], np.nan)), axis=0)
        price_amount_corr = _column_correlation(
            ret20_window, amount_change, minimum=16)
        valid_amihud = np.isfinite(ret20_window) & np.isfinite(amount20) & \
            (amount20 > 0)
        amihud_count = valid_amihud.sum(axis=0)
        amihud = _safe_divide(np.where(
            valid_amihud, np.abs(ret20_window)/amount20, 0
        ).sum(axis=0), amihud_count) * 1e9
        amihud[amihud_count < 16] = np.nan
        downside = np.where(ret20_window < 0, ret20_window, np.nan)
        downside_count = np.isfinite(downside).sum(axis=0)
        downside_vol = _column_std(downside)*np.sqrt(252)
        downside_vol[downside_count < 2] = np.nan
        close60 = close[day-59:day+1]
        valid_close60 = np.isfinite(close60) & (close60 > 0)
        running = np.maximum.accumulate(
            np.where(valid_close60, close60, -np.inf), axis=0)
        drawdown = np.where(valid_close60 & np.isfinite(running),
                            close60/running-1, np.nan)
        max_drawdown = np.min(np.where(
            np.isfinite(drawdown), drawdown, np.inf), axis=0)
        max_drawdown[valid_close60.sum(axis=0) < 48] = np.nan
        high60_values = high[day-59:day+1]
        high252_values = high[day-251:day+1]
        high60 = np.max(np.where(
            np.isfinite(high60_values), high60_values, -np.inf), axis=0)
        high252 = np.max(np.where(
            np.isfinite(high252_values), high252_values, -np.inf), axis=0)
        high60[~np.isfinite(high60)] = np.nan
        high252[~np.isfinite(high252)] = np.nan
        day_range = high[day]-low[day]
        stock_return20 = endpoint(20)
        industry_return, industry_breadth = self._industry_features(
            day, stock_return20)
        values = (
            endpoint(1), endpoint(5), endpoint(10), stock_return20,
            endpoint(60), endpoint(120),
            _safe_divide(current, self.panel.ma10[day])-1,
            _safe_divide(current, self.panel.ma60[day])-1,
            _safe_divide(current, self.panel.ma120[day])-1,
            _safe_divide(current, high60), _safe_divide(current, high252),
            _column_volatility(ret20_window, 16),
            _column_volatility(ret60_window, 48), downside_vol,
            max_drawdown, _safe_divide(self.panel.atr21[day], current),
            _safe_divide(amount[day], amount_median), amount_cv,
            price_amount_corr, amihud,
            _safe_divide(current, opening[day])-1,
            _safe_divide(current-low[day], day_range),
            _safe_divide(day_range, close[day-1]), industry_return,
            industry_breadth, stock_return20-industry_return,
        )
        return np.column_stack(values).astype(np.float32)

    def _labels(self, day, columns):
        count = len(columns)
        target = np.full(count, np.nan, dtype=float)
        horizon = self.config.label_horizon_sessions
        entry_day, exit_day = day+1, day+horizon
        if exit_day >= len(self.panel.dates):
            return target, np.full(count, np.nan, dtype=np.float32)
        factor = _safe_divide(
            self.panel.exec_close[day, columns],
            self.panel.close[day, columns])
        max_buy_raw = self.panel.exec_close[day, columns] + \
            self.config.max_gap_atr*self.panel.atr21[day, columns]*factor
        raw_open = np.asarray(self.panel.exec_open[entry_day, columns], float)
        adjusted_open = np.asarray(self.panel.open[entry_day, columns], float)
        adjusted_exit = np.asarray(self.panel.close[exit_day, columns], float)
        executable = (
            self.panel.buy_tradable_mask[entry_day, columns] &
            np.isfinite(raw_open) & (raw_open > 0) &
            np.isfinite(adjusted_open) & (adjusted_open > 0) &
            np.isfinite(adjusted_exit) & (adjusted_exit > 0) &
            np.isfinite(max_buy_raw) & (raw_open <= max_buy_raw)
        )
        benchmark_entry = float(self.panel.benchmark_open[entry_day])
        benchmark_exit = float(self.panel.benchmark_close[exit_day])
        if np.isfinite(benchmark_entry) and benchmark_entry > 0 and \
                np.isfinite(benchmark_exit):
            benchmark_return = benchmark_exit/benchmark_entry-1
            target[executable] = (
                adjusted_exit[executable]/adjusted_open[executable]-1-
                benchmark_return)
        return target, _rank(target)

    def snapshot(self, day, include_labels=True):
        eligible = self.eligible(day)
        columns = np.flatnonzero(eligible)
        base_columns = [
            "signal_asof", "symbol", "column", "feature_version",
            "feature_config_sha256", *ALPHA158_LITE_FEATURES,
            "baseline_score", "excess_return_20d", "target_rank",
            "label_version", "label_config_sha256",
        ]
        if not len(columns):
            return pd.DataFrame(columns=base_columns)
        raw = self.raw_features(day)[columns]
        ranked = np.column_stack([_rank(raw[:, index])
                                  for index in range(raw.shape[1])])
        frame = pd.DataFrame(ranked, columns=ALPHA158_LITE_FEATURES)
        frame.insert(0, "feature_config_sha256", self.feature_config_sha256)
        frame.insert(0, "feature_version", self.config.feature_version)
        frame.insert(0, "column", columns)
        frame.insert(0, "symbol", [self.panel.symbols[item] for item in columns])
        frame.insert(0, "signal_asof", int(self.panel.dates[day]))
        frame["baseline_score"] = (
            .30*frame.return_60d + .25*frame.return_20d +
            .20*frame.high_252_nearness + .10*frame.ma120_bias -
            .15*frame.realized_vol_20d)
        if include_labels:
            target, target_rank = self._labels(day, columns)
        else:
            target = np.full(len(columns), np.nan)
            target_rank = np.full(len(columns), np.nan)
        frame["excess_return_20d"] = target
        frame["target_rank"] = target_rank
        frame["label_version"] = self.config.label_version
        frame["label_config_sha256"] = self.label_config_sha256
        return frame[base_columns]

    def make_intent(self, day, column, score):
        adjusted = float(self.panel.close[day, column])
        raw = float(self.panel.exec_close[day, column])
        atr = float(self.panel.atr21[day, column])
        factor = raw/adjusted
        stop_adjusted = adjusted-self.config.atr_stop_multiple*atr
        if not np.isfinite(stop_adjusted) or stop_adjusted <= 0:
            return None
        max_buy_raw = raw+self.config.max_gap_atr*atr*factor
        symbol = self.panel.symbols[column]
        return TradeIntent(
            intent_id=make_record_id(
                self.config.strategy_version, int(self.panel.dates[day]), symbol),
            strategy_id=self.config.strategy_version, strategy_version="1",
            signal_asof=int(self.panel.dates[day]), symbol=symbol,
            score=float(score), signal_price_adjusted=adjusted,
            signal_price_raw=raw, adjustment_factor_signal=factor,
            initial_stop_adjusted=stop_adjusted,
            initial_stop_raw=stop_adjusted*factor,
            industry_asof=int(self.panel.industry[day, column]),
            required_fields=("amount",), max_gap_atr=self.config.max_gap_atr,
            r_definition_version="alpha158_lite_2atr_v1",
            metadata={
                "max_buy_price_raw": float(max_buy_raw),
                "feature_version": self.config.feature_version,
                "rank_exit_buffer": self.config.retention_top_k,
            },
        )


class Alpha158LiteModel(object):
    """Frozen linear cross-sectional rank model."""

    def __init__(self, config=None):
        self.config = config or Alpha158LiteConfig()
        self.pipeline = None
        self.manifest = None

    def fit(self, frame, train_dates):
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import Pipeline

        rows = frame.dropna(subset=["target_rank"])
        if len(rows) < 1000:
            raise ValueError("insufficient alpha158-lite training rows")
        self.pipeline = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", Ridge(alpha=self.config.ridge_alpha)),
        ])
        self.pipeline.fit(rows[list(ALPHA158_LITE_FEATURES)], rows.target_rank)
        self.manifest = {
            "strategy_version": self.config.strategy_version,
            "config_sha256": self.config.sha256,
            "features": list(ALPHA158_LITE_FEATURES),
            "train_rows": int(len(rows)),
            "train_dates": int(len(set(int(item) for item in train_dates))),
            "train_start": int(min(train_dates)),
            "train_end": int(max(train_dates)),
            "model": "median_imputer_plus_ridge",
        }
        return self

    def predict(self, frame):
        if self.pipeline is None:
            raise ValueError("model is not fitted")
        return self.pipeline.predict(frame[list(ALPHA158_LITE_FEATURES)])


@dataclass
class Alpha158LitePositionState:
    symbol: str
    entry_day: int
    initial_stop_adjusted: float
    initial_r_adjusted: float
    peak_close_adjusted: float
    current_stop_adjusted: float
    trailing_enabled: bool = False


class Alpha158LiteExitEngine(object):
    PRIORITY = ("INITIAL_STOP", "TRAILING_STOP", "STAGNATION")

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or Alpha158LiteConfig()
        self.states = {}

    def register_entry(self, intent, fill, day):
        adjusted_entry = fill.fill_price_raw/intent.adjustment_factor_signal
        initial_r = adjusted_entry-float(intent.initial_stop_adjusted)
        self.states[intent.symbol] = Alpha158LitePositionState(
            symbol=intent.symbol, entry_day=int(day),
            initial_stop_adjusted=float(intent.initial_stop_adjusted),
            initial_r_adjusted=float(initial_r),
            peak_close_adjusted=float(adjusted_entry),
            current_stop_adjusted=float(intent.initial_stop_adjusted),
        )

    def signal(self, day, symbol):
        state = self.states[symbol]
        column = self.panel.symbol_index[symbol]
        close = float(self.panel.close[day, column])
        if not np.isfinite(close):
            return None
        state.peak_close_adjusted = max(state.peak_close_adjusted, close)
        mfe = state.peak_close_adjusted - (
            state.initial_stop_adjusted+state.initial_r_adjusted)
        if (state.initial_r_adjusted > 0 and
                mfe >= self.config.trailing_activation_r*
                state.initial_r_adjusted):
            state.trailing_enabled = True
        atr = float(self.panel.atr21[day, column])
        if state.trailing_enabled and np.isfinite(atr):
            state.current_stop_adjusted = max(
                state.current_stop_adjusted,
                state.peak_close_adjusted-self.config.trailing_atr_multiple*atr)
        held = int(day)-state.entry_day+1
        if close <= state.initial_stop_adjusted:
            return "INITIAL_STOP"
        if state.trailing_enabled and close <= state.current_stop_adjusted:
            return "TRAILING_STOP"
        if (held >= self.config.stagnation_sessions and
                mfe < self.config.stagnation_mfe_r*state.initial_r_adjusted):
            return "STAGNATION"
        return None

    def remove(self, symbol):
        self.states.pop(symbol, None)
