"""Frozen context configuration and non-mutating shadow tests."""

import unittest
from pathlib import Path

from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from abupy.AlphaBu.ABuVCPContext import (
    build_context_shadow, evaluate_market_context,
    load_vcp_context_config, load_vcp_context_registry,
)


ROOT = Path(__file__).parents[1]


def market(**changes):
    row = {
        "universe_scope": "signal_eligible", "coverage_ratio": 0.99,
        "breadth_above_ma120": 0.60, "breadth_above_ma60": 0.60,
        "equal_weight_return_5d": 0.01, "new_low_20d_ratio": 0.03,
    }
    row.update(changes)
    return row


def intent(name, industry, score, side="buy"):
    return TradeIntent(
        intent_id=name, strategy_id="vcp_residual_v2", strategy_version="1",
        signal_asof=20250102, symbol=name, side=side, score=score,
        industry_asof=industry,
    )


class VCPContextTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.config = load_vcp_context_config(
            ROOT / "configs/selection/vcp_context_overlay_v1.json")

    def test_market_states_are_mutually_exclusive_and_fail_closed(self):
        self.assertEqual(evaluate_market_context(
            market(), self.config).state, "NORMAL")
        self.assertEqual(evaluate_market_context(
            market(breadth_above_ma120=0.50), self.config).state, "CAUTION")
        self.assertEqual(evaluate_market_context(
            market(new_low_20d_ratio=0.25), self.config).state, "RETREAT")
        unknown = evaluate_market_context(
            market(coverage_ratio=0.50), self.config)
        self.assertEqual(unknown.state, "UNKNOWN")
        self.assertEqual(unknown.new_risk_multiplier, 0.0)

    def test_shadow_does_not_mutate_intents_and_sells_remain_allowed(self):
        intents = [intent("a", 1, 0.9), intent("sell", 1, 0.1, "sell")]
        before = tuple(intents)
        rows = build_context_shadow(
            intents, market(new_low_20d_ratio=0.25),
            {1: {"coverage_ratio": 1.0,
                 "excess_return_20d_vs_market_rank": 0.8}},
            {"a": {"coverage_ratio": 1.0,
                   "residual_momentum_rank_within_industry": 0.8},
             "sell": {"coverage_ratio": 1.0,
                      "residual_momentum_rank_within_industry": 0.2}},
            self.config,
        )
        self.assertEqual(tuple(intents), before)
        self.assertEqual(rows[0]["market_shadow_action"], "REJECT_NEW_RISK")
        self.assertEqual(rows[1]["market_shadow_action"], "ALLOW_EXIT")

    def test_industry_and_leader_orders_are_separate(self):
        intents = [intent("a", 1, .9), intent("b", 2, .8), intent("c", 1, .7)]
        rows = build_context_shadow(
            intents, market(),
            {1: {"coverage_ratio": 1.0,
                 "excess_return_20d_vs_market_rank": 0.2},
             2: {"coverage_ratio": 1.0,
                 "excess_return_20d_vs_market_rank": 0.9}},
            {"a": {"coverage_ratio": 1.0,
                   "residual_momentum_rank_within_industry": 0.1},
             "b": {"coverage_ratio": 1.0,
                   "residual_momentum_rank_within_industry": 0.5},
             "c": {"coverage_ratio": 1.0,
                   "residual_momentum_rank_within_industry": 0.9}},
            self.config,
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(by_symbol["b"]["industry_shadow_rank"], 1)
        # Base industry sequence is [1,2,1]; leader ranking may swap a/c only.
        leader_order = sorted(rows, key=lambda row:
                              row["leader_fixed_industry_shadow_rank"])
        self.assertEqual([row["industry_id"] for row in leader_order], [1, 2, 1])
        self.assertEqual([row["symbol"] for row in leader_order], ["c", "b", "a"])

    def test_low_context_coverage_keeps_base_order_and_reason(self):
        rows = build_context_shadow(
            [intent("a", 1, .9)], market(),
            {1: {"coverage_ratio": .5,
                 "excess_return_20d_vs_market_rank": 1.0}}, {}, self.config)
        self.assertIsNone(rows[0]["industry_rank_value"])
        self.assertIn("INDUSTRY_CONTEXT_MISSING", rows[0]["reason_codes"])
        self.assertIn("INDUSTRY_LEADER_CONTEXT_MISSING", rows[0]["reason_codes"])

    def test_registry_is_complete_and_parameter_search_is_disabled(self):
        registry = load_vcp_context_registry(
            ROOT / "configs/selection/vcp_context_experiment_registry_v1.json")
        self.assertEqual(len(registry["hypotheses"]), 3)
        self.assertFalse(registry["parameter_search_allowed"])
        self.assertEqual(len(registry["registry_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()

