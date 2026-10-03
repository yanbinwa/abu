"""External-score to intent adapter tests."""
import unittest

from abupy.AlphaBu.ABuScoreToIntent import rerank_intents
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


def _intent(identifier, symbol, score):
    return TradeIntent(
        intent_id=identifier, strategy_id="frozen", strategy_version="1",
        signal_asof=20260102, symbol=symbol, score=score,
        signal_price_adjusted=10.0, signal_price_raw=11.0,
        adjustment_factor_signal=1.1, initial_stop_adjusted=9.0,
        initial_stop_raw=9.9, metadata={"breakout_level": 9.8},
    )


class ScoreToIntentTest(unittest.TestCase):

    def test_rerank_preserves_execution_fields_and_versions_source(self):
        source = [_intent("old-a", "sz000001", .9),
                  _intent("old-b", "sz000002", .1)]
        ranked = rerank_intents(
            source, {"old-a": -.5, "old-b": .7}, "quality_v1")
        self.assertEqual([item.symbol for item in ranked],
                         ["sz000002", "sz000001"])
        self.assertEqual(ranked[0].strategy_id, "quality_v1")
        self.assertEqual(ranked[0].initial_stop_raw, 9.9)
        self.assertEqual(ranked[0].metadata["source_intent_id"], "old-b")
        self.assertEqual(ranked[0].metadata["source_score"], .1)

    def test_missing_or_nonfinite_score_is_rejected(self):
        source = [_intent("a", "sz000001", 1),
                  _intent("b", "sz000002", 2)]
        self.assertEqual(
            [item.symbol for item in rerank_intents(
                source, {"a": float("nan")}, "quality_v1")], [])


if __name__ == "__main__":
    unittest.main()
