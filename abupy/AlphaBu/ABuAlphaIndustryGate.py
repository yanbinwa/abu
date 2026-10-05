"""Research-only entry gate on unranked, signal-close industry excess return."""
import numpy as np

from .ABuAlpha158Lite import Alpha158LiteFeatureEngine, ALPHA158_LITE_FEATURES
from .ABuCostAwareAlpha import CostAwareReview


class IndustryExcessHistory:
    """Cache pure features for one immutable panel; never cache account decisions."""

    def __init__(self, panel, config):
        self.panel = panel
        self.engine = Alpha158LiteFeatureEngine(panel, config)
        self.column = ALPHA158_LITE_FEATURES.index('stock_excess_industry_20d')
        self.cache = {}

    def values(self, day):
        if day not in self.cache:
            values = self.engine.raw_features(day)[:, self.column].copy()
            values.setflags(write=False)
            self.cache[day] = values
        return self.cache[day]


class IndustryExcessEntryGate(CostAwareReview):
    """Keep existing entry order/persistence and all protective exit rules.

    Threshold is a raw return difference: -0.05 means minus five percentage
    points. Missing values fail closed. This gate does not rank extra stocks,
    resize orders, or force an exit when an existing holding fails the gate.
    """

    def __init__(self, history, minimum_excess=-0.05):
        super().__init__(suppress_rank_exits=True)
        if not np.isfinite(minimum_excess):
            raise ValueError('finite minimum industry excess required')
        self.history = history
        self.minimum_excess = float(minimum_excess)
        self.entry_decisions = []

    def filter_review(self, panel, executor, day, rank_exits, entry_symbols):
        if panel is not self.history.panel:
            raise ValueError('industry history belongs to a different panel')
        rank_exits, entry_symbols = super().filter_review(
            panel, executor, day, rank_exits, entry_symbols)
        if not entry_symbols:
            return rank_exits, entry_symbols
        values = self.history.values(day)
        allowed = []
        for symbol in entry_symbols:
            value = float(values[panel.symbol_index[symbol]])
            passed = bool(np.isfinite(value) and value >= self.minimum_excess)
            self.entry_decisions.append(dict(
                signal_asof=int(panel.dates[day]), symbol=symbol,
                stock_excess_industry_20d=value,
                minimum_excess=self.minimum_excess, allowed=passed))
            if passed:
                allowed.append(symbol)
        return rank_exits, allowed
