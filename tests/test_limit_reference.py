"""Limit-reference sidecar and corporate-action reconstruction tests."""

import tempfile
import unittest
from pathlib import Path

from abupy.AlphaBu.ABuLimitReference import (
    LimitReference, LimitReferenceStore, reconstruct_corporate_action_reference,
    reference_from_provider_row,
)
from abupy.AlphaBu.ABuPriceLimit import LimitRuleContext, limit_prices, limit_rule_for_context


class LimitReferenceTest(unittest.TestCase):

    def test_provider_reference_and_direct_limits_have_explicit_provenance(self):
        record = reference_from_provider_row(
            20250716, "sh600000",
            {"previous_raw_close": 10.0, "pre_close": 9.59,
             "upper_limit": 10.55, "lower_limit": 8.63},
            source="exchange_snapshot",
            available_at="2025-07-16T09:15:00+08:00",
            availability_evidence="fixture",
        )
        self.assertTrue(record.has_reference)
        self.assertTrue(record.has_direct_limits)
        self.assertEqual(record.quality, "known")
        self.assertIn("DIRECT_EXCHANGE_LIMIT", record.reason_codes)
        self.assertIn("PROVIDER_PRE_CLOSE", record.reason_codes)

    def test_missing_provider_reference_stays_unknown(self):
        record = reference_from_provider_row(
            20250103, "sz000001", {"previous_raw_close": 10.0},
            source="raw_ohlc", available_at="2025-01-03T09:15:00+08:00",
        )
        self.assertFalse(record.has_reference)
        self.assertEqual(record.quality, "unknown")
        self.assertEqual(record.reason_codes, ("UNKNOWN_REFERENCE_PRICE",))

    def test_corporate_action_formula_handles_cash_stock_and_rights(self):
        actual = reconstruct_corporate_action_reference(
            12.0, cash_per_share=0.2, stock_per_share=0.1,
            rights_per_share=0.2, rights_price=5.0,
        )
        self.assertAlmostEqual(actual, (12.0 - 0.2 + 0.2 * 5.0) / 1.3)

    def test_cash_dividend_reference_changes_the_daily_limits(self):
        reference = reconstruct_corporate_action_reference(
            10.0, cash_per_share=0.2)
        rule = limit_rule_for_context(LimitRuleContext(
            "sh", "main", 20250716))
        self.assertEqual(limit_prices(reference, rule), (8.82, 10.78))

    def test_store_round_trip_and_asof_gate(self):
        record = LimitReference(
            trade_date=20250103, symbol="sz000001",
            limit_reference_price_raw=10.0, source="provider",
            quality="known", effective_at="2025-01-03T00:00:00+08:00",
            available_at="2025-01-03T09:15:00+08:00",
            availability_evidence="fixture",
            reason_codes=("PROVIDER_PRE_CLOSE",),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sidecar.csv"
            LimitReferenceStore([record]).write(path)
            restored = LimitReferenceStore.read(path)
            self.assertTrue(restored.get(20250103, "sz000001").has_reference)
            before = restored.get(
                20250103, "sz000001", as_of="2025-01-03T09:14:59+08:00"
            )
            self.assertFalse(before.has_reference)

    def test_duplicate_keys_are_rejected(self):
        record = LimitReference.unknown(20250103, "sz000001")
        with self.assertRaises(ValueError):
            LimitReferenceStore([record, record])


if __name__ == "__main__":
    unittest.main()
