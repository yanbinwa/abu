"""Full-market structured-fundamental pipeline tests."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.audit_fundamental_theme_availability_v1 import build_audit
from scripts.collect_full_market_fundamental_chunk_v1 import collect
from scripts.freeze_full_market_fundamental_universe_v1 import freeze
from scripts.merge_full_market_fundamental_v1 import merge
from scripts.run_full_market_fundamental_chunks_v1 import run_chunks


FIELDS = {
    "TOTAL_OPERATE_INCOME": {
        "standard_field": "revenue", "statement_types": ["income"],
        "value_kind": "cumulative"},
    "OPERATE_COST": {
        "standard_field": "operating_cost", "statement_types": ["income"],
        "value_kind": "cumulative"},
    "PARENT_NETPROFIT": {
        "standard_field": "parent_net_profit", "statement_types": ["income"],
        "value_kind": "cumulative"},
    "NETCASH_OPERATE": {
        "standard_field": "operating_cash_flow",
        "statement_types": ["cashflow"], "value_kind": "cumulative"},
    "CONSTRUCT_LONG_ASSET": {
        "standard_field": "capital_expenditure_cash",
        "statement_types": ["cashflow"], "value_kind": "cumulative"},
    "TOTAL_ASSETS": {
        "standard_field": "total_assets", "statement_types": ["balance"],
        "value_kind": "instant"},
    "TOTAL_LIABILITIES": {
        "standard_field": "total_liabilities",
        "statement_types": ["balance"], "value_kind": "instant"},
    "TOTAL_PARENT_EQUITY": {
        "standard_field": "parent_equity", "statement_types": ["balance"],
        "value_kind": "instant"},
}


def config(root, master):
    return {
        "config_version": "test", "adapter_version": "test-v1",
        "source_role": "primary_numeric_candidate",
        "security_master": str(master), "raw_root": str(root / "raw"),
        "history_start_date": "2020-01-01",
        "history_end_date": "2026-09-30", "chunk_size": 1,
        "max_concurrency": 1, "validation_sample_size": 1,
        "required_statements": ["income", "balance", "cashflow"],
        "strict_rules": {
            "statement_symbol_coverage_min": 1.0,
            "required_field_symbol_coverage_min": 1.0,
            "historical_revision_versions_required": True,
        },
        "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
    }


class Adapter(object):
    def __init__(self, statement):
        self.statement = statement
        self.__name__ = "fake_" + statement

    def __call__(self, _symbol):
        common = {
            "REPORT_DATE": "2024-12-31", "NOTICE_DATE": "2025-04-20",
            "UPDATE_DATE": "2025-04-20", "CURRENCY": "CNY",
        }
        if self.statement == "income":
            common.update({"TOTAL_OPERATE_INCOME": 100, "OPERATE_COST": 60,
                           "PARENT_NETPROFIT": 10})
        elif self.statement == "balance":
            common.update({"TOTAL_ASSETS": 200, "TOTAL_LIABILITIES": 80,
                           "TOTAL_PARENT_EQUITY": 120})
        else:
            common.update({"NETCASH_OPERATE": 12,
                           "CONSTRUCT_LONG_ASSET": 3})
        return pd.DataFrame([common])


class FullMarketFundamentalPipelineTest(unittest.TestCase):

    def _master(self, root):
        path = root / "master.csv"
        pd.DataFrame([{
            "symbol": "sh600001", "list_date": "2000-01-01",
            "delist_date": "", "exchange": "sh", "status": "listed",
        }]).to_csv(path, index=False)
        return path

    def test_freeze_collect_and_merge_keep_revision_gate_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root, self._master(root))
            universe = root / "universe"
            manifest, _ = freeze(cfg, universe)
            self.assertEqual(manifest["security_count"], 1)
            adapters = {(kind, False): Adapter(kind)
                        for kind in cfg["required_statements"]}
            report, _ = collect(
                cfg, {"fields": FIELDS}, universe / "chunks/0000",
                root / "collection/chunks/0000", adapters=adapters,
                ingested_at="2026-10-06T00:00:00+00:00")
            self.assertEqual(
                report["status"], "COLLECTED_FULL_MARKET_FUNDAMENTAL_CHUNK")
            merged, _ = merge(
                universe, [root / "collection"], root / "merged", cfg,
                {"fields": FIELDS})
            self.assertEqual(
                merged["collection_gate_status"],
                "PASS_FULL_MARKET_FUNDAMENTAL_COLLECTION_GATE")
            self.assertEqual(
                merged["coverage_gate_status"],
                "PASS_FULL_MARKET_FUNDAMENTAL_COVERAGE_GATE")
            self.assertEqual(merged["revision_gate_status"],
                             "BLOCKED_HISTORICAL_REVISION_VERSIONS")

    def test_runner_rejects_parallel_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({"max_concurrency": 1}),
                                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sequentially"):
                run_chunks(root, root / "collection", cfg_path,
                           root / "mapping.json", workers=2)

    def test_theme_audit_requires_strictly_prior_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            symbols = root / "symbols.json"
            symbols.write_text(json.dumps({"symbols": ["sh600001"]}),
                               encoding="utf-8")
            facts = root / "facts.jsonl"
            rows = []
            periods = ("2023-12-31", "2024-03-31", "2024-06-30",
                       "2024-09-30", "2024-12-31")
            field_statements = {
                "TOTAL_OPERATE_INCOME": "income",
                "OPERATE_COST": "income", "PARENT_NETPROFIT": "income",
                "NETCASH_OPERATE": "cashflow",
                "CONSTRUCT_LONG_ASSET": "cashflow",
                "TOTAL_ASSETS": "balance",
                "TOTAL_LIABILITIES": "balance",
                "TOTAL_PARENT_EQUITY": "balance",
            }
            for period_index, period in enumerate(periods):
                for field, statement in field_statements.items():
                    rows.append({
                        "symbol": "sh600001", "statement_type": statement,
                        "report_period": period,
                        "announcement": ("2025-01-02" if period ==
                                         "2024-12-31" else "2024-01-01"),
                        "revision_id": str(period_index), "raw_field": field,
                        "raw_value": period_index + 1, "unit_scale": 1,
                        "revision_history_complete": False,
                    })
            facts.write_text("".join(json.dumps(row) + "\n" for row in rows),
                             encoding="utf-8")
            report = build_audit(
                facts, symbols, ["2025-01-02", "2025-01-03"])
            first, second = report["daily_availability"]
            self.assertEqual(first["theme_coverage"]["value"], 0)
            self.assertEqual(second["theme_coverage"]["value"], 1)
            self.assertEqual(second["theme_coverage"]["quality"], 1)
            self.assertEqual(second["theme_coverage"]["investment"], 1)
            self.assertEqual(report["revision_gate_status"],
                             "BLOCKED_HISTORICAL_REVISION_VERSIONS")


if __name__ == "__main__":
    unittest.main()
