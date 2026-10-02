# -*- encoding:utf-8 -*-
"""Scale a long A-share position down as the observed ATR rises."""

from __future__ import division

import numpy as np

from .ABuPositionBase import AbuPositionBase


class AbuVolatilityRiskPosition(AbuPositionBase):
    """Use signal-day ATR with buy_tomorrow factors only.

    A buy_today factor would make the signal day's ATR unavailable at execution.
    """

    def _init_self(self, **kwargs):
        self.max_weight = float(kwargs.pop('max_weight', 0.12))
        self.risk_fraction = float(kwargs.pop('risk_fraction', 0.01))
        self.atr_move = float(kwargs.pop('atr_move', 2.0))
        if not 0 < self.max_weight <= 1 or self.risk_fraction <= 0 or self.atr_move <= 0:
            raise ValueError('invalid volatility position parameters')

    def fit_position(self, factor_object):
        signal = factor_object.kl_pd.iloc[factor_object.today_ind]
        if int(self.kl_pd_buy.date) <= int(signal.date):
            raise ValueError('AbuVolatilityRiskPosition requires a next-day buy factor')
        close = float(signal.close)
        atr = float(signal.atr21)
        if not np.isfinite(close) or not np.isfinite(atr) or close <= 0 or atr <= 0:
            return 0
        atr_fraction = atr / close
        weight = min(self.max_weight, self.pos_max,
                     self.risk_fraction / (self.atr_move * atr_fraction))
        return self.read_cash * weight / self.bp * self.deposit_rate
