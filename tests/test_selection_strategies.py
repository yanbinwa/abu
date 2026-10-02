"""Timing and account invariants for the A-share selection research engine."""

import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuSelectionStrategies import (
    INITIAL_CASH, SelectionPanel, _fee_components, run_selection_backtest,
    run_matched_placebos,
)


def make_panel(lock_first_2025=False, future_shock_day=None):
    calendar = pd.bdate_range('2024-01-02', '2025-04-30')
    dates = calendar.strftime('%Y%m%d').astype(int).to_numpy()
    t = np.arange(len(dates))
    benchmark = 100 * 1.001 ** t
    close = np.column_stack((10 * 1.012 ** t, 20 * 1.007 ** t,
                             30 * 1.0015 ** t)).astype(np.float32)
    if future_shock_day is not None:
        close[future_shock_day + 1:] *= 0.01
    opening = close.copy()
    high, low = close * 1.002, close * 0.998
    volume = np.full_like(close, 100_000)
    if lock_first_2025:
        first = np.flatnonzero(dates // 10000 == 2025)[0]
        high[first] = low[first] = opening[first]
    return SelectionPanel(dates, ('a', 'b', 'c'), opening, high, low,
                          close, volume, benchmark)


class SelectionStrategiesTest(unittest.TestCase):
    def test_momentum_signal_ignores_future_bars(self):
        panel = make_panel()
        day = 280
        expected = panel.momentum_picks(day)
        altered = make_panel(future_shock_day=day)
        self.assertEqual(expected, altered.momentum_picks(day))

    def test_next_day_execution_cash_and_locked_buy(self):
        panel = make_panel()
        result, curve, trades = run_selection_backtest(
            panel, 'trend_breakout', 2025, 25)
        first_day = int(panel.dates[np.flatnonzero(panel.dates // 10000 == 2025)[0]])
        self.assertGreater(result['buys'], 0)
        self.assertEqual(first_day, int(trades.iloc[0].date))
        self.assertTrue((curve.cash >= -1e-6).all())
        self.assertEqual(result['buys'], int((trades.side == 'buy').sum()))
        self.assertEqual(result['sells'], int((trades.side == 'sell').sum()))
        for symbol in trades.symbol.unique():
            symbol_trades = trades[trades.symbol == symbol]
            if (symbol_trades.side == 'sell').any():
                self.assertLess(int(symbol_trades.iloc[0].date),
                                int(symbol_trades[symbol_trades.side == 'sell'].iloc[0].date))

        locked = make_panel(lock_first_2025=True)
        _, _, locked_trades = run_selection_backtest(
            locked, 'trend_breakout', 2025, 25)
        self.assertFalse((locked_trades.date == first_day).any())

    def test_raw_prices_drive_fills_and_initial_cash_drives_drawdown(self):
        panel = make_panel()
        raw = {
            'open': panel.open * 2,
            'high': panel.high * 2,
            'low': panel.low * 2,
            'close': panel.close * 2,
            'volume': panel.volume,
        }
        enhanced = SelectionPanel(
            panel.dates, panel.symbols, panel.open, panel.high, panel.low,
            panel.close, panel.volume, panel.benchmark_close,
            execution=raw)
        result, curve, trades = run_selection_backtest(
            enhanced, 'trend_breakout', 2025, 0)
        first_buy = trades[trades.side == 'buy'].iloc[0]
        day = int(np.flatnonzero(panel.dates == first_buy.date)[0])
        symbol = panel.symbols.index(first_buy.symbol)
        self.assertAlmostEqual(first_buy.reference_price,
                               float(raw['open'][day, symbol]), places=5)
        values = np.r_[INITIAL_CASH, curve.capital.to_numpy()]
        expected = (values / np.maximum.accumulate(values) - 1).min() * 100
        self.assertAlmostEqual(result['max_drawdown_pct'], expected, places=8)

    def test_fee_components_and_corporate_action_credits(self):
        commission, transfer, stamp = _fee_components(100, 1.0, 'sell')
        self.assertEqual(commission, 5.0)
        self.assertAlmostEqual(transfer, 0.001)
        self.assertAlmostEqual(stamp, 0.05)

        panel = make_panel()
        first = int(np.flatnonzero(panel.dates // 10000 == 2025)[0])
        actions = {
            first: [{
                'symbol': 0, 'cash_per_share': 0.1,
                'stock_per_share': 0.1, 'cash_day': first + 1,
                'stock_day': first + 1, 'description': 'test action',
            }]
        }
        enhanced = SelectionPanel(
            panel.dates, panel.symbols, panel.open, panel.high, panel.low,
            panel.close, panel.volume, panel.benchmark_close,
            corporate_actions=actions)
        _, _, trades = run_selection_backtest(
            enhanced, 'trend_breakout', 2025, 0)
        # Symbol a is the strongest synthetic breakout and is held on record day.
        self.assertTrue((trades.side == 'cash_dividend').any())
        self.assertTrue((trades.side == 'stock_dividend').any())
        self.assertGreater(trades.loc[trades.side == 'cash_dividend',
                                     'cash_flow'].sum(), 0)

    def test_matched_placebo_preserves_run_count(self):
        panel = make_panel()
        result, _, trades = run_selection_backtest(
            panel, 'trend_breakout', 2025, 25)
        summary, distribution = run_matched_placebos(
            panel, trades, 2025, result['return_pct'], replicates=12, seed=7)
        self.assertEqual(len(distribution), 12)
        self.assertEqual(summary['placebo_replicates'], 12)
        self.assertGreater(summary['matched_trade_templates'], 0)
        self.assertTrue(distribution.return_pct.notna().all())


if __name__ == '__main__':
    unittest.main()
