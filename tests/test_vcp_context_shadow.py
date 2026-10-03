"""Vectorized context-shadow attachment tests."""

import unittest
from pathlib import Path

import pandas as pd

from abupy.AlphaBu.ABuVCPContext import load_vcp_context_config
from scripts.build_vcp_context_shadow import attach_context_frames


class VCPContextShadowTest(unittest.TestCase):

    def test_attachment_is_left_joined_and_never_drops_base_intents(self):
        config = load_vcp_context_config(
            Path(__file__).parents[1] /
            "configs/selection/vcp_context_overlay_v1.json")
        intents = pd.DataFrame([
            {"intent_id": "a", "strategy_id": "vcp_residual_v2",
             "signal_asof": 20250102, "symbol": "sz000001", "side": "buy",
             "score": .8, "industry_asof": 1, "metadata": "{}"},
            {"intent_id": "b", "strategy_id": "vcp_residual_v2",
             "signal_asof": 20250102, "symbol": "sz000002", "side": "buy",
             "score": .7, "industry_asof": 2, "metadata": "{}"},
        ])
        breadth = pd.DataFrame([{
            "trade_date": 20250102, "universe_scope": "signal_eligible",
            "coverage_ratio": .99, "breadth_above_ma120": .6,
            "breadth_above_ma60": .6, "equal_weight_return_5d": .01,
            "new_low_20d_ratio": .03,
        }])
        industry = pd.DataFrame([{
            "trade_date": 20250102, "industry_id": 1, "coverage_ratio": 1.,
            "excess_return_20d_vs_market_rank": .9,
        }])
        leaders = pd.DataFrame([{
            "trade_date": 20250102, "symbol": "sz000001", "industry_id": 1,
            "coverage_ratio": 1.,
            "residual_momentum_rank_within_industry": .9,
        }])
        result = attach_context_frames(
            intents, breadth, industry, leaders, config)
        self.assertEqual(result.intent_id.tolist(), ["a", "b"])
        self.assertEqual(result.market_state.tolist(), ["NORMAL", "NORMAL"])
        missing = result.set_index("intent_id").loc["b"]
        self.assertIn("INDUSTRY_CONTEXT_MISSING", missing.context_reason_codes)
        self.assertIn("INDUSTRY_LEADER_CONTEXT_MISSING", missing.context_reason_codes)


if __name__ == "__main__":
    unittest.main()

