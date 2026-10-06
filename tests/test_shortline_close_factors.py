"""Retrospective close-event factor tests."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuShortLineCloseFactors import (
    MarketSentimentEntryGate, MarketSentimentTransitionEntryGate, _symbol,
    attach_close_event_features, build_close_event_features,
)


def events(dates):
    rows = []
    for index, date in enumerate(dates):
        rows.extend([
            {"trade_date": date, "symbol": "sz000001", "status": "limit_up",
             "board_level": 1 + index % 3, "broken_count": 0,
             "seal_amount": 1000 + index},
            {"trade_date": date, "symbol": "sh600001", "status": "broken",
             "board_level": 0, "broken_count": 2, "seal_amount": 0},
        ])
    return pd.DataFrame(rows)


class ShortLineCloseFactorTest(unittest.TestCase):

    class _Panel:
        dates = np.asarray([20240101, 20240102])

    def test_new_chinext_prefix_is_mapped(self):
        self.assertEqual(_symbol("302132"), "sz302132")

    def test_daily_and_stock_features_have_expected_semantics(self):
        dates = [20240101 + value for value in range(25)]
        daily, stock = build_close_event_features(events(dates), dates)
        self.assertTrue((daily.limit_up_count == 1).all())
        self.assertTrue((daily.broken_count == 1).all())
        self.assertTrue(np.allclose(daily.seal_rate, 0.5))
        row = stock[(stock.signal_asof == dates[0]) &
                    stock.symbol.eq("sz000001")].iloc[0]
        self.assertEqual(row.event_limit_up, 1.0)
        self.assertEqual(row.event_broken, 0.0)

    def test_future_event_does_not_change_prior_rolling_features(self):
        dates = [20240101 + value for value in range(25)]
        before, _ = build_close_event_features(events(dates), dates)
        future = events(dates + [20240201])
        extra = pd.DataFrame([
            {"trade_date": 20240201, "symbol": "sz{:06d}".format(i + 100),
             "status": "limit_down", "board_level": 0,
             "broken_count": 0, "seal_amount": 0}
            for i in range(100)])
        after, _ = build_close_event_features(
            pd.concat([future, extra], ignore_index=True), dates + [20240201])
        np.testing.assert_allclose(
            before.sentiment_z63, after.iloc[:len(before)].sentiment_z63)
        np.testing.assert_allclose(
            before.risk_z63, after.iloc[:len(before)].risk_z63)

    def test_attach_drops_incomplete_dates_and_zeros_non_events(self):
        predictions = pd.DataFrame([
            {"signal_asof": 20240101, "symbol": "sz000001", "score": 2.0},
            {"signal_asof": 20240101, "symbol": "sz000002", "score": 1.0},
            {"signal_asof": 20240102, "symbol": "sz000001", "score": 3.0},
        ])
        daily, stock = build_close_event_features(
            events([20240101]), [20240101])
        result = attach_close_event_features(
            predictions, daily, stock, "score")
        self.assertEqual(set(result.signal_asof), {20240101})
        non_event = result[result.symbol.eq("sz000002")].iloc[0]
        self.assertEqual(non_event.event_limit_up, 0.0)
        self.assertGreater(result[result.symbol.eq("sz000001")].iloc[0]
                           .base_rank_centered, non_event.base_rank_centered)

    def test_market_gate_suppresses_entries_but_preserves_exits(self):
        factors = pd.DataFrame([
            {"signal_asof": 20240101, "sentiment_z63": -1.2,
             "risk_z63": 1.1},
            {"signal_asof": 20240102, "sentiment_z63": -0.5,
             "risk_z63": 1.5},
        ])
        gate = MarketSentimentEntryGate(factors)
        exits, entries = gate.filter_review(
            self._Panel(), None, 0, ["sz000001"], ["sz000002"])
        self.assertEqual(exits, ["sz000001"])
        self.assertEqual(entries, [])
        exits, entries = gate.filter_review(
            self._Panel(), None, 1, [], ["sz000003"])
        self.assertEqual(entries, ["sz000003"])
        self.assertEqual(gate.evaluations[0]["suppressed_entries"], 1)

    def test_market_gate_fails_on_incomplete_date(self):
        gate = MarketSentimentEntryGate(pd.DataFrame([
            {"signal_asof": 20240101, "sentiment_z63": -1.2,
             "risk_z63": 1.1},
        ]))
        with self.assertRaises(KeyError):
            gate.filter_review(self._Panel(), None, 1, [], ["sz000003"])

    def test_transition_gate_requires_deterioration_in_both_dimensions(self):
        factors = pd.DataFrame([
            {"signal_asof": 20240101, "sentiment_z63": -0.5,
             "risk_z63": 0.5},
            {"signal_asof": 20240102, "sentiment_z63": -1.2,
             "risk_z63": 1.1},
        ])
        gate = MarketSentimentTransitionEntryGate(factors)
        self.assertEqual(gate.trigger_dates(), [20240102])
        _, entries = gate.filter_review(
            self._Panel(), None, 1, [], ["sz000003"])
        self.assertEqual(entries, [])
        self.assertLess(gate.evaluations[0]["sentiment_z63_delta_1d"], 0)
        self.assertGreater(gate.evaluations[0]["risk_z63_delta_1d"], 0)

        recovering = factors.copy()
        recovering.loc[0, ["sentiment_z63", "risk_z63"]] = [-1.5, 1.5]
        gate = MarketSentimentTransitionEntryGate(recovering)
        _, entries = gate.filter_review(
            self._Panel(), None, 1, [], ["sz000003"])
        self.assertEqual(entries, ["sz000003"])


if __name__ == "__main__":
    unittest.main()
