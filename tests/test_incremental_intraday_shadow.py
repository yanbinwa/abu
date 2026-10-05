import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from abupy.AlphaBu.ABuIntradayShadow import (
    IntradayShadowRunner, initialize_shadow_state,
)
from tests.test_intraday_execution import approved, bar
from tests.test_intraday_shadow import FakeAdapter


class IncrementalIntradayShadowTest(unittest.TestCase):

    def test_snapshot_mode_is_incremental_and_rejects_sequence_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            order = approved()
            initialize_shadow_state(
                state_dir, [order], "M1", "m5" * 32,
                created_at="2025-01-03T09:00:00+08:00")
            runner = IntradayShadowRunner(
                state_dir, FakeAdapter([]), Path(directory) / "bars")
            first = SimpleNamespace(
                snapshot_id="minute-1", sequence_no=1,
                previous_snapshot_id=None,
                decision_cutoff="2025-01-03T09:35:05+08:00",
                events_by_symbol={order.symbol: (bar("09:35"),)})
            state = runner.poll_once(snapshot_batch=first)
            self.assertEqual(1, state["last_consumed_minute_sequence"])
            self.assertEqual("CANDIDATE", state["outcomes"][order.order_id]["state"])
            repeated = runner.poll_once(snapshot_batch=first)
            self.assertEqual(1, repeated["last_consumed_minute_sequence"])
            self.assertEqual(len(state["polls"]), len(repeated["polls"]))
            self.assertEqual(len(state["order_events"]),
                             len(repeated["order_events"]))

            gap = SimpleNamespace(
                snapshot_id="minute-3", sequence_no=3,
                previous_snapshot_id="minute-1",
                decision_cutoff="2025-01-03T09:37:05+08:00",
                events_by_symbol={})
            with self.assertRaisesRegex(ValueError, "sequence gap"):
                runner.poll_once(snapshot_batch=gap)
            second = SimpleNamespace(
                snapshot_id="minute-2", sequence_no=2,
                previous_snapshot_id="minute-1",
                decision_cutoff="2025-01-03T09:37:05+08:00",
                events_by_symbol={
                    order.symbol: (bar("09:37", opening=10.2),)})
            state = runner.poll_once(snapshot_batch=second)
            self.assertEqual(2, state["last_consumed_minute_sequence"])
            self.assertEqual("FILLED", state["outcomes"][order.order_id]["state"])
            self.assertEqual(0, runner.adapter.calls)


if __name__ == "__main__":
    unittest.main()
