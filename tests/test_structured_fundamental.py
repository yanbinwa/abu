"""Structured-interface-first fundamental pilot tests."""
import json
import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import numpy as np

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
from scripts.audit_eligible_market_cap_coverage_v4 import (
    build_audit as build_eligible_cap_audit,
)
from scripts.materialize_structured_share_capital_v4 import materialize
from scripts.replay_structured_share_events_v4 import replay
from scripts.freeze_cninfo_share_universe_v4 import (
    freeze_universe as freeze_share_universe,
)
from scripts.collect_cninfo_share_chunk_v4 import collect as collect_share_chunk
from scripts.audit_cninfo_share_coverage_v4 import (
    build_audit as build_cninfo_share_audit,
)
from scripts.run_cninfo_share_chunks_v4 import run_chunks as run_share_chunks
from scripts.materialize_full_market_cap_panel_v4 import (
    _asof_states, materialize as materialize_full_market_cap,
)
from scripts.freeze_fundamental_fallback_universe_v4 import (
    freeze as freeze_fallback_universe,
)
from scripts.collect_balance_fallback_chunk_v4 import (
    collect as collect_balance_fallback,
)
from scripts.run_balance_fallback_chunks_v4 import (
    run_chunks as run_balance_fallback_chunks,
)
from scripts.probe_sse_xbrl_revision_v4 import probe as probe_sse_xbrl


