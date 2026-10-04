import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from scripts.run_intraday_shadow_session_v1 import (
    _is_collection_time, load_session_orders, load_watchlist, poll_session,
    prepare_session,
)
from tests.test_intraday_execution import approved
from tests.test_intraday_shadow import FakeAdapter
from tests.test_intraday_execution import bar


class IntradayShadowSessionTest(unittest.TestCase):

    def _paper_state(self, directory, orders, positions=None):
        path = Path(directory) / "paper_state.json"
        path.write_text(json.dumps({
            "active": {
                "orders": [asdict(item) for item in orders],
                "positions": positions or {},
            },
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

    def test_no_pending_buy_collects_sentinels_but_is_not_effective(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._paper_state(directory, [])
            session, orders, manifest = prepare_session(
                Path(directory) / "run", state, "a" * 64, 20250103,
                "2025-01-03T09:00:00+08:00",
                extra_symbols=["sh600519"])
            self.assertFalse(orders)
            self.assertEqual(
                "COLLECT_ONLY_NO_PENDING_BUYS", manifest["status"])
            self.assertFalse(manifest["effective_order_day"])
            self.assertEqual(["sh600519"], manifest["watchlist"])
            self.assertFalse((session / "M1" / "state.json").exists())

    def test_prepare_freezes_both_policy_states_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._paper_state(directory, [approved()])
            arguments = (
                Path(directory) / "run", state, "b" * 64, 20250103,
                "2025-01-03T09:00:00+08:00", ["sz000001"])
            first = prepare_session(*arguments)
            second = prepare_session(*arguments)
            self.assertEqual(first[2], second[2])
            self.assertTrue((first[0] / "M1" / "state.json").exists())
            self.assertTrue((first[0] / "M2" / "state.json").exists())

    def test_watchlist_combines_orders_positions_and_sentinels(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._paper_state(
                directory, [], positions={"sz000002": {}})
            symbols = load_watchlist(
                state, [approved()], ["300750", "sh600519"])
            self.assertEqual(
                ["sh600519", "sz000001", "sz000002", "sz300750"], symbols)

    def test_collection_clock_pauses_for_lunch(self):
        stamp = lambda value: __import__("pandas").Timestamp(
            "2025-01-03T{}+08:00".format(value))
        clocks = [stamp(value) for value in (
            "09:31:05", "11:30:30", "13:01:05", "15:01:30")]
        self.assertTrue(_is_collection_time(stamp("10:00:00"), *clocks))
        self.assertFalse(_is_collection_time(stamp("12:00:00"), *clocks))
        self.assertTrue(_is_collection_time(stamp("14:00:00"), *clocks))

    def test_shared_collection_feeds_both_policies_with_one_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._paper_state(directory, [approved()])
            session, _, manifest = prepare_session(
                Path(directory) / "run", state, "d" * 64, 20250103,
                "2025-01-03T09:00:00+08:00")
            adapter = FakeAdapter([
                bar("09:35"), bar("09:37", opening=10.2)])
            result = poll_session(
                session, Path(directory) / "bars", manifest["watchlist"],
                20250103,
                now=lambda: __import__("pandas").Timestamp(
                    "2025-01-03T10:31:30+08:00"), adapter=adapter)
            self.assertEqual(1, adapter.calls)
            self.assertEqual("FILLED", result["policies"]["M1"][
                "outcomes"]["order-1"]["state"])
            self.assertEqual("FILLED", result["policies"]["M2"][
                "outcomes"]["order-1"]["state"])


if __name__ == "__main__":
    unittest.main()
