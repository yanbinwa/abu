import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.freeze_baostock_float_share_fallback_v1 import freeze
from scripts.materialize_full_market_cap_panel_v6 import materialize


class BaoStockFloatShareFallbackTest(unittest.TestCase):

    def _panel(self, root, dates):
        path = root / "st.npz"
        np.savez_compressed(
            path, dates=np.array(dates), symbols=np.array(["sh600001"]),
            known=np.ones((len(dates), 1), dtype=bool),
            is_st=np.zeros((len(dates), 1), dtype=bool),
            source_role=np.array(["st_exclusion_only"]))
        return path

    def _config(self, sessions=None):
        config = {
            "strict_rules": {
                "share_reconciliation_relative_tolerance": .005,
                "total_market_cap_coverage_min": .0,
                "float_market_cap_coverage_min": .0,
            },
            "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
        }
        if sessions is not None:
            config["trading_sessions_source"] = str(sessions)
        return config

    def test_freeze_uses_only_cninfo_explicit_empty_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.csv"
            pd.DataFrame([{
                "symbol": "sh600001", "list_date": "2020-01-01",
                "delist_date": "", "exchange": "sh", "status": "listed",
            }, {
                "symbol": "sz000001", "list_date": "2010-01-01",
                "delist_date": "2024-12-31", "exchange": "sz",
                "status": "delisted",
            }]).to_csv(master, index=False)
            coverage = root / "coverage.json"
            coverage.write_text(json.dumps({
                "explicit_empty_symbol_count": 1,
                "explicit_empty_symbols": ["sz000001"],
            }), encoding="utf-8")
            config = {
                "config_version": "test", "source_version": "test-v1",
                "source_role": "float_share_gap_fallback_only",
                "security_master": str(master),
                "cninfo_coverage_audit": str(coverage),
                "history_start_date": "2019-01-01",
                "history_end_date": "2026-09-30",
                "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
            }
            report, _ = freeze(config, root / "frozen")
            self.assertEqual(report["security_count"], 1)
            payload = json.loads((root / "frozen/pilot_symbols.json").read_text())
            self.assertEqual(payload["symbols"], ["sz000001"])
            self.assertEqual(payload["symbol_ranges"]["sz000001"], {
                "start_date": "2019-01-01", "end_date": "2024-12-31",
            })

    def test_float_source_priority_is_cninfo_then_local_then_baostock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            (research / "raw").mkdir(parents=True)
            dates = [20250102, 20250103, 20250106, 20250107]
            pd.DataFrame([
                {"date": dates[0], "close": 10, "outstanding_share": 75},
                {"date": dates[1], "close": 10, "outstanding_share": None},
                {"date": dates[2], "close": 10, "outstanding_share": None},
                {"date": dates[3], "close": 10, "outstanding_share": 75},
            ]).to_csv(research / "raw/sh600001.csv", index=False)
            events = root / "events.jsonl"
            events.write_text(json.dumps({
                "symbol": "sh600001",
                "effective_at": "2025-01-06T09:25:00+08:00",
                "event_date": "2025-01-03", "total_shares": 100,
                "float_shares": 80, "source_key": "event-1",
            }) + "\n", encoding="utf-8")
            fallback = root / "baostock.jsonl"
            fallback.write_text(json.dumps({
                "symbol": "sh600001", "date": dates[1],
                "derived_float_shares": 70,
                "source_role": "float_share_gap_fallback_only",
            }) + "\n", encoding="utf-8")
            report, paths = materialize(
                events, research, self._panel(root, dates), root / "output",
                self._config(), baostock_fallback_path=fallback)
            self.assertEqual(report["eligible_float_share_source_counts"], {
                "baostock_turnover_derived": 1,
                "cninfo_share_change": 2,
                "local_daily": 1,
            })
            with np.load(paths["panel"], allow_pickle=False) as payload:
                self.assertEqual(payload["float_market_cap"][:, 0].tolist(),
                                 [750, 700, 800, 800])
                self.assertEqual(payload["float_share_source_code"][:, 0].tolist(),
                                 [1, 3, 2, 2])

    def test_baostock_above_known_total_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            (research / "raw").mkdir(parents=True)
            pd.DataFrame([{
                "date": 20250103, "close": 10, "outstanding_share": None,
            }]).to_csv(research / "raw/sh600001.csv", index=False)
            events = root / "events.jsonl"
            events.write_text("", encoding="utf-8")
            sessions = root / "sessions.csv"
            pd.DataFrame({"date_time": ["2025-01-02", "2025-01-03"]}).to_csv(
                sessions, index=False)
            facts = root / "facts.jsonl"
            facts.write_text(json.dumps({
                "symbol": "sh600001", "raw_field": "SHARE_CAPITAL",
                "raw_value": 100, "unit_scale": 1.0,
                "announcement": "2025-01-02", "report_period": "2024-12-31",
                "source_key": "balance-1", "raw_payload_sha256": "a" * 64,
            }) + "\n", encoding="utf-8")
            fallback = root / "baostock.jsonl"
            fallback.write_text(json.dumps({
                "symbol": "sh600001", "date": 20250103,
                "derived_float_shares": 101,
                "source_role": "float_share_gap_fallback_only",
            }) + "\n", encoding="utf-8")
            report, paths = materialize(
                events, research, self._panel(root, [20250103]),
                root / "output", self._config(sessions),
                balance_facts_path=facts, baostock_fallback_path=fallback)
            self.assertEqual(
                report["baostock_fallback_rejected_above_total_days"], 1)
            with np.load(paths["panel"], allow_pickle=False) as payload:
                self.assertTrue(np.isnan(payload["float_market_cap"][0, 0]))


if __name__ == "__main__":
    unittest.main()
