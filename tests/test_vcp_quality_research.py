"""Quality research metric tests."""
import unittest

import pandas as pd

from scripts.backtest_vcp_quality_rank_v1 import quality_intent_frame
from scripts.research_vcp_quality_rank_v1 import daily_rank_ic, selection_summary


class VCPQualityResearchTest(unittest.TestCase):

    def test_quality_frame_replaces_score_but_preserves_stop(self):
        shadow = pd.DataFrame([{
            "intent_id": "old", "strategy_id": "base", "strategy_version": "1",
            "signal_asof": 20260102, "symbol": "sz000001", "side": "buy",
            "score": .1, "signal_price_adjusted": 10., "signal_price_raw": 10.,
            "adjustment_factor_signal": 1., "initial_stop_adjusted": 9.,
            "initial_stop_raw": 9., "industry_asof": 1,
            "required_fields": "()", "missing_fields": "()",
            "max_gap_atr": 1., "valid_for_sessions": 1,
            "r_definition_version": "v1",
            "metadata": "{'max_buy_price_raw': 10.5, 'breakout_level': 9.8}",
        }])
        predictions = pd.DataFrame([{"intent_id": "old", "quality_score": .8}])
        result = quality_intent_frame(shadow, predictions, "quality_v1")
        self.assertEqual(result.iloc[0].score, .8)
        self.assertEqual(result.iloc[0].initial_stop_raw, 9.)
        self.assertEqual(result.iloc[0].strategy_id, "quality_v1")

    def test_daily_ic_and_selection_compare_with_same_day_candidates(self):
        rows = []
        for date in (20260102, 20260103):
            for value in range(6):
                rows.append({
                    "signal_asof": date, "quality_score": value,
                    "legacy_score": 5-value,
                    "excess_return_20d": value/100,
                    "false_breakout_5d": value < 2,
                })
        frame = pd.DataFrame(rows)
        ic = daily_rank_ic(frame, "quality_score", "excess_return_20d")
        self.assertEqual(len(ic), 2)
        self.assertTrue((ic.ic == 1).all())
        summary = selection_summary(frame, topk=2)
        self.assertTrue((summary.quality_return > summary.candidate_return).all())
        self.assertTrue((summary.legacy_return < summary.candidate_return).all())


if __name__ == "__main__":
    unittest.main()
