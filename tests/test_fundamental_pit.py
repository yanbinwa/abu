"""Immutable raw fundamental source tests (M2)."""
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuFundamentalPIT import (
    FundamentalFact, FundamentalFactStore, ImmutableFundamentalRawStore,
    FrozenUniverseBuilder, FundamentalPanelSidecar,
    ShareCapitalEvent, ShareCapitalStore, build_fundamental_coverage_report,
    conservative_available_at, fundamental_source_conflicts, market_cap_snapshot,
    normalize_extracted_facts, reconcile_share_capital,
    structured_balance_total_share_events, structured_share_events,
    structured_statement_records, TotalShareStore,
)


class SequenceFactory(object):
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return "batch-{}".format(self.value)


class FundamentalRawStoreTest(unittest.TestCase):

    def store(self, directory):
        return ImmutableFundamentalRawStore(
            directory,
            clock=lambda: datetime(2026, 10, 3, 0, 0,
                                   tzinfo=timezone.utc),
            batch_id_factory=SequenceFactory(),
        )

    def test_same_payload_deduplicates_blob_but_preserves_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            first_path, first = store.capture(
                "source", "income", {"symbol": "a"}, b'{"value":1}',
                "SUCCESS_NONEMPTY", "v1", record_count=1,
            )
            second_path, second = store.capture(
                "source", "income", {"symbol": "a"}, b'{"value":1}',
                "SUCCESS_NONEMPTY", "v1", record_count=1,
            )
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(first["payload_sha256"], second["payload_sha256"])
            self.assertEqual(first["blob_path"], second["blob_path"])
            self.assertEqual(len(list((Path(directory) / "blobs").rglob("*.bin"))), 1)
            self.assertEqual(store.read_payload(first), b'{"value":1}')

    def test_empty_response_is_distinct_from_successful_zero_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            _, empty = store.capture(
                "source", "income", {}, b'{"data":[]}', "SUCCESS_EMPTY",
                "v1", record_count=0,
            )
            _, zero = store.capture(
                "source", "income", {}, b'{"data":[{"value":0}]}',
                "SUCCESS_NONEMPTY", "v1", record_count=1,
            )
            self.assertNotEqual(empty["outcome"], zero["outcome"])
            self.assertNotEqual(empty["payload_sha256"], zero["payload_sha256"])

    def test_currency_unit_and_credentials_are_auditable_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            path, metadata = store.capture(
                "source", "balance",
                {"symbol": "a", "api_key": "do-not-store",
                 "headers": {"Authorization": "secret"}},
                b'{"assets":1}', "SUCCESS_NONEMPTY", "v1",
                record_count=1, currency="CNY", unit="yuan",
                response_headers={"Set-Cookie": "secret-cookie"},
            )
            serialized = path.read_text(encoding="utf-8")
            self.assertNotIn("do-not-store", serialized)
            self.assertNotIn("secret-cookie", serialized)
            self.assertEqual(metadata["currency"], "CNY")
            self.assertEqual(metadata["unit"], "yuan")

    def test_source_conflicts_are_explicit_and_deterministic(self):
        primary = [{"symbol": "a", "period": "2025Q4", "profit": 10,
                    "currency": "CNY"}]
        auxiliary = [{"symbol": "a", "period": "2025Q4", "profit": 11,
                      "currency": "CNY"}]
        first = fundamental_source_conflicts(
            primary, auxiliary, ("symbol", "period"),
            ("profit", "currency"))
        second = fundamental_source_conflicts(
            list(reversed(primary)), list(reversed(auxiliary)),
            ("symbol", "period"), ("profit", "currency"))
        self.assertEqual(first, second)
        self.assertEqual(first[0]["differences"]["profit"]["primary"], 10)


def make_fact(period, kind, value, available, revision="1",
              field="parent_net_profit", statement="income",
              value_kind="cumulative", restated=False):
    return FundamentalFact(
        symbol="sz000001", statement_type=statement,
        report_period=period, report_kind=kind,
        announcement_date=available[:10], available_at=available,
        revision_id=revision, is_restated=restated, source="cninfo_pdf",
        source_key="{}-{}-{}".format(period, field, revision),
        ingested_at="2026-10-03T00:00:00+00:00", currency="CNY",
        unit_scale=1.0, field=field, value=float(value),
        value_kind=value_kind, raw_field=field, raw_value=float(value),
        mapping_version="test", raw_payload_sha256="a" * 64,
    )


