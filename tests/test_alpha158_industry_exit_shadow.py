from types import SimpleNamespace
import unittest

import numpy as np

from scripts.run_alpha158_industry_exit_shadow_v1 import build_observations


class FakeContext:
    def __init__(self, reason="INDUSTRY_EXHAUSTION_EXIT"):
        self.reason = reason
        self.industry_features = {
            name: np.array([[value]], dtype=float)
            for name, value in {
                "return_5d": 0.2,
                "intraday_return": -0.03,
                "amount_ratio_20": 2.0,
                "advance_ratio": 0.3,
                "return_hot_threshold": 0.1,
                "intraday_reversal_threshold": -0.02,
                "return_retreat_threshold": -0.1,
                "advance_retreat_threshold": 0.2,
                "amount_hot_threshold": 1.5,
                "amount_retreat_threshold": 1.1,
            }.items()
        }

    def industry_signal(self, day, symbol):
        return self.reason, 0


class IndustryExitShadowTest(unittest.TestCase):
    def panel(self):
        return SimpleNamespace(
            dates=np.array([20261008]),
            symbols=["sz000001"],
            symbol_index={"sz000001": 0},
            industry=np.array([[0]]),
        )

    def state(self, trailing=True, pending=None):
        position = SimpleNamespace(quantity=100)
        executor = SimpleNamespace(
            positions={"sz000001": position}, orders=pending or [])
        account = {
            "executor": executor,
            "exit_states": {
                "sz000001": SimpleNamespace(trailing_enabled=trailing)},
        }
        return {"accounts": {"event_exit_only": account}}

    def test_eligible_signal_is_observed_without_mutation(self):
        state = self.state()
        before = dict(state["accounts"]["event_exit_only"]["executor"].positions)
        rows = build_observations(
            self.panel(), state, FakeContext(), 20261008)
        self.assertTrue(rows[0]["i1_triggered"])
        self.assertTrue(rows[0]["incremental_shadow_exit"])
        self.assertEqual(
            before,
            state["accounts"]["event_exit_only"]["executor"].positions)

    def test_base_sell_blocks_incremental_attribution(self):
        order = SimpleNamespace(
            side="sell", symbol="sz000001", reason="TRAILING_STOP")
        rows = build_observations(
            self.panel(), self.state(pending=[order]),
            FakeContext(), 20261008)
        self.assertTrue(rows[0]["i1_triggered"])
        self.assertTrue(rows[0]["base_sell_pending"])
        self.assertFalse(rows[0]["incremental_shadow_exit"])

    def test_signal_requires_existing_trailing_activation(self):
        rows = build_observations(
            self.panel(), self.state(trailing=False),
            FakeContext(), 20261008)
        self.assertFalse(rows[0]["i1_triggered"])
        self.assertFalse(rows[0]["incremental_shadow_exit"])


if __name__ == "__main__":
    unittest.main()
