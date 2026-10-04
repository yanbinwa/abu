"""Past-only return calibration and voluntary replacement gates for Alpha-lite.

This independent price-only experiment does not implement or unblock fundamental
multifactor v4. Scores are converted to return bps before comparison with costs.
Event exits and the existing risk approval remain authoritative.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CalibrationConfig:
    label_horizon_sessions: int = 20
    minimum_calibration_dates: int = 126
    maximum_calibration_dates: int = 252
    percentile_edges: tuple = (0., .5, .8, .9, .95, .98, .99, .995, 1.)
    hac_lags: int = 19
    confidence_z: float = 1.645
    extra_cost_buffer_bps: float = 5.

    def __post_init__(self):
        edges = np.asarray(self.percentile_edges, dtype=float)
        if (len(edges) < 3 or edges[0] != 0 or edges[-1] != 1 or
                not np.all(np.isfinite(edges)) or not np.all(np.diff(edges) > 0)):
            raise ValueError('invalid percentile edges')
        if (self.minimum_calibration_dates < 2 or
                self.maximum_calibration_dates < self.minimum_calibration_dates or
                self.label_horizon_sessions <= 0 or self.hac_lags < 0 or
                not np.isfinite(self.confidence_z) or self.confidence_z < 0 or
                not np.isfinite(self.extra_cost_buffer_bps) or self.extra_cost_buffer_bps < 0):
            raise ValueError('invalid calibration settings')


def hac_mean_se(values, lags):
    """Bartlett/Newey-West standard error for a mean of overlapping labels."""
    values = np.asarray(values, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all():
        raise ValueError('finite observations required')
    centered = values-values.mean()
    n = len(values)
    lag_count = min(int(lags), n-1)
    variance = np.dot(centered, centered)/n
    for lag in range(1, lag_count+1):
        variance += 2*(1-lag/(lag_count+1))*np.dot(centered[lag:], centered[:-lag])/n
    return float(np.sqrt(max(0., variance)/(n-1)))


class PastScoreCalibration:
    """Calibrate OOS cross-sectional percentile bins with matured OOS labels.

    Labels must end strictly before the decision date. Each historical signal
    date has equal weight. No monotonicity is imposed after observing outcomes.
    """

    def __init__(self, predictions, calendar, config=None):
        self.config = config or CalibrationConfig()
        self.calendar = np.asarray(calendar, dtype=int)
        if len(self.calendar) < 2 or not np.all(np.diff(self.calendar) > 0):
            raise ValueError('calendar must be unique and increasing')
        required = ['signal_asof', 'symbol', 'column', 'alpha_score',
                    'excess_return_20d', 'train_end']
        if self.config.label_horizon_sessions != 20:
            raise ValueError('this artifact contains only 20-session labels')
        frame = predictions[required].copy()
        if frame.duplicated(['signal_asof', 'symbol']).any():
            raise ValueError('duplicate prediction')
        if not (frame.train_end < frame.signal_asof).all():
            raise ValueError('predictions must be out of sample')
        if not np.isfinite(frame.alpha_score).all():
            raise ValueError('non-finite score')
        rows = pd.Index(self.calendar).get_indexer(frame.signal_asof)
        if (rows < 0).any():
            raise ValueError('prediction date outside calendar')
        maturity = rows+self.config.label_horizon_sessions
        frame['label_end'] = self.calendar[np.minimum(maturity, len(self.calendar)-1)]
        frame.loc[maturity >= len(self.calendar), 'label_end'] = 99991231
        groups = frame.groupby('signal_asof').alpha_score
        percentile = (groups.rank(method='average')-.5)/groups.transform('size')
        frame['bucket'] = np.minimum(np.searchsorted(
            self.config.percentile_edges, percentile.to_numpy(), side='right')-1,
            len(self.config.percentile_edges)-2)
        self.current = {int(date): group.set_index('symbol')[['column', 'bucket']]
                        for date, group in frame.groupby('signal_asof', sort=True)}
        labeled = frame[np.isfinite(frame.excess_return_20d)]
        daily = labeled.groupby(['signal_asof', 'bucket']).excess_return_20d.mean().unstack()
        self.daily = daily.reindex(columns=range(len(self.config.percentile_edges)-1))*10000
        self.maturity = frame.groupby('signal_asof').label_end.first().reindex(self.daily.index)
        self._cache = {}

    def window(self, asof):
        return self.daily.loc[self.maturity < int(asof)].tail(self.config.maximum_calibration_dates)

    def first_ready_date(self):
        for date in sorted(self.current):
            if len(self.window(date)) >= self.config.minimum_calibration_dates:
                return date
        raise ValueError('insufficient matured calibration history')

    def edge(self, asof, candidate, holding):
        current = self.current.get(int(asof))
        missing = dict(status='MISSING_CURRENT_SCORE', mean_edge_bps=np.nan,
                       se_bps=np.nan, lower_edge_bps=np.nan, calibration_dates=0,
                       latest_label_end=None)
        if current is None or candidate not in current.index or holding not in current.index:
            return missing
        candidate_bin = int(current.loc[candidate, 'bucket'])
        holding_bin = int(current.loc[holding, 'bucket'])
        key = (int(asof), candidate_bin, holding_bin)
        if key in self._cache:
            return dict(self._cache[key])
        history = self.window(asof)
        pair = history[[candidate_bin, holding_bin]].dropna()
        count = len(pair)
        if count < self.config.minimum_calibration_dates:
            return dict(missing, status='INSUFFICIENT_CALIBRATION', calibration_dates=count)
        # Indexing columns individually also handles candidate_bin == holding_bin.
        spread = (history[candidate_bin]-history[holding_bin]).dropna().to_numpy()
        mean = float(spread.mean())
        se = hac_mean_se(spread, self.config.hac_lags)
        result = dict(status='READY', mean_edge_bps=mean, se_bps=se,
                      lower_edge_bps=mean-self.config.confidence_z*se,
                      calibration_dates=count, candidate_bucket=candidate_bin,
                      holding_bucket=holding_bin,
                      latest_label_end=int(self.maturity.loc[pair.index].max()))
        self._cache[key] = result
        return dict(result)


def switching_cost_bps(buy_notional, sell_notional, execution):
    """Two immediate legs, including minimum commission, tax and slippage.

    The caller uses one board lot for the buy commission estimate, conservatively
    avoiding underestimating minimum commissions if risk approval reduces size.
    Values are decision-close estimates, not guaranteed next-open costs.
    """
    if not np.isfinite([buy_notional, sell_notional]).all() or min(buy_notional, sell_notional) <= 0:
        return np.nan
    slip = execution.slippage_bps/10000
    buy = (max(execution.min_commission/buy_notional,
               execution.broker_rate*(1+slip)) + execution.transfer_rate*(1+slip) + slip)
    sell = (max(execution.min_commission/sell_notional,
                execution.broker_rate*(1-slip)) +
            (execution.transfer_rate+execution.sell_stamp_rate)*(1-slip) + slip)
    return float((buy+sell)*10000)


class CostAwareReview:
    """Filter voluntary rank replacements; never filter protective exits.

    If an exit passes, only evaluated replacement candidates may be queued that
    review. A rejected next-open fill can still leave cash; no atomic fill or
    guaranteed reinvestment is assumed. Existing free-slot entries are otherwise
    unchanged. Holding scores outside the top-100 list use full-day predictions.
    """

    def __init__(self, calibration=None, suppress_rank_exits=False):
        self.calibration = calibration
        self.suppress_rank_exits = suppress_rank_exits
        self.decisions = []

    def filter_review(self, panel, executor, day, rank_exits, entry_symbols):
        if not rank_exits:
            return rank_exits, entry_symbols
        if len(rank_exits) != 1:
            raise ValueError('experiment requires at most one rank replacement')
        holding = rank_exits[0]
        asof = int(panel.dates[day])
        if self.suppress_rank_exits:
            self.decisions.append(dict(signal_asof=asof, holding=holding,
                                       allowed=False, status='RANK_EXIT_DISABLED'))
            return [], entry_symbols
        if self.calibration is None:
            raise ValueError('calibration required')
        position = executor.positions[holding]
        sell_price = float(panel.exec_close[day, panel.symbol_index[holding]])
        allowed = []
        for candidate in entry_symbols:
            edge = self.calibration.edge(asof, candidate, holding)
            buy_price = float(panel.exec_close[day, panel.symbol_index[candidate]])
            cost = switching_cost_bps(100*buy_price, position.quantity*sell_price, executor.config)
            hurdle = cost+self.calibration.config.extra_cost_buffer_bps
            passed = bool(edge['status'] == 'READY' and np.isfinite(hurdle) and
                          edge['lower_edge_bps'] > hurdle)
            self.decisions.append(dict(signal_asof=asof, holding=holding,
                candidate=candidate, cost_bps=cost, hurdle_bps=hurdle,
                allowed=passed, **edge))
            if passed:
                allowed.append(candidate)
        return ([holding], allowed) if allowed else ([], entry_symbols)
