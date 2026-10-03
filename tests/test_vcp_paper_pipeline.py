"""Pipeline routing tests for optional short-line shadow capture."""
from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from scripts.run_vcp_paper_pipeline import shortline_trade_date


class VCPPaperPipelineTest(unittest.TestCase):

    def test_new_market_update_routes_its_trade_date(self):
        value = shortline_trade_date(
            {"status": "updated", "trade_date": 20261009},
            datetime(2026, 10, 9, 15, 21, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(value, 20261009)

    def test_late_same_day_rerun_can_retry_shadow_capture(self):
        value = shortline_trade_date(
            {"status": "no_new_session", "cached_date": 20261009},
            datetime(2026, 10, 9, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(value, 20261009)

    def test_weekend_does_not_backfill_last_market_session(self):
        value = shortline_trade_date(
            {"status": "no_new_session", "cached_date": 20261009},
            datetime(2026, 10, 10, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main()