class FundamentalFactTest(unittest.TestCase):

    def test_structured_statement_uses_later_update_and_marks_history(self):
        mapping = {
            "allowed_unit_scales": [1.0],
            "fields": {
                "PARENT_NETPROFIT": {
                    "standard_field": "parent_net_profit",
                    "statement_types": ["income"],
                    "value_kind": "cumulative",
                },
            },
        }
        rows = [{
            "REPORT_DATE": "2024-12-31 00:00:00",
            "NOTICE_DATE": "2025-03-20 00:00:00",
            "UPDATE_DATE": "2025-06-01 00:00:00",
            "CURRENCY": "CNY", "PARENT_NETPROFIT": 80,
        }]
        records, audit = structured_statement_records(
            rows, "income", "sz000001", mapping, "f" * 64,
            "2026-10-03T00:00:00+00:00")
        self.assertEqual(records[0]["announcement"], "2025-06-01")
        self.assertTrue(records[0]["is_restated"])
        self.assertFalse(records[0]["revision_history_complete"])
        self.assertEqual(audit["restated_rows"], 1)
        sessions = [date(2025, 6, 2), date(2025, 6, 3)]
        facts = normalize_extracted_facts(
            records, mapping, sessions, "structured-test")
        self.assertEqual(
            facts[0].available_at, "2025-06-02T09:25:00+08:00")
        self.assertFalse(facts[0].revision_history_complete)

    def test_structured_statement_fails_closed_on_bad_metadata(self):
        mapping = {
            "allowed_unit_scales": [1.0],
            "fields": {"TOTAL_ASSETS": {
                "standard_field": "total_assets",
                "statement_types": ["balance"],
                "value_kind": "instant",
            }},
        }
        rows = [
            {"REPORT_DATE": "2025-03-31", "NOTICE_DATE": None,
             "UPDATE_DATE": None, "CURRENCY": "CNY", "TOTAL_ASSETS": 1},
            {"REPORT_DATE": "2025-03-31", "NOTICE_DATE": "2025-04-20",
             "UPDATE_DATE": "2025-04-19", "CURRENCY": "CNY",
             "TOTAL_ASSETS": 1},
            {"REPORT_DATE": "2025-03-31", "NOTICE_DATE": "2025-04-20",
             "UPDATE_DATE": "2025-04-20", "CURRENCY": "USD",
             "TOTAL_ASSETS": 1},
        ]
        records, audit = structured_statement_records(
            rows, "balance", "sz000001", mapping, "e" * 64,
            "2026-10-03T00:00:00+00:00")
        self.assertEqual(records, [])
        self.assertEqual(audit["excluded_rows"], 3)

    def test_structured_share_events_scale_and_visibility(self):
        rows = [{
            "DECLAREDATE": "2025-04-20", "VARYDATE": "2025-03-31",
            "F003N": 120.0, "F021N": 90.0, "F022N": 80.0,
        }]
        sessions = [date(2025, 4, 21), date(2025, 4, 22)]
        events, audit = structured_share_events(
            rows, "sh600001", "d" * 64, sessions)
        self.assertEqual(events[0].total_shares, 1200000)
        self.assertEqual(events[0].float_shares, 800000)
        self.assertEqual(
            events[0].effective_at, "2025-04-21T09:25:00+08:00")
        self.assertEqual(events[0].event_date, "2025-03-31")
        self.assertEqual(events[0].float_share_basis,
                         "circulating_a_shares")
        self.assertEqual(audit["float_share_field"], "F022N")
        self.assertEqual(audit["normalized_events"], 1)

    def test_share_store_does_not_rollback_to_older_periodic_snapshot(self):
        events = ShareCapitalStore([
            ShareCapitalEvent(
                "sz000001", "2025-07-07T09:25:00+08:00", 1200, 900,
                "change", "a" * 64, event_date="2025-07-04",
                announcement_date="2025-07-03"),
            ShareCapitalEvent(
                "sz000001", "2025-08-29T09:25:00+08:00", 1000, 800,
                "periodic", "b" * 64, event_date="2025-06-30",
                announcement_date="2025-08-28"),
        ])
        current = events.asof(
            "sz000001", "2025-09-01T15:00:00+08:00")
        self.assertEqual(current.source_key, "change")
        self.assertEqual(current.total_shares, 1200)

    def test_balance_total_share_fallback_never_invents_float_shares(self):
        records = [{
            "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
            "raw_value": 1200, "unit_scale": 1.0,
            "announcement": "2025-04-20", "source_key": "balance-1",
            "raw_payload_sha256": "a" * 64,
        }]
        sessions = [date(2025, 4, 21), date(2025, 4, 22)]
        events, audit = structured_balance_total_share_events(
            records, sessions)
        store = TotalShareStore(events)
        event = store.asof(
            "sh600001", "2025-04-21T15:00:00+08:00")
        missing = reconcile_share_capital(None, event, None)
        self.assertEqual(missing["total_shares"], 1200)
        self.assertIsNone(missing["float_shares"])
        self.assertEqual(missing["reconciliation_status"], "MISSING")
        complete = reconcile_share_capital(None, event, 800)
        self.assertEqual(complete["reconciliation_status"],
                         "STRUCTURED_FALLBACK")
        self.assertEqual(complete["float_share_source"],
                         "daily_price_outstanding_share")
        self.assertEqual(audit["state_transitions"], 1)

    def test_balance_share_store_does_not_rollback_to_older_report(self):
        records = [{
            "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
            "raw_value": 1200, "unit_scale": 1.0,
            "announcement": "2025-07-03", "report_period": "2025-06-30",
            "source_key": "new-period", "raw_payload_sha256": "a" * 64,
        }, {
            "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
            "raw_value": 1000, "unit_scale": 1.0,
            "announcement": "2025-08-28", "report_period": "2025-03-31",
            "source_key": "old-period-update",
            "raw_payload_sha256": "b" * 64,
        }]
        events, audit = structured_balance_total_share_events(
            records, [date(2025, 7, 4), date(2025, 8, 29),
                      date(2025, 9, 1)])
        current = TotalShareStore(events).asof(
            "sh600001", "2025-09-01T15:00:00+08:00")
        self.assertEqual(current.source_key, "new-period")
        self.assertEqual(current.total_shares, 1200)
        self.assertEqual(audit["state_transitions"], 2)

    def test_primary_share_event_wins_and_conflicts_are_visible(self):
        primary = ShareCapitalEvent(
            "sh600001", "2025-04-21T09:25:00+08:00", 1200, 800,
            "primary", "b" * 64)
        records = [{
            "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
            "raw_value": 1000, "unit_scale": 1.0,
            "announcement": "2025-04-20", "source_key": "balance-1",
            "raw_payload_sha256": "c" * 64,
        }]
        fallback = structured_balance_total_share_events(
            records, [date(2025, 4, 21), date(2025, 4, 22)])[0][0]
        result = reconcile_share_capital(primary, fallback, 700)
        self.assertEqual(result["total_shares"], 1200)
        self.assertEqual(result["float_shares"], 800)
        self.assertEqual(result["reconciliation_status"], "CONFLICT")
        self.assertEqual(result["conflict_reasons"], [
            "FLOAT_PRIMARY_VS_DAILY", "TOTAL_PRIMARY_VS_BALANCE"])
        self.assertEqual(result["primary_total_shares"], 1200)
        self.assertEqual(result["balance_total_shares"], 1000)
        self.assertEqual(result["daily_float_shares_observed"], 700)
        self.assertGreater(result["total_relative_difference"], .005)

    def test_float_above_total_is_a_conflict_not_missing_data(self):
        balance = structured_balance_total_share_events([{
            "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
            "raw_value": 700, "unit_scale": 1.0,
            "announcement": "2025-04-20", "source_key": "balance-1",
            "raw_payload_sha256": "d" * 64,
        }], [date(2025, 4, 21), date(2025, 4, 22)])[0][0]
        result = reconcile_share_capital(None, balance, 800)
        self.assertEqual(result["reconciliation_status"], "CONFLICT")
        self.assertEqual(result["conflict_reasons"], [
            "FLOAT_EXCEEDS_TOTAL"])
        self.assertIsNone(result["missing_reason"])

    def test_date_only_announcement_is_visible_next_trading_session(self):
        sessions = [date(2025, 4, 25), date(2025, 4, 28), date(2025, 4, 29)]
        available = conservative_available_at("2025-04-25", sessions)
        self.assertEqual(available.isoformat(), "2025-04-28T09:25:00+08:00")

    def test_future_revision_does_not_change_past_snapshot(self):
        original = make_fact(
            "2024-12-31", "FY", 100,
            "2025-03-31T09:00:00+08:00", revision="original")
        revised = make_fact(
            "2024-12-31", "FY", 80,
            "2025-06-01T09:00:00+08:00", revision="revision-1",
            restated=True)
        store = FundamentalFactStore([original, revised])
        before = store.fact_asof(
            "sz000001", "income", "2024-12-31", "parent_net_profit",
            "2025-05-31T15:00:00+08:00")
        after = store.fact_asof(
            "sz000001", "income", "2024-12-31", "parent_net_profit",
            "2025-06-02T15:00:00+08:00")
        self.assertEqual(before.value, 100)
        self.assertEqual(after.value, 80)
        revised_future = make_fact(
            "2024-12-31", "FY", 60,
            "2025-07-01T09:00:00+08:00", revision="revision-2",
            restated=True)
        past_again = FundamentalFactStore(
            [original, revised, revised_future]).fact_asof(
                "sz000001", "income", "2024-12-31",
                "parent_net_profit", "2025-05-31T15:00:00+08:00")
        self.assertEqual(past_again.value, 100)

    def test_cumulative_quarters_and_cross_year_ttm_reconcile(self):
        facts = [
            make_fact("2024-03-31", "Q1", 10,
                      "2024-04-30T09:00:00+08:00"),
            make_fact("2024-06-30", "H1", 25,
                      "2024-08-30T09:00:00+08:00"),
            make_fact("2024-09-30", "Q3", 45,
                      "2024-10-30T09:00:00+08:00"),
            make_fact("2024-12-31", "FY", 70,
                      "2025-03-30T09:00:00+08:00"),
            make_fact("2025-03-31", "Q1", 30,
                      "2025-04-30T09:00:00+08:00"),
        ]
        store = FundamentalFactStore(facts)
        quarters = store.single_quarters_asof(
            "sz000001", "income", "parent_net_profit",
            "2025-05-01T15:00:00+08:00")
        self.assertEqual([quarters[2024 * 4 + index].value
                          for index in range(4)], [10, 15, 20, 25])
        ttm = store.ttm_asof(
            "sz000001", "income", "parent_net_profit",
            "2025-05-01T15:00:00+08:00", "2025-03-31")
        self.assertEqual(ttm.value, 90)
        self.assertEqual(ttm.missing_reason, None)

    def test_missing_quarter_fails_closed(self):
        store = FundamentalFactStore([
            make_fact("2024-03-31", "Q1", 10,
                      "2024-04-30T09:00:00+08:00"),
            make_fact("2024-09-30", "Q3", 45,
                      "2024-10-30T09:00:00+08:00"),
            make_fact("2024-12-31", "FY", 70,
                      "2025-03-30T09:00:00+08:00"),
        ])
        ttm = store.ttm_asof(
            "sz000001", "income", "parent_net_profit",
            "2025-04-01T15:00:00+08:00", "2024-12-31")
        self.assertIsNone(ttm.value)
        self.assertIn("TTM_INCOMPLETE", ttm.missing_reason)

    def test_total_and_float_market_cap_are_separate_and_effective_dated(self):
        events = ShareCapitalStore([
            ShareCapitalEvent(
                "sz000001", "2025-01-01T00:00:00+08:00", 1000, 700,
                "shares-1", "b" * 64),
            ShareCapitalEvent(
                "sz000001", "2025-06-01T00:00:00+08:00", 1200, 800,
                "shares-2", "c" * 64),
        ])
        before = market_cap_snapshot(10, events.asof(
            "sz000001", "2025-05-31T15:00:00+08:00"))
        after = market_cap_snapshot(10, events.asof(
            "sz000001", "2025-06-01T15:00:00+08:00"))
        self.assertEqual(before["total_market_cap"], 10000)
        self.assertEqual(before["float_market_cap"], 7000)
        self.assertEqual(after["total_market_cap"], 12000)
        self.assertEqual(after["float_market_cap"], 8000)

    def test_normalization_rejects_currency_unit_and_zero_share_errors(self):
        mapping = {
            "allowed_unit_scales": [1.0, 10000.0],
            "fields": {"营业收入": {
                "standard_field": "revenue",
                "statement_types": ["income"],
                "value_kind": "cumulative",
            }},
        }
        record = {
            "symbol": "sz000001", "statement_type": "income",
            "report_period": "2025-03-31", "announcement": "2025-04-25",
            "revision_id": "1", "is_restated": False,
            "source": "cninfo_pdf", "source_key": "doc-1",
            "ingested_at": "2025-04-26T00:00:00+08:00",
            "currency": "USD", "unit_scale": 1.0,
            "raw_field": "营业收入", "raw_value": 2,
            "raw_payload_sha256": "d" * 64,
        }
        sessions = [date(2025, 4, 25), date(2025, 4, 28)]
        with self.assertRaisesRegex(ValueError, "CNY"):
            normalize_extracted_facts([record], mapping, sessions, "test")
        bad_unit = dict(record, currency="CNY", unit_scale=100.0)
        with self.assertRaisesRegex(ValueError, "unit"):
            normalize_extracted_facts([bad_unit], mapping, sessions, "test")
        with self.assertRaisesRegex(ValueError, "positive"):
            ShareCapitalEvent(
                "sz000001", "2025-01-01T00:00:00+08:00", 0, 0,
                "bad", "e" * 64)


