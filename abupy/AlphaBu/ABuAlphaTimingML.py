"""Leakage-aware research overlays for Alpha158 entry and exit timing.

The overlays are deliberately small and conservative.  They do not replace the
primary Alpha158 rank model or any protective exit.  Models are refit only at a
calendar-year boundary from labels that matured strictly before that boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


ENTRY_FEATURES = (
    "alpha_score", "rank_percentile", "return_5d", "return_20d",
    "return_60d", "ma60_bias", "atr_fraction", "amount_ratio_20d",
    "benchmark_return_20d",
)

EXIT_FEATURES = (
    "alpha_score", "return_5d", "return_20d", "return_60d",
    "ma60_bias", "atr_fraction", "benchmark_return_20d",
    "holding_sessions", "unrealized_r", "mfe_r", "peak_drawdown_r",
    "stop_distance_r", "trailing_enabled",
)


def _safe_ratio(numerator, denominator):
    numerator = np.asarray(numerator, dtype=float)
    denominator = np.asarray(denominator, dtype=float)
    out = np.full(np.broadcast(numerator, denominator).shape, np.nan, dtype=float)
    np.divide(numerator, denominator, out=out,
              where=np.isfinite(denominator) & (denominator != 0))
    return out


@dataclass(frozen=True)
class TimingMLConfig:
    entry_horizon_sessions: int = 20
    entry_roundtrip_cost_bps: float = 70.0
    entry_probability_threshold: float = 0.50
    exit_horizon_sessions: int = 5
    exit_probability_threshold: float = 0.35
    exit_persistence_sessions: int = 2
    training_sample_stride: int = 5
    logistic_c: float = 0.10
    minimum_training_rows: int = 200
    minimum_class_rows: int = 20
    random_state: int = 0

    def __post_init__(self):
        if min(self.entry_horizon_sessions, self.exit_horizon_sessions,
               self.exit_persistence_sessions, self.training_sample_stride,
               self.minimum_training_rows, self.minimum_class_rows) <= 0:
            raise ValueError("timing ML integer settings must be positive")
        if not 0 < self.entry_probability_threshold < 1:
            raise ValueError("entry probability threshold must be in (0, 1)")
        if not 0 < self.exit_probability_threshold < 1:
            raise ValueError("exit probability threshold must be in (0, 1)")
        if self.entry_roundtrip_cost_bps < 0 or self.logistic_c <= 0:
            raise ValueError("invalid timing ML cost or regularization")


class YearBoundaryLogisticModel:
    """One fixed logistic specification fitted independently for each year."""

    def __init__(self, feature_names, config=None):
        self.feature_names = tuple(feature_names)
        self.config = config or TimingMLConfig()
        self.models = {}
        self.manifest = []

    def fit(self, frame, years, group_column, skip_insufficient=False):
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        required = {"date", "label_end", "label", group_column,
                    *self.feature_names}
        missing = required-set(frame.columns)
        if missing:
            raise ValueError("model frame missing columns: " + repr(sorted(missing)))
        for year in years:
            cutoff = int(year)*10000+101
            train = frame[(frame.date < cutoff) & (frame.label_end < cutoff) &
                          frame.label.notna()].copy()
            class_counts = train.label.astype(int).value_counts()
            if (len(train) < self.config.minimum_training_rows or
                    len(class_counts) != 2 or
                    int(class_counts.min()) < self.config.minimum_class_rows):
                if skip_insufficient:
                    self.manifest.append({
                        "year": int(year), "cutoff": cutoff,
                        "status": "SKIPPED_INSUFFICIENT_TRAINING_DATA",
                        "training_rows": int(len(train)),
                        "training_groups": int(train[group_column].nunique()),
                        "class_counts": {
                            str(key): int(value)
                            for key, value in class_counts.items()},
                    })
                    continue
                raise ValueError("insufficient purged training data for {}: {}"
                                 .format(year, class_counts.to_dict()))
            group_size = train.groupby(group_column)[group_column].transform("size")
            sample_weight = 1.0/group_size.to_numpy(dtype=float)
            sample_weight *= len(sample_weight)/sample_weight.sum()
            pipeline = Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                ("model", LogisticRegression(
                    C=self.config.logistic_c, class_weight="balanced",
                    solver="liblinear", max_iter=2000,
                    random_state=self.config.random_state)),
            ])
            pipeline.fit(train[list(self.feature_names)],
                         train.label.astype(int), model__sample_weight=sample_weight)
            self.models[int(year)] = pipeline
            self.manifest.append({
                "year": int(year), "cutoff": cutoff, "status": "FITTED",
                "training_rows": int(len(train)),
                "training_groups": int(train[group_column].nunique()),
                "positive_rows": int(train.label.sum()),
                "negative_rows": int((1-train.label).sum()),
                "latest_label_end": int(train.label_end.max()),
            })
        return self

    def probability(self, date, values):
        year = int(date)//10000
        model = self.models.get(year)
        if model is None:
            return np.nan
        row = pd.DataFrame([{name: values.get(name, np.nan)
                             for name in self.feature_names}])
        return float(model.predict_proba(row)[0, 1])

    def score(self, frame):
        from sklearn.metrics import brier_score_loss, roc_auc_score

        rows = []
        for year, model in sorted(self.models.items()):
            sample = frame[(frame.date//10000 == year) & frame.label.notna()].copy()
            if sample.empty:
                continue
            probability = model.predict_proba(sample[list(self.feature_names)])[:, 1]
            label = sample.label.astype(int).to_numpy()
            rows.append({
                "year": year, "rows": int(len(sample)),
                "positive_rate": float(label.mean()),
                "mean_probability": float(probability.mean()),
                "brier": float(brier_score_loss(label, probability)),
                "auc": (float(roc_auc_score(label, probability))
                        if len(np.unique(label)) == 2 else np.nan),
            })
        return rows


class YearBoundaryShallowBoostingModel(YearBoundaryLogisticModel):
    """Fixed low-capacity nonlinear challenger used only for diagnostics."""

    def fit(self, frame, years, group_column, skip_insufficient=False):
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline

        required = {"date", "label_end", "label", group_column,
                    *self.feature_names}
        missing = required-set(frame.columns)
        if missing:
            raise ValueError("model frame missing columns: " + repr(sorted(missing)))
        for year in years:
            cutoff = int(year)*10000+101
            train = frame[(frame.date < cutoff) & (frame.label_end < cutoff) &
                          frame.label.notna()].copy()
            class_counts = train.label.astype(int).value_counts()
            if (len(train) < self.config.minimum_training_rows or
                    len(class_counts) != 2 or
                    int(class_counts.min()) < self.config.minimum_class_rows):
                if skip_insufficient:
                    self.manifest.append({
                        "year": int(year), "cutoff": cutoff,
                        "status": "SKIPPED_INSUFFICIENT_TRAINING_DATA",
                        "training_rows": int(len(train)),
                        "training_groups": int(train[group_column].nunique()),
                        "class_counts": {
                            str(key): int(value)
                            for key, value in class_counts.items()},
                    })
                    continue
                raise ValueError("insufficient purged training data for {}: {}"
                                 .format(year, class_counts.to_dict()))
            group_size = train.groupby(group_column)[group_column].transform("size")
            sample_weight = 1.0/group_size.to_numpy(dtype=float)
            label = train.label.astype(int).to_numpy()
            counts = np.bincount(label, minlength=2).astype(float)
            class_weight = len(label)/(2*counts)
            sample_weight *= class_weight[label]
            sample_weight *= len(sample_weight)/sample_weight.sum()
            pipeline = Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("model", HistGradientBoostingClassifier(
                    learning_rate=.05, max_iter=100, max_leaf_nodes=7,
                    min_samples_leaf=50, l2_regularization=10.0,
                    random_state=self.config.random_state)),
            ])
            pipeline.fit(train[list(self.feature_names)], label,
                         model__sample_weight=sample_weight)
            self.models[int(year)] = pipeline
            self.manifest.append({
                "year": int(year), "cutoff": cutoff, "status": "FITTED",
                "model": "shallow_hist_gradient_boosting",
                "training_rows": int(len(train)),
                "training_groups": int(train[group_column].nunique()),
                "positive_rows": int(train.label.sum()),
                "negative_rows": int((1-train.label).sum()),
                "latest_label_end": int(train.label_end.max()),
            })
        return self


def add_entry_tail_risk_labels(panel, frame, horizon_sessions=20,
                               profit_r_multiple=1.0):
    """Label whether the initial stop is reached before +1R within a horizon.

    No-touch observations are non-events.  A bar that touches both barriers
    first is excluded because daily data cannot establish the intraday order.
    """
    if horizon_sessions <= 0 or profit_r_multiple <= 0:
        raise ValueError("tail-risk label settings must be positive")
    result = frame.copy()
    day = result.day.to_numpy(dtype=int)
    column = result.column.to_numpy(dtype=int)
    valid = day+horizon_sessions < len(panel.dates)
    entry = panel.open[np.minimum(day+1, len(panel.dates)-1), column].astype(float)
    signal_close = panel.close[day, column].astype(float)
    initial_stop = signal_close-2*panel.atr21[day, column]
    initial_r = entry-initial_stop
    profit = entry+profit_r_multiple*initial_r
    valid &= (np.isfinite(entry) & np.isfinite(initial_stop) &
              np.isfinite(initial_r) & (initial_r > 0))
    first_stop = np.full(len(result), horizon_sessions+1, dtype=int)
    first_profit = np.full(len(result), horizon_sessions+1, dtype=int)
    for offset in range(1, horizon_sessions+1):
        lookup = np.minimum(day+offset, len(panel.dates)-1)
        low = panel.low[lookup, column]
        high = panel.high[lookup, column]
        first_stop[(first_stop > horizon_sessions) &
                   np.isfinite(low) & (low <= initial_stop)] = offset
        first_profit[(first_profit > horizon_sessions) &
                     np.isfinite(high) & (high >= profit)] = offset
    ambiguous = valid & (first_stop == first_profit) & \
        (first_stop <= horizon_sessions)
    label = np.full(len(result), np.nan)
    label[valid & ~ambiguous] = (
        first_stop[valid & ~ambiguous] <
        first_profit[valid & ~ambiguous]).astype(float)
    event_offset = np.minimum(first_stop, first_profit)
    event_offset = np.minimum(event_offset, horizon_sessions)
    label_end = np.full(len(result), 99991231, dtype=int)
    rows = np.flatnonzero(valid)
    label_end[rows] = panel.dates[day[rows]+event_offset[rows]].astype(int)
    result["label"] = label
    result["label_end"] = label_end
    result["tail_stop_first"] = label
    result["tail_ambiguous"] = ambiguous
    result["tail_event_offset"] = np.where(valid, event_offset, np.nan)
    return result


def add_holding_protective_exit_labels(panel, frame, logical_trades,
                                       base_exits, horizon_sessions=5):
    """Label a protective exit in the next N sessions, censoring other exits."""
    if horizon_sessions <= 0:
        raise ValueError("holding risk horizon must be positive")
    result = frame.copy()
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    exits = base_exits.copy()
    exits["protective"] = exits.reason.isin(("INITIAL_STOP", "TRAILING_STOP"))
    trades = logical_trades[["trade_id", "symbol", "opened_at", "closed_at"]]
    event_by_trade = {}
    for trade in trades.itertuples(index=False):
        candidates = exits[(exits.symbol.astype(str) == str(trade.symbol)) &
                           (exits.date.astype(int) >= int(trade.opened_at))]
        if np.isfinite(trade.closed_at):
            candidates = candidates[
                candidates.date.astype(int) <= int(trade.closed_at)]
        if len(candidates):
            event = candidates.sort_values("date").iloc[0]
            event_by_trade[str(trade.trade_id)] = (
                int(event.date), bool(event.protective), str(event.reason))
    label = np.full(len(result), np.nan)
    label_end = np.full(len(result), 99991231, dtype=int)
    event_reason = []
    for position, row in enumerate(result.itertuples(index=False)):
        day = date_index[int(row.date)]
        if day+horizon_sessions >= len(panel.dates):
            event_reason.append("END_OF_DATA")
            continue
        event = event_by_trade.get(str(row.trade_id))
        if event is None:
            label[position] = 0.0
            label_end[position] = int(panel.dates[day+horizon_sessions])
            event_reason.append("NO_EXIT_IN_TRADE")
            continue
        event_date, protective, reason = event
        gap = date_index[event_date]-day
        if gap <= 0:
            event_reason.append("EVENT_ALREADY_DUE")
            continue
        if gap <= horizon_sessions:
            label_end[position] = event_date
            label[position] = 1.0 if protective else np.nan
            event_reason.append(reason)
        else:
            label[position] = 0.0
            label_end[position] = int(panel.dates[day+horizon_sessions])
            event_reason.append("NO_EXIT_IN_HORIZON")
    result["label"] = label
    result["label_end"] = label_end
    result["protective_exit_within_horizon"] = label
    result["label_event_reason"] = event_reason
    return result


def risk_lift_diagnostics(model, frame, top_fraction=.10):
    """Evaluate risk probability and top-tail lift without choosing a threshold."""
    from sklearn.metrics import brier_score_loss, roc_auc_score

    if not 0 < top_fraction < 1:
        raise ValueError("top_fraction must be in (0, 1)")
    rows = []
    for year, estimator in sorted(model.models.items()):
        sample = frame[(frame.date//10000 == year) & frame.label.notna()].copy()
        if sample.empty:
            continue
        probability = estimator.predict_proba(
            sample[list(model.feature_names)])[:, 1]
        label = sample.label.astype(int).to_numpy()
        count = max(1, int(np.ceil(len(sample)*top_fraction)))
        order = np.argsort(-probability, kind="mergesort")
        selected = label[order[:count]]
        base_rate = float(label.mean())
        tail_rate = float(selected.mean())
        rows.append({
            "year": int(year), "rows": int(len(sample)),
            "events": int(label.sum()), "event_rate": base_rate,
            "mean_probability": float(probability.mean()),
            "auc": (float(roc_auc_score(label, probability))
                    if len(np.unique(label)) == 2 else np.nan),
            "brier": float(brier_score_loss(label, probability)),
            "top_fraction": float(top_fraction), "top_rows": count,
            "top_event_rate": tail_rate,
            "top_lift": float(tail_rate/base_rate) if base_rate else np.nan,
            "event_capture": float(selected.sum()/label.sum())
            if label.sum() else np.nan,
        })
    return rows


def build_entry_frame(panel, scores, config=None):
    """Build sparse training events plus features for all tradable top-100 rows."""
    config = config or TimingMLConfig()
    frame = scores.copy()
    if frame.duplicated(["signal_asof", "symbol"]).any():
        raise ValueError("duplicate entry score")
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    frame["day"] = frame.signal_asof.map(date_index)
    if frame.day.isna().any():
        raise ValueError("entry score date outside panel")
    day = frame.day.to_numpy(dtype=int)
    column = frame.column.to_numpy(dtype=int)
    close = panel.close[day, column].astype(float)
    frame["date"] = frame.signal_asof.astype(int)
    frame["rank_percentile"] = 1-(frame.daily_rank.to_numpy(dtype=float)-.5)/100.0
    frame["return_5d"] = _safe_ratio(close, panel.close[day-5, column])-1
    frame["return_20d"] = _safe_ratio(close, panel.close[day-20, column])-1
    frame["return_60d"] = _safe_ratio(close, panel.close[day-60, column])-1
    frame["ma60_bias"] = _safe_ratio(close, panel.ma60[day, column])-1
    frame["atr_fraction"] = _safe_ratio(panel.atr21[day, column], close)
    amount_ratio = np.full(len(frame), np.nan)
    for current_day in np.unique(day):
        mask = day == current_day
        columns = column[mask]
        median = np.nanmedian(panel.amount[current_day-19:current_day+1, columns], axis=0)
        amount_ratio[mask] = _safe_ratio(panel.amount[current_day, columns], median)
    frame["amount_ratio_20d"] = amount_ratio
    frame["benchmark_return_20d"] = (
        _safe_ratio(panel.benchmark_close[day], panel.benchmark_close[day-20])-1)
    horizon = config.entry_horizon_sessions
    label_end_index = day+horizon
    valid = label_end_index < len(panel.dates)
    label_return = np.full(len(frame), np.nan)
    valid_rows = np.flatnonzero(valid)
    if len(valid_rows):
        entry = panel.open[day[valid_rows]+1, column[valid_rows]].astype(float)
        ending = panel.close[label_end_index[valid_rows], column[valid_rows]].astype(float)
        label_return[valid_rows] = (_safe_ratio(ending, entry)-1-
                                    config.entry_roundtrip_cost_bps/10000.0)
    frame["label_return"] = label_return
    frame["label"] = np.where(np.isfinite(label_return), label_return > 0, np.nan)
    frame["label_end"] = 99991231
    frame.loc[valid, "label_end"] = panel.dates[label_end_index[valid]].astype(int)
    first_day = int(frame.day.min())
    frame["training_sample"] = (
        (frame.daily_rank <= 50) &
        ((frame.day-first_day) % config.training_sample_stride == 0))
    return frame


def build_exit_training_frame(panel, positions, logical_trades, base_exits,
                              score_lookup, config=None):
    """Build non-overlapping baseline holding examples for continuation labels."""
    config = config or TimingMLConfig()
    trades = logical_trades[[
        "trade_id", "initial_entry_price_adjusted", "initial_stop_adjusted",
        "initial_r_per_share_adjusted", "entry_session_index",
    ]].copy()
    frame = positions.merge(trades, on="trade_id", how="left", validate="many_to_one")
    if frame.initial_r_per_share_adjusted.isna().any():
        raise ValueError("holding row missing trade state")
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    frame["day"] = frame.date.map(date_index)
    frame["column"] = frame.symbol.map(panel.symbol_index)
    if frame[["day", "column"]].isna().any().any():
        raise ValueError("holding row outside panel")
    day = frame.day.to_numpy(dtype=int)
    column = frame.column.to_numpy(dtype=int)
    close = panel.close[day, column].astype(float)
    initial = frame.initial_entry_price_adjusted.to_numpy(dtype=float)
    initial_r = frame.initial_r_per_share_adjusted.to_numpy(dtype=float)
    frame["alpha_score"] = [score_lookup.get((int(date), str(symbol)), np.nan)
                            for date, symbol in zip(frame.date, frame.symbol)]
    frame["return_5d"] = _safe_ratio(close, panel.close[day-5, column])-1
    frame["return_20d"] = _safe_ratio(close, panel.close[day-20, column])-1
    frame["return_60d"] = _safe_ratio(close, panel.close[day-60, column])-1
    frame["ma60_bias"] = _safe_ratio(close, panel.ma60[day, column])-1
    frame["atr_fraction"] = _safe_ratio(panel.atr21[day, column], close)
    frame["benchmark_return_20d"] = (
        _safe_ratio(panel.benchmark_close[day], panel.benchmark_close[day-20])-1)
    frame["holding_sessions"] = day-frame.entry_session_index.to_numpy(dtype=int)+1
    frame["unrealized_r"] = _safe_ratio(close-initial, initial_r)
    frame["peak_close"] = frame.assign(_close=close).groupby("trade_id")._close.cummax()
    peak = np.maximum(frame.peak_close.to_numpy(dtype=float), initial)
    frame["mfe_r"] = _safe_ratio(peak-initial, initial_r)
    frame["peak_drawdown_r"] = _safe_ratio(close-peak, initial_r)
    trailing = frame.mfe_r.to_numpy(dtype=float) >= 1.0
    stop = np.maximum(frame.initial_stop_adjusted.to_numpy(dtype=float),
                      np.where(trailing, peak-3*panel.atr21[day, column], -np.inf))
    frame["stop_distance_r"] = _safe_ratio(close-stop, initial_r)
    frame["trailing_enabled"] = trailing.astype(float)
    horizon = config.exit_horizon_sessions
    label_end_index = day+horizon
    valid = label_end_index < len(panel.dates)
    label_return = np.full(len(frame), np.nan)
    valid_rows = np.flatnonzero(valid)
    if len(valid_rows):
        start = panel.open[day[valid_rows]+1, column[valid_rows]].astype(float)
        ending = panel.close[label_end_index[valid_rows], column[valid_rows]].astype(float)
        label_return[valid_rows] = _safe_ratio(ending, start)-1
    frame["label_return"] = label_return
    frame["label"] = np.where(np.isfinite(label_return), label_return > 0, np.nan)
    frame["label_end"] = 99991231
    frame.loc[valid, "label_end"] = panel.dates[label_end_index[valid]].astype(int)
    exit_keys = set(zip(base_exits.date.astype(int), base_exits.symbol.astype(str)))
    frame["base_exit_day"] = [
        (int(date), str(symbol)) in exit_keys
        for date, symbol in zip(frame.date, frame.symbol)]
    frame["training_sample"] = (
        ~frame.base_exit_day &
        (((frame.holding_sessions-1) % config.training_sample_stride) == 0))
    return frame


class EntryMetaOverlay:
    """Suppress a candidate only when the fixed entry model rejects it."""

    def __init__(self, model, feature_frame, config=None):
        self.model = model
        self.config = config or TimingMLConfig()
        columns = ["date", "symbol", *ENTRY_FEATURES]
        self.features = {
            (int(row.date), str(row.symbol)): row._asdict()
            for row in feature_frame[columns].itertuples(index=False)}
        self.decisions = []

    def filter_entries(self, panel, executor, day, entry_symbols):
        date = int(panel.dates[day])
        allowed = []
        for symbol in entry_symbols:
            values = self.features.get((date, str(symbol)))
            probability = (np.nan if values is None else
                           self.model.probability(date, values))
            passed = bool(np.isfinite(probability) and
                          probability >= self.config.entry_probability_threshold)
            self.decisions.append({
                "signal_asof": date, "symbol": str(symbol),
                "probability_positive": probability,
                "threshold": self.config.entry_probability_threshold,
                "allowed": passed,
                "status": "READY" if np.isfinite(probability) else "MISSING_MODEL",
            })
            if passed:
                allowed.append(symbol)
        return allowed


class ExitContinuationOverlay:
    """Add a voluntary exit after persistently low continuation probability."""

    def __init__(self, model, score_lookup, config=None):
        self.model = model
        self.score_lookup = score_lookup
        self.config = config or TimingMLConfig()
        self.streak = {}
        self.decisions = []

    def signal(self, panel, executor, day, symbol, state):
        date = int(panel.dates[day])
        column = panel.symbol_index[symbol]
        close = float(panel.close[day, column])
        initial = state.initial_stop_adjusted+state.initial_r_adjusted
        initial_r = state.initial_r_adjusted
        values = {
            "alpha_score": self.score_lookup.get((date, str(symbol)), np.nan),
            "return_5d": float(_safe_ratio(close, panel.close[day-5, column])-1),
            "return_20d": float(_safe_ratio(close, panel.close[day-20, column])-1),
            "return_60d": float(_safe_ratio(close, panel.close[day-60, column])-1),
            "ma60_bias": float(_safe_ratio(close, panel.ma60[day, column])-1),
            "atr_fraction": float(_safe_ratio(panel.atr21[day, column], close)),
            "benchmark_return_20d": float(
                _safe_ratio(panel.benchmark_close[day], panel.benchmark_close[day-20])-1),
            "holding_sessions": int(day-state.entry_day+1),
            "unrealized_r": float(_safe_ratio(close-initial, initial_r)),
            "mfe_r": float(_safe_ratio(state.peak_close_adjusted-initial, initial_r)),
            "peak_drawdown_r": float(
                _safe_ratio(close-state.peak_close_adjusted, initial_r)),
            "stop_distance_r": float(
                _safe_ratio(close-state.current_stop_adjusted, initial_r)),
            "trailing_enabled": float(state.trailing_enabled),
        }
        probability = self.model.probability(date, values)
        weak = bool(np.isfinite(probability) and
                    probability <= self.config.exit_probability_threshold)
        self.streak[symbol] = self.streak.get(symbol, 0)+1 if weak else 0
        triggered = self.streak[symbol] >= self.config.exit_persistence_sessions
        self.decisions.append({
            "signal_asof": date, "symbol": symbol,
            "probability_positive": probability,
            "threshold": self.config.exit_probability_threshold,
            "weak": weak, "weak_streak": self.streak[symbol],
            "triggered": triggered,
            "status": "READY" if np.isfinite(probability) else "MISSING_MODEL",
        })
        if triggered:
            self.streak.pop(symbol, None)
            return "ML_CONTINUATION_EXIT"
        return None
