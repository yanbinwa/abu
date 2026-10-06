"""Short-line source availability contract tests."""

import unittest

import pandas as pd

from abupy.AlphaBu.ABuShortLineEvents import classify_probe_result
from scripts.audit_shortline_sources import (
    compare_event_sets, eltdx_limit_frame, probe_call, retry_call,
)


class ShortLineSourceAuditTest(unittest.TestCase):

    def test_backfilled_success_cannot_enter_asof_features(self):
        meta = classify_probe_result(
            pd.DataFrame([{"代码": "000001"}]), source="test", dataset="pool",
            query_date=20250102, ingested_at="2026-10-03T12:00:00+08:00",
            ingested_date=20261003)
        self.assertEqual(meta.availability_evidence, "BACKFILLED_QUERY")
        self.assertFalse(meta.asof_feature_allowed)

    def test_empty_response_is_ambiguous(self):
        meta = classify_probe_result(
            pd.DataFrame(), source="test", dataset="pool",
            query_date=20261003, ingested_at="2026-10-03T12:00:00+08:00",
            ingested_date=20261003)
        self.assertEqual(meta.status, "empty_ambiguous")
        self.assertFalse(meta.asof_feature_allowed)

    def test_retention_error_has_distinct_code(self):
        def failed():
            raise ValueError("只能获取最近 30 个交易日的数据")
        meta = probe_call(
            failed, source="test", dataset="pool", query_date=20220101,
            ingested_at="2026-10-03T12:00:00+08:00", ingested_date=20261003)
        self.assertEqual(meta.error_code, "OUTSIDE_PROVIDER_RETENTION")
        self.assertNotEqual(meta.status, "empty_ambiguous")

    def test_eltdx_rows_are_normalized_without_provider_dependency(self):
        class Row(object):
            code = "000001"
            full_code = "sz000001"
            name = "平安银行"
            status = "limit_up"
            board_level = 2
            highest_board_level = 4
            trading_date_value = "20260930"
            industry = "银行"
            limit_reason = "测试"
            limit_reason_extra = None
            seal_amount = 100
            limit_time = "10:00:00"
            broken_count = 0

        class Result(object):
            rows = [Row()]

        frame = eltdx_limit_frame(Result())
        self.assertEqual(frame.iloc[0].full_code, "sz000001")
        self.assertEqual(frame.iloc[0].board_level, 2)

    def test_cross_source_comparison_keeps_disagreements(self):
        frames = {
            ("eltdx_tdx", "limit_up_down_list", 20260930): pd.DataFrame([
                {"code": "000001", "status": "limit_up"},
                {"code": "000002", "status": "limit_up"},
            ]),
            ("akshare_eastmoney", "stock_zt_pool_em", 20260930):
                pd.DataFrame([{"代码": "000001"}, {"代码": "000003"}]),
        }
        rows = compare_event_sets(frames, [20260930])
        self.assertEqual(rows[0]["intersection_count"], 1)
        self.assertEqual(rows[0]["only_eltdx"], ["000002"])
        self.assertEqual(rows[0]["only_akshare"], ["000003"])

    def test_retry_call_recovers_after_transient_failure(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("temporary")
            return "ok"

        self.assertEqual(retry_call(flaky, attempts=2, delay_seconds=0), "ok")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
