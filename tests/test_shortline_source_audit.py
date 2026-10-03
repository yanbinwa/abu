"""Short-line source availability contract tests."""

import unittest

import pandas as pd

from abupy.AlphaBu.ABuShortLineEvents import classify_probe_result
from scripts.audit_shortline_sources import probe_call


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


if __name__ == "__main__":
    unittest.main()

