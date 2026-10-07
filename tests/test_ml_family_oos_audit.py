"""M7 family OOS source and common-key audit tests."""
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from abupy.MLBu.ABuMLContracts import MLContractError
from abupy.MLBu.ABuMLFamilyOOSAudit import (
    audit_family_set, source_manifest_from_fold,
)


def write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def fold():
    return {
        "fold": 0, "train_end": 20240101, "train_label_end": 20240131,
        "validation_start": 20240201, "validation_end": 20240301,
        "validation_label_end": 20240329, "test_start": 20240401,
        "test_end": 20240430, "strict_label_non_overlap": True,
    }


class FamilyOOSAuditTest(unittest.TestCase):

    def test_manifest_rejects_label_maturity_overlap(self):
        invalid = fold()
        invalid["validation_label_end"] = 20240402
        with self.assertRaises(MLContractError):
            source_manifest_from_fold("family", invalid, "a"*64)

    def test_incomplete_family_set_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root/"dataset.json"
            write_json(report, {"status": "COMPLETE", "missing_signals": []})
            result = audit_family_set(root, report, families=("a", "b"))
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(result["incomplete_families"], ["a", "b"])

    def test_common_keys_pass_and_hash_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root/"dataset.json"
            write_json(report, {"status": "COMPLETE", "missing_signals": []})
            for family in ("a", "b"):
                directory = root/"families"/family
                directory.mkdir(parents=True)
                frame = pd.DataFrame({
                    "signal_asof": [20240401, 20240401],
                    "symbol": ["sz000001", "sz000002"], "fold": [0, 0]})
                path = directory/"oos_predictions.csv.gz"
                with gzip.GzipFile(path, "wb", mtime=0) as raw:
                    raw.write(frame.to_csv(index=False).encode())
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                write_json(directory/"fold_manifests.json", [fold()])
                write_json(directory/"completion.json", {
                    "status": "COMPLETE", "prediction_sha256": checksum,
                    "rows": 2})
            result = audit_family_set(root, report, families=("a", "b"))
            self.assertEqual(result["status"], "PASS")
            path = root/"families"/"b"/"oos_predictions.csv.gz"
            path.write_bytes(path.read_bytes()+b"changed")
            with self.assertRaises(MLContractError):
                audit_family_set(root, report, families=("a", "b"))


if __name__ == "__main__":
    unittest.main()
