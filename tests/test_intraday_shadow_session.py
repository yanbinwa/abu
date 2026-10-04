import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from scripts.run_intraday_shadow_session_v1 import (
    load_session_orders, prepare_session,
)
from tests.test_intraday_execution import approved


class IntradayShadowSessionTest(unittest.TestCase):

    def _paper_state(self, directory, orders):
        path = Path(directory) / "paper_state.json"
        path.write_text(json.dumps({
            "active": {"orders": [asdict(item) for item in orders]},
        }), encoding="utf-8")
        return path

    def test_zero_valid_session_is_bound_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            original = replace(approved(), valid_session=0)
            state = self._paper_state(directory, [original])
            orders = load_session_orders(state, 20250103)
            self.assertEqual(20250103, orders[0].valid_session)
            source = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(0, source["active"]["orders"][0]["valid_session"])

    def test_no_pending_buy_is_explicitly_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._paper_state(directory, [])
            session, orders, manifest = prepare_session(
                Path(directory) / "run", state, "a" * 64, 20250103,
                "2025-01-03T09:00:00+08:00")
            self.assertFalse(orders)
            self.assertEqual("SKIPPED_NO_PENDING_BUYS", manifest["status"])
            self.assertFalse((session / "M1" / "state.json").exists())

    def test_prepare_freezes_both_policy_states_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._paper_state(directory, [approved()])
            arguments = (
                Path(directory) / "run", state, "b" * 64, 20250103,
                "2025-01-03T09:00:00+08:00")
            first = prepare_session(*arguments)
            second = prepare_session(*arguments)
            self.assertEqual(first[2], second[2])
            self.assertTrue((first[0] / "M1" / "state.json").exists())
            self.assertTrue((first[0] / "M2" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
