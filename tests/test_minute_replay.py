import tempfile
import unittest

from abupy.AlphaBu.ABuMinuteReplay import (
    EmpiricalDelayModel, FixedDelayModel, ZeroDelayModel,
    run_paired_replay, write_paired_replay,
)
from tests.test_intraday_execution import approved, bar


class MinuteReplayTest(unittest.TestCase):

    def inputs(self):
        order = approved(quantity=100)
        bars = [
            bar("09:35", volume=100_000),
            bar("09:36", opening=10.1, volume=100_000),
            bar("09:37", opening=10.2, volume=100_000),
            bar("09:38", opening=10.3, volume=100_000),
        ]
        return order, bars

    def test_paired_replay_is_deterministic_and_keeps_same_order(self):
        order, bars = self.inputs()
        arguments = dict(
            orders=[order], bars_by_symbol={order.symbol: bars},
            d0_by_order={order.order_id: {
                "status": "filled", "reason_code": "",
                "fill_price_raw": 10.0}},
            latency_model=FixedDelayModel(2_000),
            source_snapshot_hash="a" * 64,
            data_manifest_hash="b" * 64,
        )
        first = run_paired_replay(**arguments)
        second = run_paired_replay(**arguments)
        self.assertEqual(first, second)
        records, manifest = first
        self.assertEqual(["D0", "M1", "M2"],
                         [item.policy_id for item in records])
        self.assertEqual(1, len({item.approved_order_sha256
                                for item in records}))
        with tempfile.TemporaryDirectory() as directory:
            write_paired_replay(directory, records, manifest)
            with self.assertRaises(FileExistsError):
                write_paired_replay(directory, records, manifest)

    def test_latency_changes_candidate_without_mutating_market_time(self):
        order, bars = self.inputs()
        common = dict(
            orders=[order], bars_by_symbol={order.symbol: bars},
            d0_by_order={order.order_id: {
                "status": "filled", "fill_price_raw": 10.0}})
        zero, _ = run_paired_replay(
            **common, latency_model=ZeroDelayModel())
        delayed, _ = run_paired_replay(
            **common, latency_model=FixedDelayModel(61_000))
        zero_m1 = next(item for item in zero if item.policy_id == "M1")
        delayed_m1 = next(item for item in delayed if item.policy_id == "M1")
        self.assertEqual(10.1 * 1.0025, zero_m1.fill_price_raw)
        self.assertEqual(10.3 * 1.0025, delayed_m1.fill_price_raw)
        self.assertTrue(bars[0].available_at.endswith("09:35:02+08:00"))

    def test_empirical_latency_selection_is_key_stable(self):
        _, bars = self.inputs()
        model = EmpiricalDelayModel(
            [100, 200, 300], sample_manifest_sha256="c" * 64, seed=7)
        self.assertEqual(model.delay_ms(bars[0]), model.delay_ms(bars[0]))
        self.assertEqual(model.to_dict()["sample_manifest_sha256"], "c" * 64)


if __name__ == "__main__":
    unittest.main()
