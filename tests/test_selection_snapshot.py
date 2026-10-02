"""Tests for deterministic snapshot freezing and field coverage auditing."""

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.audit_selection_coverage import audit_coverage, write_outputs
from scripts.freeze_selection_snapshot import (
    build_file_inventory,
    build_snapshot_manifest,
    stable_snapshot_id,
    write_new_json,
)


class SelectionSnapshotTest(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.signal = self.root / "signal"
        self.research = self.root / "research"
        self.raw = self.research / "raw"
        self.signal.mkdir()
        self.raw.mkdir(parents=True)
        pd.DataFrame({
            "code": ["000001", "600000"],
            "name": ["A", "B"],
            "list_date": ["2000-01-01", "2000-01-01"],
            "delist_date": [None, None],
            "exchange": ["sz", "sh"],
            "symbol": ["sz000001", "sh600000"],
            "status": ["listed", "listed"],
        }).to_csv(self.research / "security_master.csv", index=False)
        pd.DataFrame({
            "date": [20260102, 20260105], "open": [10, 11],
            "high": [11, 12], "low": [9, 10], "close": [10.5, 11.5],
            "volume": [100, 200], "amount": [1050, 2300],
            "outstanding_share": [1000, 1000], "turnover": [0.1, 0.2],
        }).to_csv(self.raw / "sz000001.csv", index=False)
        pd.DataFrame({
            "date": [20260102], "open": [20], "high": [21], "low": [19],
            "close": [20.5], "volume": [300], "amount": [None],
            "outstanding_share": [None], "turnover": [None],
        }).to_csv(self.raw / "sh600000.csv", index=False)
        (self.signal / "sz000001_test.csv").write_text("date,close\n20260102,10.5\n")
        (self.signal / "sh600000_test.csv").write_text("date,close\n20260102,20.5\n")
        (self.research / "manifest.json").write_text(json.dumps({
            "start": "20260101", "end": "20260105",
            "price_providers": {"tencent": 1, "sina": 1},
            "limitations": ["test limitation"],
        }))

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_is_stable_and_changes_with_content(self):
        roots = [("signal", self.signal), ("research", self.research)]
        first = build_file_inventory(roots)
        second = build_file_inventory(list(reversed(roots)))
        self.assertEqual(first, second)
        first_id = stable_snapshot_id(first)
        path = self.raw / "sh600000.csv"
        path.write_text(path.read_text() + "\n")
        changed_id = stable_snapshot_id(build_file_inventory(roots))
        self.assertNotEqual(first_id, changed_id)

    def test_manifest_records_environment_and_limitations(self):
        manifest = build_snapshot_manifest(
            self.signal, self.research, self.root,
            created_at="2026-10-02T00:00:00+08:00",
        )
        self.assertTrue(manifest["snapshot_id"].startswith("sha256:"))
        self.assertEqual(manifest["start_date"], "20260101")
        self.assertEqual(manifest["known_limitations"], ["test limitation"])
        self.assertGreater(len(manifest["files"]), 0)

    def test_refuses_implicit_overwrite(self):
        path = self.root / "manifest.json"
        write_new_json(path, {"a": 1})
        with self.assertRaises(FileExistsError):
            write_new_json(path, {"a": 2})
        write_new_json(path, {"a": 2}, force=True)
        self.assertEqual(json.loads(path.read_text()), {"a": 2})

    def test_coverage_uses_actual_fields(self):
        days, symbols, provenance, summary = audit_coverage(
            self.signal, self.research
        )
        self.assertEqual(summary["security_count"], 2)
        self.assertEqual(summary["raw_file_count"], 2)
        self.assertEqual(summary["complete_attention_symbols"], 1)
        self.assertEqual(summary["incomplete_attention_symbols"], 1)
        self.assertEqual(len(days), 2)
        hint = provenance.set_index("symbol").loc["sh600000", "provider_hint"]
        self.assertEqual(hint, "tencent_like_or_incomplete")
        output = self.root / "out"
        write_outputs(output, days, symbols, provenance, summary)
        self.assertTrue((output / "coverage_summary.json").exists())


if __name__ == "__main__":
    unittest.main()
