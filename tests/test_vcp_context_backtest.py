"""Context ablation ordering and risk multiplier tests."""

import unittest
from types import SimpleNamespace

import pandas as pd

from scripts.backtest_vcp_context_v1 import (
    order_context_candidates, requested_quantity_for_context,
)


class VCPContextBacktestTest(unittest.TestCase):

    def test_external_ranking_column_does_not_require_context_fields(self):
        frame = pd.DataFrame([
            {"symbol": "a", "model_score": .1},
            {"symbol": "b", "model_score": .9},
        ])
        result = order_context_candidates(
            frame, "external_model", ranking_column="model_score")
        self.assertEqual(result.symbol.tolist(), ["b", "a"])

    def test_industry_and_leader_ablations_change_one_ordering_layer(self):
        frame = pd.DataFrame([
            {"symbol": "a", "score": .9, "industry_rank_value": .2,
             "leader_rank_value": .1},
            {"symbol": "b", "score": .8, "industry_rank_value": .9,
             "leader_rank_value": .2},
            {"symbol": "c", "score": .7, "industry_rank_value": .9,
             "leader_rank_value": .8},
        ])
        self.assertEqual(order_context_candidates(
            frame, "baseline_replay").symbol.tolist(), ["a", "b", "c"])
        self.assertEqual(order_context_candidates(
            frame, "industry_rank").symbol.tolist(), ["b", "c", "a"])
        self.assertEqual(order_context_candidates(
            frame, "industry_leader_rank").symbol.tolist(), ["c", "b", "a"])

    def test_context_multiplier_scales_requested_r_quantity(self):
        risk = SimpleNamespace(
            config=SimpleNamespace(single_trade_risk_fraction=.0025),
            _state=lambda executor, day: (100_000., 0, 0, {}, 0))
        intent = SimpleNamespace(
            metadata={"max_buy_price_raw": 10.0}, initial_stop_raw=9.0)
        self.assertIsNone(requested_quantity_for_context(
            risk, object(), intent, 1, 1.0))
        self.assertEqual(requested_quantity_for_context(
            risk, object(), intent, 1, .5), 100)
        self.assertEqual(requested_quantity_for_context(
            risk, object(), intent, 1, 0), 0)


if __name__ == "__main__":
    unittest.main()