class StructuredPilotTest(unittest.TestCase):

    def test_sse_xbrl_probe_does_not_infer_versions_from_current_catalog(self):
        class Response(object):
            status_code = 200
            content = json.dumps({"result": [{
                "STOCK_ID": "600001", "REPORT_YEAR": "2024",
                "REPORT_PERIOD_ID": "5000", "ACTUAL_DATE": "2025-04-20",
            }]}).encode("utf-8")

            def json(self):
                return json.loads(self.content)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "config_version": "test", "source_version": "test-v1",
                "source_role": "revision_capability_probe_only",
                "raw_root": str(root / "raw"), "symbols": ["600001"],
                "required_revision_fields": [
                    "VERSION_ID", "REVISION_DATE", "SUPERSEDES_ID"],
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report, _ = probe_sse_xbrl(
                config, root / "output",
                fetcher=lambda _symbol: (Response(), {"test": True}))
            self.assertFalse(report["historical_revision_versions_exposed"])
            self.assertEqual(
                report["capability_gate_status"],
                "BLOCKED_OFFICIAL_XBRL_REVISION_CAPABILITY")

    def test_full_market_cap_panel_uses_announced_balance_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            (research / "raw").mkdir(parents=True)
            pd.DataFrame([{
                "date": 20250102, "close": 10,
                "outstanding_share": 80,
            }]).to_csv(research / "raw/sh600001.csv", index=False)
            sessions = root / "sessions.csv"
            pd.DataFrame({"date_time": [
                "2025-01-02", "2025-01-03"]}).to_csv(sessions, index=False)
            panel = root / "st.npz"
            np.savez_compressed(
                panel, dates=np.array([20250102]),
                symbols=np.array(["sh600001"]), known=np.array([[True]]),
                is_st=np.array([[False]]),
                source_role=np.array(["st_exclusion_only"]))
            events = root / "events.jsonl"
            events.write_text("", encoding="utf-8")
            facts = root / "facts.jsonl"
            facts.write_text(json.dumps({
                "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
                "raw_value": 100, "unit_scale": 1.0,
                "announcement": "2024-12-31",
                "report_period": "2024-12-31", "source_key": "balance-1",
                "raw_payload_sha256": "a" * 64,
            }) + "\n", encoding="utf-8")
            config = {
                "trading_sessions_source": str(sessions),
                "strict_rules": {
                    "share_reconciliation_relative_tolerance": .005,
                    "total_market_cap_coverage_min": .995,
                    "float_market_cap_coverage_min": .995,
                },
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report, paths = materialize_full_market_cap(
                events, research, panel, root / "output", config,
                balance_facts_path=facts)
            self.assertEqual(report["coverage_gate_status"],
                             "PASS_ELIGIBLE_MARKET_CAP_COVERAGE")
            with np.load(paths["panel"], allow_pickle=False) as payload:
                self.assertEqual(payload["total_market_cap"][0, 0], 1000)
                self.assertEqual(payload["total_share_source_code"][0, 0], 2)

    def test_balance_fallback_runner_rejects_parallel_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"max_concurrency": 1}),
                                   encoding="utf-8")
            mapping_path = root / "mapping.json"
            mapping_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sequentially"):
                run_balance_fallback_chunks(
                    root / "universe", root / "collection", config_path,
                    mapping_path, workers=2)

    def test_balance_fallback_chunk_is_injectable_and_marks_revision_scope(self):
        class Adapter(object):
            __name__ = "fake_balance"

            def __call__(self, symbol):
                self.symbol = symbol
                return pd.DataFrame([{
                    "REPORT_DATE": "2024-12-31",
                    "NOTICE_DATE": "2025-04-20",
                    "UPDATE_DATE": "2025-04-20", "CURRENCY": "CNY",
                    "SHARE_CAPITAL": 1000, "TOTAL_ASSETS": 2000,
                }])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chunk = root / "chunk"
            chunk.mkdir()
            (chunk / "fallback_symbols.json").write_text(json.dumps({
                "symbols": ["sh600001"], "records": [{
                    "symbol": "sh600001", "security_status": "delisted",
                }],
            }), encoding="utf-8")
            config = {
                "config_version": "test", "adapter_version": "test-v1",
                "source_role": "total_share_fallback_candidate",
                "raw_root": str(root / "raw"),
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            mapping = {"fields": {
                "SHARE_CAPITAL": {
                    "standard_field": "total_shares",
                    "statement_types": ["balance"],
                    "value_kind": "instant"},
            }}
            adapter = Adapter()
            report, paths = collect_balance_fallback(
                config, mapping, chunk, root / "output",
                adapters={("balance", True): adapter})
            self.assertEqual(adapter.symbol, "SH600001")
            self.assertEqual(report["symbols_with_facts"], 1)
            self.assertFalse(report["revision_history_complete"])
            fact = json.loads(paths["facts"].read_text(encoding="utf-8"))
            self.assertEqual(fact["raw_field"], "SHARE_CAPITAL")
            self.assertFalse(fact["revision_history_complete"])

    def test_balance_fallback_converts_only_verified_no_data_to_empty(self):
        class BrokenAdapter(object):
            __name__ = "broken_balance"

            def __call__(self, _symbol):
                raise TypeError("provider returned null result")

        class Response(object):
            status_code = 200
            content = b'{"result":null}'

            def json(self):
                return {"result": None}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chunk = root / "chunk"
            chunk.mkdir()
            (chunk / "fallback_symbols.json").write_text(json.dumps({
                "symbols": ["sh600001"], "records": [{
                    "symbol": "sh600001", "security_status": "delisted",
                }],
            }), encoding="utf-8")
            config = {
                "config_version": "test", "adapter_version": "test-v1",
                "source_role": "total_share_fallback_candidate",
                "raw_root": str(root / "raw"),
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report, _ = collect_balance_fallback(
                config, {"fields": {}}, chunk, root / "output",
                adapters={("balance", True): BrokenAdapter()},
                empty_probe=lambda _symbol, _delisted: (
                    Response(), {"probe": True}))
            self.assertEqual(report["failures"], [])
            self.assertEqual(report["empty_symbols"], ["sh600001"])

    def test_fallback_universe_contains_only_eligible_missing_cap_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel = root / "cap.npz"
            np.savez_compressed(
                panel, dates=np.array([20250102, 20250103]),
                symbols=np.array(["sh600001", "sz000001"]),
                total_market_cap=np.array([[np.nan, np.nan], [100, np.nan]]),
                eligible_non_st_price_day=np.array(
                    [[True, False], [True, True]]))
            master = root / "master.csv"
            pd.DataFrame([{
                "symbol": "sh600001", "exchange": "sh",
                "status": "listed", "delist_date": "",
            }, {
                "symbol": "sz000001", "exchange": "sz",
                "status": "delisted", "delist_date": "2025-01-03",
            }]).to_csv(master, index=False)
            report, _ = freeze_fallback_universe(
                panel, master, root / "frozen", chunk_size=1)
            self.assertEqual(report["fallback_symbol_count"], 2)
            self.assertEqual(report["missing_eligible_security_days"], 2)
            self.assertEqual(report["chunk_count"], 2)

    def test_full_market_cap_panel_is_pit_and_uses_eligible_denominator(self):
        events = [
            (20250102, 20241231, "2025-01-02T09:25:00+08:00", "a", 100, 80),
            (20250103, 20240101, "2025-01-03T09:25:00+08:00", "b", 90, 70),
            (20250106, 20250105, "2025-01-06T09:25:00+08:00", "c", 120, 90),
        ]
        states = _asof_states(events, np.array([20250102, 20250103, 20250106]))
        self.assertEqual(states[:, 0].tolist(), [100, 100, 120])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            (research / "raw").mkdir(parents=True)
            pd.DataFrame([{
                "date": 20250102, "close": 10,
                "outstanding_share": 80,
            }, {
                "date": 20250103, "close": 11,
                "outstanding_share": 80,
            }]).to_csv(research / "raw/sh600001.csv", index=False)
            panel = root / "st.npz"
            np.savez_compressed(
                panel, dates=np.array([20250102, 20250103]),
                symbols=np.array(["sh600001"]),
                known=np.array([[True], [True]]),
                is_st=np.array([[True], [False]]),
                source_role=np.array(["st_exclusion_only"]))
            event_path = root / "events.jsonl"
            event_path.write_text(json.dumps({
                "symbol": "sh600001",
                "effective_at": "2025-01-03T09:25:00+08:00",
                "event_date": "2025-01-02", "total_shares": 100,
                "float_shares": 80, "source_key": "event-1",
            }) + "\n", encoding="utf-8")
            config = {
                "strict_rules": {
                    "share_reconciliation_relative_tolerance": .005,
                    "total_market_cap_coverage_min": .995,
                    "float_market_cap_coverage_min": .995,
                },
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report, paths = materialize_full_market_cap(
                event_path, research, panel, root / "output", config)
            self.assertEqual(report["eligible_non_st_price_days"]
                             ["security_days"], 1)
            self.assertEqual(report["coverage_gate_status"],
                             "PASS_ELIGIBLE_MARKET_CAP_COVERAGE")
            with np.load(paths["panel"], allow_pickle=False) as payload:
                self.assertTrue(np.isnan(payload["total_market_cap"][0, 0]))
                self.assertEqual(payload["total_market_cap"][1, 0], 1100)
                self.assertEqual(payload["eligible_non_st_price_day"]
                                 [:, 0].tolist(), [False, True])
                self.assertEqual(payload["local_float_share_known"]
                                 [:, 0].tolist(), [False, True])

    def test_cninfo_share_audit_separates_collection_from_fact_coverage(self):
        class Response(object):
            status_code = 200

            def __init__(self, records):
                self.content = json.dumps({"records": records}).encode("utf-8")

            def json(self):
                return json.loads(self.content)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.csv"
            pd.DataFrame([{
                "symbol": symbol, "list_date": "2020-01-01",
                "delist_date": "", "exchange": "sh", "status": "listed",
            } for symbol in ("sh600001", "sh600002")]).to_csv(
                master, index=False)
            sessions = root / "sessions.csv"
            pd.DataFrame({"date_time": ["2025-04-21"]}).to_csv(
                sessions, index=False)
            config = {
                "config_version": "test", "source_version": "test-v1",
                "source_role": "primary_share_capital_candidate",
                "security_master": str(master),
                "trading_sessions_source": str(sessions),
                "raw_root": str(root / "raw"),
                "history_start_date": "2020-01-01",
                "history_end_date": "2025-04-21", "chunk_size": 2,
                "max_concurrency": 1,
                "fields": {"unit_scale": 10000.0},
                "strict_rules": {"minimum_symbols_with_events": .995},
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            freeze_share_universe(config, root / "frozen")

            def fetcher(code, _start, _end):
                records = [] if code == "600002" else [{
                    "DECLAREDATE": "2025-04-20",
                    "VARYDATE": "2025-03-31",
                    "F003N": 120.00000000001,
                    "F022N": 80.00000000001,
                }]
                return Response(records), {"code": code}

            collect_share_chunk(
                config, root / "frozen/chunks/0000",
                root / "collected/chunks/0000", fetcher=fetcher)
            report = build_cninfo_share_audit(
                root / "frozen", [root / "collected"], config)
            self.assertEqual(report["collection_gate_status"],
                             "PASS_CNINFO_SHARE_COLLECTION_GATE")
            self.assertEqual(report["primary_fact_coverage_gate_status"],
                             "BLOCKED_CNINFO_PRIMARY_SHARE_COVERAGE")
            self.assertEqual(report["symbols_with_events"], 1)
            self.assertEqual(report["explicit_empty_symbol_count"], 1)
            self.assertEqual(report["invalid_rows"], {})

    def test_cninfo_share_runner_rejects_parallel_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"max_concurrency": 1}),
                                   encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sequentially"):
                run_share_chunks(
                    root / "universe", root / "collection", config_path,
                    workers=2)

    def test_full_share_universe_freeze_and_injected_chunk_collection(self):
        class Response(object):
            status_code = 200
            content = json.dumps({"records": [{
                "DECLAREDATE": "2025-04-20", "VARYDATE": "2025-03-31",
                "F003N": 120.0, "F022N": 80.0,
            }]}).encode("utf-8")

            def json(self):
                return json.loads(self.content)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.csv"
            pd.DataFrame([{
                "symbol": "sh600001", "list_date": "2020-01-01",
                "delist_date": "", "exchange": "sh", "status": "listed",
            }]).to_csv(master, index=False)
            sessions = root / "sessions.csv"
            pd.DataFrame({"date_time": [
                "2025-04-21", "2025-04-22"]}).to_csv(sessions, index=False)
            config = {
                "config_version": "test", "source_version": "test-v1",
                "source_role": "primary_share_capital_candidate",
                "security_master": str(master),
                "trading_sessions_source": str(sessions),
                "raw_root": str(root / "raw"),
                "history_start_date": "2019-01-01",
                "history_end_date": "2025-04-22", "chunk_size": 100,
                "max_concurrency": 1,
                "fields": {"unit_scale": 10000.0},
                "strict_rules": {},
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            manifest, _ = freeze_share_universe(config, root / "frozen")
            self.assertEqual(manifest["security_count"], 1)

            def fetcher(code, start, end):
                self.assertEqual(code, "600001")
                self.assertEqual(start, "20200101")
                self.assertEqual(end, "20250422")
                return Response(), {"code": code, "start": start, "end": end}

            report, paths = collect_share_chunk(
                config, root / "frozen/chunks/0000", root / "collected",
                fetcher=fetcher)
            self.assertEqual(report["status"],
                             "COLLECTED_CNINFO_SHARE_CHUNK")
            self.assertEqual(report["share_event_count"], 1)
            event = json.loads(paths["events"].read_text(encoding="utf-8"))
            self.assertEqual(event["total_shares"], 1200000)
            self.assertEqual(event["float_shares"], 800000)

    def test_market_cap_coverage_uses_non_st_eligible_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            panel = Path(directory) / "st.npz"
            np.savez_compressed(
                panel, dates=np.array([20250102, 20250103]),
                symbols=np.array(["sh600001"]),
                known=np.array([[True], [True]]),
                is_st=np.array([[True], [False]]),
                source_role=np.array(["st_exclusion_only"]))
            rows = [{
                "symbol": "sh600001", "date": 20250102,
                "raw_close": 10, "total_market_cap": 100,
                "float_market_cap": None,
                "reconciliation_status": "MISSING",
                "conflict_reasons": [],
            }, {
                "symbol": "sh600001", "date": 20250103,
                "raw_close": 10, "total_market_cap": 100,
                "float_market_cap": 80,
                "reconciliation_status": "CONFLICT",
                "conflict_reasons": ["FLOAT_PRIMARY_VS_DAILY"],
            }]
            config = {
                "strict_rules": {
                    "total_market_cap_coverage_min": .995,
                    "float_market_cap_coverage_min": .995,
                },
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report = build_eligible_cap_audit(rows, panel, config)
            self.assertEqual(report["raw_all_price_days"]
                             ["float_market_cap_coverage"], .5)
            self.assertEqual(report["eligible_non_st_price_days"]
                             ["float_market_cap_coverage"], 1.0)
            self.assertEqual(report["exclusion_reason_counts"], {
                "KNOWN_ST": 1})
            self.assertEqual(report["coverage_gate_status"],
                             "PASS_ELIGIBLE_MARKET_CAP_COVERAGE")
            self.assertEqual(report["reconciliation_gate_status"],
                             "BLOCKED_SHARE_RECONCILIATION")
            self.assertEqual(report["exchange_eligible_coverage"]["sh"]
                             ["conflict_rows"], 1)

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
                "coverage_gates": {
                    "lifecycle_and_st_known_min": .995,
                    "total_market_cap_min": .995,
                    "float_market_cap_min": .995,
                },
            }
            report = blocked_report(
                config, root, root / "missing.jsonl", st_audit)
            self.assertNotIn("shanghai_historical_st_incomplete",
                             report["blockers"])
            self.assertTrue(report["st_coverage"]
                            ["full_market_exclusion_gate_passed"])
            self.assertEqual(report["gate_status"], "BLOCKED_DATA_GATE")

    def test_main_coverage_report_clears_only_total_cap_subgate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pd.DataFrame([{"symbol": "sh600001"}]).to_csv(
                root / "security_master.csv", index=False)
            cap_audit = root / "cap.json"
            cap_audit.write_text(json.dumps({
                "eligible_non_st_price_days": {
                    "total_market_cap_coverage": .9996,
                    "float_market_cap_coverage": .984,
                },
                "reconciliation_gate_status": "BLOCKED_SHARE_RECONCILIATION",
            }), encoding="utf-8")
            config = {
                "strategy_version": "test",
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
                "economic_themes": ["value"],
                "coverage_gates": {
                    "lifecycle_and_st_known_min": .995,
                    "total_market_cap_min": .995,
                    "float_market_cap_min": .995,
                },
            }
            report = blocked_report(
                config, root, root / "missing.jsonl",
                market_cap_audit_path=cap_audit)
            self.assertTrue(report["market_cap"]
                            ["total_market_cap_available"])
            self.assertNotIn(
                "total_shares_and_total_market_cap_not_materialized",
                report["blockers"])
            self.assertIn(
                "float_shares_and_float_market_cap_below_threshold",
                report["blockers"])
            self.assertIn(
                "share_market_cap_reconciliation_conflicts",
                report["blockers"])
            self.assertIn(
                "historical_fundamental_revision_versions_incomplete",
                report["blockers"])

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
