"""M0 pre-registration and frozen-baseline tests for multifactor v4."""
import json
import tempfile
import unittest
from pathlib import Path

from abupy.AlphaBu.ABuTrialRegistry import (
    read_trial_registry, register_trial,
)
from scripts.freeze_multifactor_snapshot_v4 import (
    validate_experiment_catalog, verify_frozen_baseline,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "configs/selection/alpha158_multifactor_experiments_v4.json"
BASELINE = ROOT / "configs/selection/alpha158_multifactor_baseline_v4.json"


class MultifactorV4M0Test(unittest.TestCase):

    def test_catalog_has_frozen_unique_order_and_is_research_only(self):
        payload = validate_experiment_catalog(CATALOG)
        self.assertEqual(
            payload["required_order"],
            ["D0", "D1", "B2", "B3", "B4", "S1", "B5", "F1"],
        )
        self.assertTrue(payload["registered_before_v4_returns"])
        self.assertNotEqual(payload["status"], "APPROVED_FOR_LIVE")
        self.assertTrue(all(
            item["failure_status"] != "APPROVED_FOR_LIVE"
            for item in payload["experiments"]
        ))

    def test_duplicate_experiment_id_is_rejected(self):
        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        payload["experiments"][1]["experiment_id"] = "D0"
        payload["required_order"][1] = "D0"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_experiment_catalog(path)

    def test_changed_registered_experiment_is_rejected_by_baseline(self):
        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        payload["experiments"][0]["hypothesis"] += " changed"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different experiment catalog"):
                verify_frozen_baseline(
                    ROOT, BASELINE, path, verify_external=False,
                )

    def test_repo_baseline_and_catalog_hashes_match(self):
        report = verify_frozen_baseline(
            ROOT, BASELINE, CATALOG, verify_external=False,
        )
        self.assertEqual(report["status"], "VERIFIED_RESEARCH_ONLY")
        self.assertIn("alpha158_lite_low_turnover_v3",
                      report["frozen_strategy_versions"])

    def test_trial_ids_are_append_only_and_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.jsonl"
            register_trial(path, "D0-test", "coverage", {"version": 1})
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_trial(path, "D0-test", "changed", {"version": 2})
            record = json.loads(path.read_text(encoding="utf-8"))
            record["configuration"]["version"] = 99
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                read_trial_registry(path)


if __name__ == "__main__":
    unittest.main()