class FundamentalUniverseTest(unittest.TestCase):

    def test_size_universes_are_deterministic_nested_and_reasoned(self):
        symbols = ["s{:02d}".format(index) for index in range(10)]
        builder = FrozenUniverseBuilder(symbols)
        result = builder.build(
            np.ones(10, dtype=bool),
            np.array([1, 2, 2, 4, 5, 6, 7, 8, 9, 10], dtype=float),
            np.ones(10, dtype=bool),
        )
        all_mask = result["all_eligible_sensitivity"]
        primary = result["size_controlled_primary"]
        large = result["large_mid_sensitivity"]
        self.assertEqual(int(all_mask.sum()), 10)
        self.assertEqual(int(primary.sum()), 7)
        self.assertEqual(int(large.sum()), 5)
        self.assertTrue(np.all(large <= primary))
        self.assertTrue(np.all(primary <= all_mask))
        self.assertEqual(
            result["reasons"]["size_controlled_primary"][0],
            ("SMALL_CAP_BOTTOM_30",))
        self.assertEqual(
            result["reasons"]["size_controlled_primary"][1],
            ("SMALL_CAP_BOTTOM_30",))

    def test_future_cap_change_does_not_change_past_universe(self):
        builder = FrozenUniverseBuilder(["a", "b", "c", "d"])
        caps = np.array([[1., 2., 3., 4.], [4., 3., 2., 1.]])
        before = builder.build(
            np.ones(4, bool), caps[0], np.ones(4, bool))
        caps[1] *= 100
        after = builder.build(
            np.ones(4, bool), caps[0], np.ones(4, bool))
        np.testing.assert_array_equal(
            before["size_controlled_primary"],
            after["size_controlled_primary"])

    def test_missing_theme_and_cap_are_not_filled(self):
        builder = FrozenUniverseBuilder(["a", "b", "c"])
        result = builder.build(
            [True, True, False], [1.0, np.nan, 3.0], [True, False, True])
        np.testing.assert_array_equal(
            result["all_eligible_sensitivity"], [True, False, False])
        self.assertIn("MISSING_TOTAL_MARKET_CAP",
                      result["reasons"]["all_eligible_sensitivity"][1])
        self.assertIn("MISSING_REQUIRED_THEME",
                      result["reasons"]["all_eligible_sensitivity"][1])
        self.assertIn("BASE_INELIGIBLE",
                      result["reasons"]["all_eligible_sensitivity"][2])

    def test_sidecar_loads_only_requested_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "20260102.jsonl").write_text(
                json.dumps({"symbol": "a", "value": 1}) + "\n",
                encoding="utf-8")
            sidecar = FundamentalPanelSidecar(
                [20260102, 20260105], ["a", "b"], root)
            first = sidecar.snapshot(0).set_index("symbol")
            self.assertEqual(first.loc["a", "value"], 1)
            self.assertEqual(first.loc["b", "missing_reason"],
                             "MISSING_FUNDAMENTAL_SYMBOL")
            second = sidecar.snapshot(1)
            self.assertTrue((second.missing_reason ==
                             "MISSING_FUNDAMENTAL_PARTITION").all())

    def test_coverage_gate_reports_low_days_without_imputation(self):
        rows = []
        for date_value, covered in [(20260102, 10), (20260105, 5)]:
            for index in range(10):
                value = 1.0 if index < covered else np.nan
                rows.append({
                    "date": date_value, "symbol": "s{}".format(index),
                    "exchange": "sh" if index < 5 else "sz",
                    "industry": "i{}".format(index % 2),
                    "universe_eligible": True, "st_known": index >= 5,
                    "total_market_cap": value, "float_market_cap": value,
                    "source": "cninfo" if np.isfinite(value) else None,
                    "is_restated": False,
                    "value": value, "quality": value, "investment": value,
                })
        report = build_fundamental_coverage_report(
            pd.DataFrame(rows), ("value", "quality", "investment"),
            shanghai_st_complete=False, reconciliation_passed=True,
            pit_tests_passed=True)
        self.assertEqual(report["gate_status"], "BLOCKED_DATA_GATE")
        self.assertEqual(report["theme_coverage"]["value"]["median"], .75)
        self.assertIn("shanghai_historical_st_incomplete", report["blockers"])


if __name__ == "__main__":
    unittest.main()
