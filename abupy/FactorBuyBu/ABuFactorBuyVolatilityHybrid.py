# -*- encoding:utf-8 -*-
"""A causal weekly/monthly calendar blend with ATR sized positions."""

from .ABuFactorBuyDemo import AbuWeekMonthBuy


def _calendar_signals(benchmark_kl):
    """Use the market calendar, rather than each stock's next observed bar."""
    dates = benchmark_kl.date.astype(int).to_numpy()
    months = dates // 100 % 100
    month_end_dates = set(dates[:-1][months[:-1] != months[1:]])
    friday_dates = set(benchmark_kl.loc[benchmark_kl.date_week == 4, 'date'].astype(int))
    next_market_date = dict(zip(dates[:-1], dates[1:]))
    return month_end_dates, friday_dates, next_market_date


def _next_bar_is_next_market_day(factor, today):
    next_date = factor.next_market_date.get(int(today.date))
    return (next_date is not None and factor.today_ind + 1 < len(factor.kl_pd)
            and int(factor.kl_pd.iloc[factor.today_ind + 1].date) == next_date)


def build_volatility_blend(variable_size=True):
    """Return ABU factor dictionaries for the weekly/monthly ATR sized blend."""
    from ..BetaBu.ABuVolatilityRiskPosition import AbuVolatilityRiskPosition
    from ..FactorSellBu.ABuFactorSellNDay import AbuFactorSellNDay

    multiplier = 1.0 if variable_size else 100.0
    monthly_position = {'class': AbuVolatilityRiskPosition, 'max_weight': 0.08,
                        'risk_fraction': 0.006 * multiplier, 'atr_move': 2.0}
    weekly_position = {'class': AbuVolatilityRiskPosition, 'max_weight': 0.04,
                       'risk_fraction': 0.003 * multiplier, 'atr_move': 2.0}
    buy = [
        {'class': AbuFactorBuyCalendarTomorrow, 'is_buy_month': True,
         'position': monthly_position},
        {'class': AbuFactorBuyCalendarTomorrow, 'is_buy_month': False,
         'position': weekly_position},
    ]
    sell = [{'class': AbuFactorSellNDay, 'sell_n': 20}]
    return buy, sell


class AbuFactorBuyCalendarTomorrow(AbuWeekMonthBuy):
    """Use an existing weekly/monthly calendar trigger with next-day execution."""

    def _init_self(self, **kwargs):
        super(AbuFactorBuyCalendarTomorrow, self)._init_self(**kwargs)
        self.factor_name = 'CalendarTomorrow:month' if self.is_buy_month else 'CalendarTomorrow:week'
        self.month_end_dates, self.friday_dates, self.next_market_date = _calendar_signals(
            self.benchmark.kl_pd)

    def fit_day(self, today):
        if not _next_bar_is_next_market_day(self, today):
            return None
        date = int(today.date)
        if (self.is_buy_month and date in self.month_end_dates) or \
                (not self.is_buy_month and date in self.friday_dates):
            return self.buy_tomorrow()
