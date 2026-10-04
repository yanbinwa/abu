# -*- encoding: utf-8 -*-
import unittest
from dataclasses import replace

from abupy.AlphaBu.ABuPositionAddPolicy import (
    CompositePositionAddPolicy, MarketTrendGatePolicy, RebreakoutPolicy,
    TurtleAtrPolicy,
)
from tests.test_protected_winner_policy import ProtectedWinnerPolicyTest


class PositionAddCompositeTest(unittest.TestCase):

    def setUp(self):
        self.context = ProtectedWinnerPolicyTest()._context()

    def test_rebreakout_window_uses_prior_value_supplied_without_current_day(self):
        context = replace(self.context, adjusted_market_window={
            **self.context.adjusted_market_window,
            "prior20_high_adjusted": 11.0})
        evaluation = RebreakoutPolicy().evaluate(context)
        self.assertTrue(evaluation.triggered)
        self.assertTrue(evaluation.evaluated_inputs["window_excludes_signal_day"])

    def test_turtle_atr_uses_frozen_half_atr_advance(self):
        evaluation = TurtleAtrPolicy().evaluate(self.context)
        self.assertTrue(evaluation.triggered)
        self.assertEqual(evaluation.evaluated_inputs["trigger_threshold"], 10.5)

    def test_all_of_requires_every_member_and_merges_strict_caps(self):
        context = replace(self.context, adjusted_market_window={
            **self.context.adjusted_market_window,
            "prior20_high_adjusted": 11.0})
        policy = CompositePositionAddPolicy(
            (RebreakoutPolicy(), TurtleAtrPolicy()), "ALL_OF")
        evaluation = policy.evaluate(context)
        self.assertTrue(evaluation.triggered)
        self.assertEqual(evaluation.proposal.logical_order_id,
                         policy.evaluate(context).proposal.logical_order_id)
        failed = replace(context, adjusted_market_window={
            **context.adjusted_market_window, "prior20_high_adjusted": 12.0})
        self.assertFalse(policy.evaluate(failed).triggered)

    def test_any_of_and_priority_create_one_proposal(self):
        context = replace(self.context, adjusted_market_window={
            **self.context.adjusted_market_window,
            "prior20_high_adjusted": 12.0})
        for mode in ("ANY_OF", "PRIORITY"):
            result = CompositePositionAddPolicy(
                (RebreakoutPolicy(), TurtleAtrPolicy()), mode).evaluate(context)
            self.assertTrue(result.triggered)
            self.assertIsNotNone(result.proposal)

    def test_market_trend_gate_passes_only_above_pit_ma200(self):
        policy = MarketTrendGatePolicy(TurtleAtrPolicy())
        up = replace(self.context, portfolio_risk_snapshot={
            **self.context.portfolio_risk_snapshot,
            "benchmark_close": 3200.0, "benchmark_ma200": 3100.0})
        passed = policy.evaluate(up)
        self.assertTrue(passed.triggered)
        self.assertIn("BENCHMARK_ABOVE_MA200", passed.proposal.reason_codes)
        down = replace(self.context, portfolio_risk_snapshot={
            **self.context.portfolio_risk_snapshot,
            "benchmark_close": 3000.0, "benchmark_ma200": 3100.0})
        rejected = policy.evaluate(down)
        self.assertFalse(rejected.triggered)
        self.assertIn("BENCHMARK_NOT_ABOVE_MA200", rejected.reason_codes)

    def test_market_trend_gate_fails_closed_when_market_field_missing(self):
        evaluation = MarketTrendGatePolicy(TurtleAtrPolicy()).evaluate(
            self.context)
        self.assertFalse(evaluation.triggered)
        self.assertIn("benchmark_close", evaluation.missing_fields)


if __name__ == "__main__":
    unittest.main()
