import unittest
from dataclasses import replace

from abupy.AlphaBu.ABuIntradayExecution import IntradayExecutionConfig
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder
from abupy.ServiceBu import MinuteSnapshotBatch
from abupy.ServiceBu.ABuExecutionReplay import D0OpenInput, ExecutionReplayHarness
from tests.test_intraday_execution import bar


class ExecutionReplayHarnessTest(unittest.TestCase):

    def order(self, order_id="order-a"):
        return ApprovedOrder(
            order_id=order_id, intent_id="intent-" + order_id,
            strategy_id="fixture", strategy_version="1",
            symbol="sh600000", side="buy", quantity=100,
            created_asof=20250102, valid_session=20250103,
            max_buy_price_raw=11.0, position_effect="OPEN")

    def batch(self, sequence, bars, previous=None):
        return MinuteSnapshotBatch(
            snapshot_id="minute-{}".format(sequence),
            stream_id="market-minute:20261009:1", sequence_no=sequence,
            previous_snapshot_id=previous,
            decision_cutoff=bars[-1].available_at,
            watchlist_id="w", watchlist_version=1, manifest={},
            events_by_symbol={"sh600000": tuple(bars)})

    def test_fixed_batches_replay_d0_m1_m2_deterministically(self):
        first = self.batch(1, (replace(bar("09:35", volume=5_000),
                                       symbol="sh600000"),))
        second = self.batch(2, (replace(bar("09:37", opening=10.2),
                                        symbol="sh600000"),), "minute-1")
        harness = ExecutionReplayHarness(
            IntradayExecutionConfig(slippage_bps=0))
        case = {"order": self.order(), "trading_session": 20250103,
                "batches": (first, second),
                "d0_open": D0OpenInput(10.0, True, 11.0)}
        first_report = harness.replay_portfolio((case,))
        second_report = harness.replay_portfolio((case,))
        self.assertEqual(first_report, second_report)
        result = first_report["orders"][0]
        self.assertEqual("FILLED", result["D0"]["state"])
        self.assertEqual("FILLED", result["M1"]["state"])
        self.assertEqual("FILLED", result["M2"]["state"])

    def test_gap_fails_closed(self):
        bad = self.batch(2, (replace(bar("09:35"), symbol="sh600000"),))
        with self.assertRaisesRegex(ValueError, "sequence gap"):
            ExecutionReplayHarness().replay_order(
                self.order(), 20250103, (bad,), D0OpenInput(10.0))

    def test_portfolio_order_is_stable(self):
        one = self.batch(1, (replace(bar("10:32"), symbol="sh600000"),))
        harness = ExecutionReplayHarness()
        cases = tuple({
            "order": self.order(order_id), "trading_session": 20250103,
            "batches": (one,), "d0_open": D0OpenInput(12.0),
        } for order_id in ("order-b", "order-a"))
        report = harness.replay_portfolio(cases)
        self.assertEqual(["order-a", "order-b"], [
            item["order_id"] for item in report["orders"]])


if __name__ == "__main__":
    unittest.main()
