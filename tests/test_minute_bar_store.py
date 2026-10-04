import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore
from abupy.MarketBu.ABuRealtimeMarket import MinuteBarEvent


def event(end="09:31:00", available="09:31:02", close=10.1,
          volume=1000, amount=None):
    date = "2026-10-09T"
    end_at = pd.Timestamp(date + end + "+08:00")
    start = (end_at - pd.Timedelta(minutes=1)).isoformat()
    return MinuteBarEvent(
        symbol="sh600000", interval_minutes=1,
        source_timestamp=date + end + "+08:00",
        bar_start=start, bar_end=end_at.isoformat(),
        request_started_at=date + available + "+08:00",
        received_at=date + available + "+08:00",
        available_at=date + available + "+08:00",
        open_raw=10.0, high_raw=max(10.2, close), low_raw=9.9,
        close_raw=close, volume_shares=volume, amount_raw=amount,
        source="fixture", is_complete=True,
        quality_codes=(("AMOUNT_MISSING",) if amount is None else ()),
    )


class MinuteBarStoreTest(unittest.TestCase):

    def test_append_is_immutable_idempotent_and_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            first = store.append([event()])[0]
            duplicate = store.append([event(available="09:31:05")])[0]
            revised = store.append([event(
                available="09:31:10", close=10.15)])[0]
            self.assertEqual(1, first["appended"])
            self.assertEqual(0, duplicate["appended"])
            self.assertEqual(1, revised["appended"])
            records = store.read("sh600000", "20261009", source="fixture",
                                 latest_revision=False)
            self.assertEqual([1, 2], [item.revision for item in records])
            batches = list(Path(directory).glob("**/batches/*.jsonl"))
            self.assertEqual(2, len(batches))
            manifests = list(Path(directory).glob("**/manifests/*.json"))
            self.assertEqual(2, len(manifests))

    def test_asof_read_keeps_first_visible_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            store.append([event()])
            store.append([event(available="09:31:10", close=10.15)])
            early = store.read(
                "sh600000", "20261009", source="fixture",
                as_of="2026-10-09T09:31:05+08:00")
            late = store.read(
                "sh600000", "20261009", source="fixture",
                as_of="2026-10-09T09:31:11+08:00")
            self.assertEqual(10.1, early[0].close_raw)
            self.assertEqual(10.15, late[0].close_raw)

    def test_audit_accepts_amount_missing_but_reports_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            store.append([event("09:31:00"), event("09:32:00")])
            audit = store.audit("sh600000", "20261009", source="fixture")
            self.assertTrue(audit["healthy"])
            self.assertEqual(2, audit["amount_missing_count"])
            store.append([event("09:34:00")])
            audit = store.audit("sh600000", "20261009", source="fixture")
            self.assertFalse(audit["healthy"])
            self.assertEqual(1, audit["gap_count"])

    def test_audit_classifies_lunch_and_closing_auction_as_scheduled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            store.append([
                event("11:30:00", available="11:30:02"),
                event("13:01:00", available="13:01:02"),
            ])
            audit = store.audit("sh600000", "20261009", source="fixture")
            self.assertTrue(audit["healthy"])
            self.assertEqual(0, audit["gap_count"])
            self.assertEqual(1, audit["scheduled_gap_count"])
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            store.append([
                event("14:57:00", available="14:57:02"),
                event("15:00:00", available="15:00:02"),
            ])
            audit = store.audit("sh600000", "20261009", source="fixture")
            self.assertTrue(audit["healthy"])
            self.assertEqual(0, audit["gap_count"])
            self.assertEqual(1, audit["scheduled_gap_count"])

    def test_current_pointer_names_verified_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            result = store.append([event()])[0]
            pointer = next(Path(directory).glob("**/CURRENT"))
            name = pointer.read_text(encoding="utf-8").strip()
            self.assertIn(result["manifest_sha256"], name)
            payload = json.loads((pointer.parent / "manifests" / name).read_text(
                encoding="utf-8"))
            self.assertEqual(result["manifest_sha256"],
                             payload["manifest_sha256"])

    def test_raw_provider_response_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MinuteBarStore(directory)
            raw = __import__("pandas").DataFrame({
                "时间": ["2026-10-09 09:31:00"], "成交量": [100]})
            first = store.append_raw_response(
                raw, "fixture", "sh600000", 1,
                "2026-10-09T09:31:01+08:00",
                "2026-10-09T09:31:02+08:00")
            second = store.append_raw_response(
                raw, "fixture", "sh600000", 1,
                "2026-10-09T09:31:01+08:00",
                "2026-10-09T09:31:02+08:00")
            self.assertEqual(first, second)
            self.assertTrue(Path(first["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
