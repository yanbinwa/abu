# -*- encoding: utf-8 -*-
import unittest
from types import SimpleNamespace

from abupy.AlphaBu.ABuScaleOutPolicy import (
    RMultipleScaleOutPolicy, ScaleOutConfig,
)


class ScaleOutPolicyTest(unittest.TestCase):

    def setUp(self):
        self.policy = RMultipleScaleOutPolicy()
        self.exit_state = SimpleNamespace(
            initial_stop_adjusted=8.0, initial_r_adjusted=2.0)
        self.policy.register_entry("sz000001", 1000)

    def test_two_stages_use_cumulative_reference_quantity(self):
        first = self.policy.evaluate("sz000001", 12.0, self.exit_state, 1000)
        self.assertEqual(first["reason"], "TAKE_PROFIT_1R")
        self.assertEqual(first["quantity"], 200)
        self.policy.record_fill("sz000001", first["reason"], first["quantity"])
        self.policy.register_add("sz000001", 200)
        second = self.policy.evaluate("sz000001", 14.0, self.exit_state, 1000)
        self.assertEqual(second["reason"], "TAKE_PROFIT_2R")
        self.assertEqual(second["quantity"], 400)
        self.policy.record_fill("sz000001", second["reason"], second["quantity"])
        self.assertIsNone(
            self.policy.evaluate("sz000001", 15.0, self.exit_state, 600))

    def test_never_sells_last_board_lot(self):
        policy = RMultipleScaleOutPolicy()
        policy.register_entry("sz000001", 100)
        self.assertIsNone(
            policy.evaluate("sz000001", 14.0, self.exit_state, 100))

    def test_cancel_releases_pending_stage(self):
        first = self.policy.evaluate("sz000001", 12.0, self.exit_state, 1000)
        self.assertIsNotNone(first)
        self.assertIsNone(
            self.policy.evaluate("sz000001", 12.0, self.exit_state, 1000))
        self.policy.record_cancel("sz000001")
        self.assertIsNotNone(
            self.policy.evaluate("sz000001", 12.0, self.exit_state, 1000))

    def test_two_r_only_uses_two_r_reason_and_sells_25_percent(self):
        policy = RMultipleScaleOutPolicy(ScaleOutConfig(
            policy_id="scale_out_2r_v1", trigger_r_multiples=(2.0,),
            cumulative_exit_fractions=(0.25,)))
        policy.register_entry("sz000001", 1000)
        self.assertIsNone(
            policy.evaluate("sz000001", 12.0, self.exit_state, 1000))
        decision = policy.evaluate("sz000001", 14.0, self.exit_state, 1000)
        self.assertEqual(decision["reason"], "TAKE_PROFIT_2R")
        self.assertEqual(decision["quantity"], 200)

    def test_two_r_half_exit_sells_half_and_keeps_remainder_active(self):
        policy = RMultipleScaleOutPolicy(ScaleOutConfig(
            policy_id="scale_out_2r_50_v1", trigger_r_multiples=(2.0,),
            cumulative_exit_fractions=(0.50,)))
        policy.register_entry("sz000001", 1000)
        decision = policy.evaluate("sz000001", 14.0, self.exit_state, 1000)
        self.assertEqual(decision["reason"], "TAKE_PROFIT_2R")
        self.assertEqual(decision["quantity"], 500)


if __name__ == "__main__":
    unittest.main()
