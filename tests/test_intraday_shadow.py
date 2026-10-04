import tempfile
import unittest
from pathlib import Path

import pandas as pd

from abupy.AlphaBu.ABuIntradayShadow import (
    IntradayShadowRunner, ShadowQualityThresholds,
    evaluate_shadow_quality, initialize_shadow_state, read_shadow_state,
)
from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore
from tests.test_intraday_execution import approved, bar


def frame(events):
    rows = []
    for item in events:
        rows.append({
            "symbol": item.symbol,
            "timestamp": pd.Timestamp(item.source_timestamp),
            "received_at": pd.Timestamp(item.received_at),
            "interval_minutes": item.interval_minutes,
            "open": item.open_raw, "high": item.high_raw,
            "low": item.low_raw, "close": item.close_raw,
            "volume": item.volume_shares, "amount": item.amount_raw,
            "average": float("nan"), "change_pct": float("nan"),
            "turnover_rate": float("nan"),
            "bar_complete": item.is_complete, "source": item.source,
            "source_timestamp": pd.Timestamp(item.source_timestamp),
            "bar_start": pd.Timestamp(item.bar_start),
            "bar_end": pd.Timestamp(item.bar_end),
            "request_started_at": pd.Timestamp(item.request_started_at),
            "available_at": pd.Timestamp(item.available_at),
            "open_raw": item.open_raw, "high_raw": item.high_raw,
            "low_raw": item.low_raw, "close_raw": item.close_raw,
            "volume_shares": item.volume_shares,
            "amount_raw": item.amount_raw, "revision": item.revision,
            "is_complete": item.is_complete,
            "quality_codes": item.quality_codes,
        })
    return pd.DataFrame(rows)


class FakeAdapter(object):
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def minute_bars(self, *args, **kwargs):
        self.calls += 1
        return frame(self.events)

    def health(self, symbol=None):
        class Health(object):
            def to_dict(self):
                return {"connected": True, "data_fresh": True}
        return Health()


class IntradayShadowTest(unittest.TestCase):

    def test_poll_is_idempotent_and_has_no_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            store = MinuteBarStore(Path(directory) / "bars")
            order = approved()
            initialize_shadow_state(
                state_dir, [order], "M1", "a" * 64,
                created_at="2025-01-03T09:00:00+08:00")
            adapter = FakeAdapter([
                bar("09:35"), bar("09:37", opening=10.2)])
            runner = IntradayShadowRunner(
                state_dir, adapter, store,
                now=lambda: pd.Timestamp(
                    "2025-01-03T10:31:30+08:00"))
            first = runner.poll_once()
            second = runner.poll_once()
            self.assertFalse(first["broker_connected"])
            self.assertEqual("FILLED", first["outcomes"][order.order_id]["state"])
            self.assertEqual(len(first["order_events"]),
                             len(second["order_events"]))
            self.assertEqual(1, len(list(
                (Path(directory) / "bars").glob("**/batches/*.jsonl"))))
            self.assertEqual(read_shadow_state(state_dir)["policy_id"], "M1")

    def test_frozen_inputs_cannot_be_tampered(self):
        with tempfile.TemporaryDirectory() as directory:
            initialize_shadow_state(directory, [approved()], "M2", "b" * 64)
            path = Path(directory) / "state.json"
            state = __import__("json").loads(path.read_text(encoding="utf-8"))
            state["policy_id"] = "M1"
            path.write_text(__import__("json").dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen inputs"):
                read_shadow_state(directory)

    def test_prefetched_bars_do_not_request_provider_again(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            store = MinuteBarStore(Path(directory) / "bars")
            order = approved()
            events = [bar("09:35"), bar("09:37", opening=10.2)]
            store.append(events)
            initialize_shadow_state(
                state_dir, [order], "M1", "c" * 64,
                created_at="2025-01-03T09:00:00+08:00")
            adapter = FakeAdapter(events)
            state = IntradayShadowRunner(
                state_dir, adapter, store,
                now=lambda: pd.Timestamp(
                    "2025-01-03T10:31:30+08:00")).poll_once(
                        prefetched_health={order.symbol: {
                            "symbol": order.symbol, "status": "OK",
                            "health": {"data_fresh": True},
                        }})
            self.assertEqual(0, adapter.calls)
            self.assertEqual("FILLED", state["outcomes"][order.order_id]["state"])

    def test_quality_gate_needs_every_day_and_every_threshold(self):
        thresholds = ShadowQualityThresholds(
            min_window_coverage=0.99, min_fresh_bar_availability=0.99,
            max_p95_latency_ms=2000, max_p99_latency_ms=5000,
            max_stale_rate=0.01, max_fallback_rate=0.05,
            max_duplicate_rate=0.0, min_recovery_rate=1.0)
        good = {
            "trade_date": "20251009", "window_coverage": 1.0,
            "fresh_bar_availability": 1.0, "p95_latency_ms": 1000,
            "p99_latency_ms": 2000, "stale_rate": 0.0,
            "fallback_rate": 0.0, "duplicate_rate": 0.0,
            "recovery_rate": 1.0, "audit_complete": True,
            "all_symbols_have_health": True,
        }
        self.assertTrue(evaluate_shadow_quality(
            [dict(good, trade_date=str(i)) for i in range(20)],
            thresholds)["passed"])
        bad = [dict(good, trade_date=str(i)) for i in range(20)]
        bad[5]["fresh_bar_availability"] = 0.5
        self.assertFalse(evaluate_shadow_quality(bad, thresholds)["passed"])


if __name__ == "__main__":
    unittest.main()
