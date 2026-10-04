"""Structured-interface-first fundamental pilot tests."""
import json
import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.audit_structured_fundamental_v4 import build_audit
from scripts.audit_share_capital_conflicts_v4 import build_audit as build_share_audit
from scripts.audit_baostock_pilot_v4 import build_audit as build_baostock_audit
from scripts.collect_structured_fundamental_v4 import (
    collect, frame_snapshot, select_pilot_symbols,
)
from scripts.collect_baostock_pit_pilot_v4 import (
    collect as collect_baostock, normalize_rows as normalize_baostock_rows,
)
from scripts.freeze_baostock_st_universe_v4 import freeze_universe
from scripts.audit_baostock_st_exclusion_v4 import build_audit as build_st_audit
from scripts.audit_multifactor_coverage_v4 import blocked_report
from scripts.materialize_structured_share_capital_v4 import materialize
from scripts.replay_structured_share_events_v4 import replay


class StructuredPilotTest(unittest.TestCase):

    def test_main_coverage_report_removes_only_passed_st_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pd.DataFrame([{"symbol": "sh600001"}]).to_csv(
                root / "security_master.csv", index=False)
            st_audit = root / "st_audit.json"
            st_audit.write_text(json.dumps({
                "gate_status": "PASS_ST_EXCLUSION_DATA_GATE",
                "exact_st_coverage": 1.0,
                "source_role": "st_exclusion_only",
                "unknown_st_policy": "exclude",
            }), encoding="utf-8")
            config = {
                "strategy_version": "test",
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
                "economic_themes": ["value"],
                "coverage_gates": {"lifecycle_and_st_known_min": .995},
            }
            report = blocked_report(
                config, root, root / "missing.jsonl", st_audit)
            self.assertNotIn("shanghai_historical_st_incomplete",
                             report["blockers"])
            self.assertTrue(report["st_coverage"]
                            ["full_market_exclusion_gate_passed"])
            self.assertEqual(report["gate_status"], "BLOCKED_DATA_GATE")

    def test_st_exclusion_audit_requires_complete_exact_date_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            (research / "raw").mkdir(parents=True)
            master = research / "security_master.csv"
            pd.DataFrame([{
                "symbol": "sh600001", "list_date": "2025-01-01",
                "delist_date": "", "exchange": "sh", "status": "listed",
            }]).to_csv(master, index=False)
            pd.DataFrame({"date": [20250102, 20250103]}).to_csv(
                research / "raw/sh600001.csv", index=False)
            config = {
                "config_version": "test", "source_version": "test-v1",
                "source_role": "st_exclusion_only",
                "security_master": str(master),
                "history_start_date": "2025-01-01",
                "history_end_date": "2025-01-03", "chunk_size": 10,
                "strict_rules": {
                    "unknown_st_policy": "exclude",
                    "security_day_coverage_min": 0.995,
                },
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            frozen = root / "frozen"
            freeze_universe(config, frozen)
            output = root / "collected/chunks/0000"
            output.mkdir(parents=True)
            output.joinpath("collection_report.json").write_text(json.dumps({
                "status": "COLLECTED_ST_EXCLUSION_CHUNK", "failures": [],
            }), encoding="utf-8")
            output.joinpath("baostock_daily.jsonl").write_text(
                json.dumps({
                    "symbol": "sh600001", "date": 20250102,
                    "is_st": 0, "source_role": "st_exclusion_only",
                    "derived_float_shares": None,
                }) + "\n", encoding="utf-8")
            report = build_st_audit(
                frozen, root / "collected", research, config)
            self.assertEqual(report["exact_st_coverage"], 0.5)
            self.assertEqual(report["unknown_security_days"], 1)
            self.assertEqual(report["gate_status"],
                             "BLOCKED_ST_EXCLUSION_DATA_GATE")
            self.assertIn("missing exact-date state excluded",
                          report["selection_semantics"])

    def test_st_chunk_runner_rejects_concurrency_above_source_limit(self):
        from scripts.run_baostock_st_chunks_v4 import run_chunks
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe"
            universe.mkdir()
            universe.joinpath("universe_manifest.json").write_text(
                json.dumps({"chunks": []}), encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"max_concurrency": 1}),
                              encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "concurrency limit"):
                run_chunks(universe, root / "output", config, workers=2)

    def test_st_exclusion_universe_is_frozen_in_dated_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.csv"
            pd.DataFrame([{
                "symbol": "sh600001", "list_date": "2020-01-01",
                "delist_date": "", "exchange": "sh", "status": "listed",
            }, {
                "symbol": "sz000001", "list_date": "2010-01-01",
                "delist_date": "2025-06-30", "exchange": "sz",
                "status": "delisted",
            }, {
                "symbol": "sh600999", "list_date": "2027-01-01",
                "delist_date": "", "exchange": "sh", "status": "listed",
            }]).to_csv(master, index=False)
            config = {
                "config_version": "test", "source_version": "test-v1",
                "source_role": "st_exclusion_only",
                "security_master": str(master),
                "history_start_date": "2019-01-01",
                "history_end_date": "2026-09-30", "chunk_size": 1,
                "strict_rules": {"unknown_st_policy": "exclude"},
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report, manifest = freeze_universe(config, root / "frozen")
            self.assertEqual(report["security_count"], 2)
            self.assertEqual(report["chunk_count"], 2)
            self.assertEqual(report["exclusion_reasons"], {
                "OUTSIDE_RESEARCH_INTERVAL": 1})
            first = json.loads((manifest.parent / report["chunks"][0]
                                ["input"]).read_text(encoding="utf-8"))
            self.assertEqual(first["source_role"], "st_exclusion_only")
            self.assertFalse(report["allow_as_alpha_or_ranking_feature"])

    def test_st_exclusion_normalization_does_not_derive_shares(self):
        rows, _ = normalize_baostock_rows([{
            "date": "2025-01-02", "code": "sh.600001",
            "tradestatus": "1", "isST": "0",
        }], "sh600001", "a" * 64, source_role="st_exclusion_only",
            derive_float_shares=False)
        self.assertIsNone(rows[0]["derived_float_shares"])
        self.assertEqual(rows[0]["source_role"], "st_exclusion_only")

    def test_baostock_audit_never_silently_overrides_primary(self):
        auxiliary = [{
            "symbol": "sh600001", "date": 20250102,
            "derived_float_shares": 800, "is_st": 1,
        }, {
            "symbol": "sh600001", "date": 20250103,
            "derived_float_shares": None, "is_st": 1,
        }]
        capital = [{
            "symbol": "sh600001", "date": 20250102,
            "primary_float_shares": 700,
            "daily_float_shares_observed": 800,
            "float_shares": 700,
            "conflict_reasons": ["FLOAT_PRIMARY_VS_DAILY"],
        }, {
            "symbol": "sh600001", "date": 20250103,
            "primary_float_shares": None,
            "daily_float_shares_observed": None,
            "balance_total_shares": 800,
            "float_shares": None, "conflict_reasons": [],
        }]
        report = build_baostock_audit(auxiliary, capital)
        self.assertEqual(report["exact_st_coverage"], 1.0)
        self.assertEqual(report["asof_derived_float_coverage"], 1.0)
        self.assertEqual(report["existing_float_conflict_arbitration"], {
            "supports_daily": 1})
        self.assertEqual(report["missing_primary_float_days_fillable"], {
            "sh600001": 1})
        fallback = report["missing_float_fallback_evidence"]["sh600001"]
        self.assertEqual(fallback["within_tolerance_rate"], 1.0)
        self.assertEqual(fallback["above_total_days"], 0)
        self.assertEqual(fallback["decision"],
                         "AUDIT_ONLY_APPROXIMATE_NOT_MATERIALIZED")
        self.assertEqual(report["authority_status"],
                         "MIXED_SOURCE_EVIDENCE_NO_OVERRIDE")

    def test_baostock_rows_derive_float_shares_without_overriding_sources(self):
        rows, audit = normalize_baostock_rows([{
            "date": "2025-01-02", "code": "sh.600001",
            "close": "10", "volume": "1000000", "turn": "2.0",
            "tradestatus": "1", "isST": "1",
        }, {
            "date": "2025-01-03", "code": "sh.600001",
            "close": "10", "volume": "0", "turn": "",
            "tradestatus": "0", "isST": "1",
        }], "sh600001", "a" * 64)
        self.assertEqual(rows[0]["derived_float_shares"], 50000000)
        self.assertIsNone(rows[1]["derived_float_shares"])
        self.assertEqual(rows[0]["source_role"], "auxiliary_audit_only")
        self.assertEqual(rows[0]["is_st"], 1)
        self.assertEqual(audit["derived_float_rows"], 1)

    def test_baostock_collection_is_immutable_and_injectable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pilot = root / "pilot"
            pilot.mkdir()
            (pilot / "pilot_symbols.json").write_text(json.dumps({
                "symbols": ["sh600001"]}), encoding="utf-8")
            config = {
                "config_version": "test", "source_version": "test-v1",
                "raw_root": str(root / "raw"),
                "history_start_date": "2025-01-01",
                "history_end_date": "2025-01-03",
                "symbol_start_overrides": {},
                "fields": ["date", "code", "close", "volume", "turn",
                           "tradestatus", "isST"],
                "source_role": "auxiliary_audit_only",
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            def fetcher(symbol, _start, _end):
                return [{
                    "date": "2025-01-02", "code": "sh.600001",
                    "close": "10", "volume": "100", "turn": "1",
                    "tradestatus": "1", "isST": "0",
                }]
            report, paths = collect_baostock(
                config, pilot, root / "output", fetcher)
            self.assertEqual(report["normalized_security_days"], 1)
            self.assertEqual(report["failures"], [])
            self.assertTrue(paths["data"].is_file())
            self.assertEqual(len(list((root / "raw/batches").rglob(
                "*.json"))), 1)

    def test_share_conflict_audit_builds_consecutive_intervals(self):
        rows = [{
            "symbol": "sh600001", "date": 20250102,
            "conflict_reasons": ["FLOAT_PRIMARY_VS_DAILY"],
            "float_relative_difference": .1,
        }, {
            "symbol": "sh600001", "date": 20250103,
            "conflict_reasons": ["FLOAT_PRIMARY_VS_DAILY"],
            "float_relative_difference": .2,
        }, {
            "symbol": "sh600001", "date": 20250106,
            "conflict_reasons": [], "float_relative_difference": 0,
        }, {
            "symbol": "sh600001", "date": 20250107,
            "conflict_reasons": ["TOTAL_PRIMARY_VS_BALANCE"],
            "total_relative_difference": .3,
        }]
        report = build_share_audit(rows)
        self.assertEqual(report["conflict_security_days"], 3)
        self.assertEqual(report["interval_count"], 2)
        self.assertEqual(
            report["reason_summary"]["FLOAT_PRIMARY_VS_DAILY"]
            ["interval_count"], 1)
        self.assertEqual(report["top_intervals"][0]["security_days"], 2)

    def test_share_replay_uses_a_share_float_and_verifies_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pilot = root / "pilot"
            raw = root / "raw"
            pilot.mkdir()
            (pilot / "pilot_symbols.json").write_text(json.dumps({
                "symbols": ["sz000002"]}), encoding="utf-8")
            payload = json.dumps({"records": [{
                "DECLAREDATE": "2025-04-20",
                "VARYDATE": "2025-03-31", "F003N": 120.0,
                "F021N": 100.0, "F022N": 80.0,
            }]}).encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            blob = raw / "blobs" / digest[:2] / (digest + ".bin")
            blob.parent.mkdir(parents=True)
            blob.write_bytes(payload)
            metadata = raw / "batches/20251003/cninfo_share_change/shares/a.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "source": "cninfo_share_change", "dataset": "shares",
                "symbol": "sz000002", "outcome": "SUCCESS_NONEMPTY",
                "retrieved_at_utc": "2025-10-03T12:00:00+00:00",
                "batch_id": "a", "payload_sha256": digest,
                "blob_path": str(blob.relative_to(raw)),
            }), encoding="utf-8")
            report, paths = replay(
                pilot, raw, root / "output",
                [date(2025, 4, 21), date(2025, 4, 22)],
                retrieved_on="2025-10-03")
            self.assertEqual(report["status"], "PASS_SHARE_REPLAY")
            event = json.loads(paths["events"].read_text(encoding="utf-8"))
            self.assertEqual(event["float_shares"], 800000)
            self.assertEqual(event["float_share_basis"],
                             "circulating_a_shares")

    def test_share_materialization_uses_balance_and_daily_float_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pilot = root / "pilot"
            research = root / "research"
            pilot.mkdir()
            (research / "raw").mkdir(parents=True)
            (pilot / "pilot_symbols.json").write_text(json.dumps({
                "symbols": ["sz000001"]}), encoding="utf-8")
            fact = {
                "symbol": "sz000001", "raw_field": "SHARE_CAPITAL",
                "raw_value": 1000, "unit_scale": 1.0,
                "announcement": "2025-04-20", "report_period": "2025-03-31",
                "source_key": "balance-1", "raw_payload_sha256": "a" * 64,
            }
            (pilot / "structured_extracted.jsonl").write_text(
                json.dumps(fact) + "\n", encoding="utf-8")
            (pilot / "share_events.jsonl").write_text("", encoding="utf-8")
            pd.DataFrame([{
                "date": 20250421, "close": 10,
                "outstanding_share": 800,
            }]).to_csv(research / "raw/sz000001.csv", index=False)
            calendar = root / "calendar.csv"
            pd.DataFrame({"date_time": [
                "2025-04-21", "2025-04-22"]}).to_csv(calendar, index=False)
            config = {
                "config_version": "test",
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
                "trading_sessions_source": str(calendar),
                "strict_rules": {
                    "share_reconciliation_relative_tolerance": .005,
                    "total_market_cap_coverage_min": .995,
                    "float_market_cap_coverage_min": .995,
                },
            }
            report, paths = materialize(
                pilot, research, root / "output", config,
                start_date=20250421, end_date=20250421)
            self.assertEqual(report["gate_status"],
                             "PASS_PILOT_SHARE_CAPITAL_GATE")
            row = json.loads(paths["data"].read_text(encoding="utf-8"))
            self.assertEqual(row["total_market_cap"], 10000)
            self.assertEqual(row["float_market_cap"], 8000)
            self.assertEqual(row["reconciliation_status"],
                             "STRUCTURED_FALLBACK")

    def test_collect_writes_replayable_outputs_without_network(self):
        class Response(object):
            status_code = 200

            def __init__(self):
                self.content = json.dumps({"records": [{
                    "DECLAREDATE": "2025-04-20",
                    "VARYDATE": "2025-03-31",
                    "F003N": 120.0, "F021N": 90.0, "F022N": 80.0,
                }]}).encode("utf-8")

            def json(self):
                return json.loads(self.content)

        def adapter(_symbol):
            return pd.DataFrame([{
                "REPORT_DATE": "2025-03-31",
                "NOTICE_DATE": "2025-04-20",
                "UPDATE_DATE": "2025-04-20", "CURRENCY": "CNY",
                "PARENT_NETPROFIT": 1.0, "TOTAL_ASSETS": 2.0,
                "NETCASH_OPERATE": 3.0,
            }])

        adapters = {
            (kind, flag): adapter
            for kind in ("income", "balance", "cashflow")
            for flag in (False, True)
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for prefix in ("sh600", "sz000", "sz300", "sh688"):
                for index in range(2):
                    rows.append({
                        "symbol": prefix + "{:03d}".format(index),
                        "list_date": "202{}-01-01".format(index),
                    })
            listed_path = root / "listed.csv"
            pd.DataFrame(rows).to_csv(listed_path, index=False)
            sessions_path = root / "sessions.csv"
            pd.DataFrame({"date_time": [
                "2025-04-21", "2025-04-22"]}).to_csv(
                    sessions_path, index=False)
            config = {
                "config_version": "test", "adapter_version": "test-v1",
                "raw_root": str(root / "raw"),
                "trading_sessions_source": str(sessions_path),
                "pilot": {
                    "sample_size": 6, "listed_source": str(listed_path),
                    "delisted_anchors": ["sz000999", "sh600999"],
                },
                "deferred": {"cninfo_pdf_extraction": True},
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            mapping = {
                "fields": {
                    "PARENT_NETPROFIT": {
                        "standard_field": "parent_net_profit",
                        "statement_types": ["income"],
                        "value_kind": "cumulative"},
                    "TOTAL_ASSETS": {
                        "standard_field": "total_assets",
                        "statement_types": ["balance"],
                        "value_kind": "instant"},
                    "NETCASH_OPERATE": {
                        "standard_field": "operating_cash_flow",
                        "statement_types": ["cashflow"],
                        "value_kind": "cumulative"},
                },
            }
            report, paths = collect(
                config, mapping, root / "output", max_symbols=2,
                adapters=adapters,
                share_fetcher=lambda *_: (Response(), {"url": "test"}))
            self.assertEqual(report["symbol_count"], 2)
            self.assertEqual(report["fact_value_count"], 6)
            self.assertEqual(report["share_event_count"], 2)
            self.assertEqual(report["failures"], [])
            self.assertTrue(all(path.is_file() for path in paths.values()))
            self.assertEqual(len(list((root / "raw" / "batches").rglob(
                "*.json"))), 8)

    def test_pilot_selection_is_deterministic_and_stratified(self):
        rows = []
        prefixes = ["sh600", "sz000", "sz300", "sh688"]
        for prefix in prefixes:
            for index in range(20):
                rows.append({
                    "symbol": prefix + "{:03d}".format(index),
                    "list_date": "20{:02d}-01-01".format(index),
                })
        frame = pd.DataFrame(rows)
        first, strata = select_pilot_symbols(
            frame, 10, ["sz000999", "sh600999"])
        second, _ = select_pilot_symbols(
            frame.sample(frac=1, random_state=1), 10,
            ["sz000999", "sh600999"])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertEqual(set(strata), {
            "sh_main", "sz_main", "chinext", "star"})

    def test_frame_snapshot_converts_nan_to_json_null(self):
        snapshot = frame_snapshot(pd.DataFrame({"a": [1.0, float("nan")]}))
        encoded = json.dumps(snapshot, allow_nan=False)
        self.assertIn("null", encoded)

    def test_audit_blocks_incomplete_revision_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pilot_symbols.json").write_text(json.dumps({
                "symbols": ["sh600001"]}), encoding="utf-8")
            (root / "collection_report.json").write_text(json.dumps({
                "config_version": "test",
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
                "failures": [], "deferred": {"cninfo_pdf_extraction": True},
            }), encoding="utf-8")
            facts = []
            for statement, field in [
                    ("income", "PARENT_NETPROFIT"),
                    ("balance", "TOTAL_ASSETS"),
                    ("cashflow", "NETCASH_OPERATE")]:
                facts.append({
                    "symbol": "sh600001", "statement_type": statement,
                    "raw_field": field, "is_restated": False,
                    "revision_history_complete": False,
                })
            (root / "structured_extracted.jsonl").write_text("".join(
                json.dumps(item) + "\n" for item in facts), encoding="utf-8")
            (root / "share_events.jsonl").write_text(json.dumps({
                "symbol": "sh600001"}) + "\n", encoding="utf-8")
            report = build_audit(root, [
                "PARENT_NETPROFIT", "TOTAL_ASSETS", "NETCASH_OPERATE"])
            self.assertEqual(report["gate_status"], "M4_BLOCKED_DATA_GATE")
            self.assertEqual(report["missing_share_symbols"], [])
            self.assertIn("historical_revision_versions_unavailable",
                          report["blockers"])


if __name__ == "__main__":
    unittest.main()
