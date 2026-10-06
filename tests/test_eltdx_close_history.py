"""Retrospective eltdx close-history cache tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from scripts.download_eltdx_close_history import (
    download_history, month_windows,
)


class Row:
    def __init__(self, date, code, status="limit_up"):
        self.trading_date_value = str(date)
        self.code = code
        self.full_code = "sz" + code
        self.name = "测试"
        self.status = status
        self.board_level = 1
        self.highest_board_level = 2
        self.industry = "测试行业"
        self.limit_reason = "测试原因"
        self.limit_reason_extra = ""
        self.seal_amount = 1000
        self.limit_time = "10:00:00"
        self.broken_count = 0
        self.raw = {"rqex": str(date), "ZQDM": code, "ztlb": status}


class Result:
    def __init__(self, rows):
        self.rows = rows
        self.request_body = {"test": True}


class Client:
    def __init__(self):
        self.calls = []

    def limit_up_down_list(self, start, end=None):
        self.calls.append((start, end))
        if end is not None:
            # Deliberately omit the second session to exercise supplementation.
            return Result([Row(start, "000001")])
        return Result([Row(start, "000002", "broken")])


class FailedClient:
    def limit_up_down_list(self, start, end=None):
        raise TimeoutError("unavailable")


class EltdxCloseHistoryTest(unittest.TestCase):

    def test_month_windows_are_bounded(self):
        self.assertEqual(month_windows(20240115, 20240302), [
            (20240115, 20240131), (20240201, 20240229),
            (20240301, 20240302),
        ])

    def test_complete_batch_is_immutable_and_resumable(self):
        clock = lambda: datetime(
            2026, 10, 6, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with TemporaryDirectory() as directory:
            client = Client()
            first = download_history(
                client=client, calendar_dates=[20240102, 20240103],
                output_dir=directory, start_date=20240102,
                end_date=20240103, sleep_seconds=0, sleep_fn=lambda _: None,
                now_fn=clock)
            self.assertEqual(first["status_counts"], {"complete": 1})
            manifest_path = Path(first["months"][0]["manifest"])
            manifest = json.loads(manifest_path.read_text())
            self.assertFalse(manifest["strict_pit_allowed"])
            self.assertEqual(manifest["availability_evidence"],
                             "BACKFILLED_QUERY")
            self.assertEqual(manifest["observed_sessions"],
                             [20240102, 20240103])
            self.assertEqual(len(client.calls), 2)
            second = download_history(
                client=client, calendar_dates=[20240102, 20240103],
                output_dir=directory, start_date=20240102,
                end_date=20240103, sleep_seconds=0, sleep_fn=lambda _: None,
                now_fn=clock)
            self.assertEqual(second["status_counts"], {"skipped_complete": 1})
            self.assertEqual(len(client.calls), 2)

    def test_source_failure_is_not_written_as_zero_events(self):
        clock = lambda: datetime(
            2026, 10, 6, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with TemporaryDirectory() as directory:
            result = download_history(
                client=FailedClient(), calendar_dates=[20240102],
                output_dir=directory, start_date=20240102,
                end_date=20240102, attempts=2, sleep_seconds=0,
                sleep_fn=lambda _: None, now_fn=clock)
            self.assertEqual(result["status_counts"], {"error": 1})
            manifest = json.loads(Path(
                result["months"][0]["manifest"]).read_text())
            self.assertEqual(manifest["error_code"], "SOURCE_REQUEST_FAILED")
            self.assertEqual(manifest["missing_sessions"], [20240102])
            self.assertNotIn("raw_path", manifest)

    def test_partial_month_does_not_satisfy_later_full_month_request(self):
        clock = lambda: datetime(
            2026, 10, 6, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with TemporaryDirectory() as directory:
            client = Client()
            download_history(
                client=client, calendar_dates=[20240115],
                output_dir=directory, start_date=20240115,
                end_date=20240115, sleep_seconds=0, sleep_fn=lambda _: None,
                now_fn=clock)
            calls = len(client.calls)
            result = download_history(
                client=client, calendar_dates=[20240102],
                output_dir=directory, start_date=20240102,
                end_date=20240102, sleep_seconds=0, sleep_fn=lambda _: None,
                now_fn=clock)
            self.assertEqual(result["status_counts"], {"complete": 1})
            self.assertGreater(len(client.calls), calls)


if __name__ == "__main__":
    unittest.main()
