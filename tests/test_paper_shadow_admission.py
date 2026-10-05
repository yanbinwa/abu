import json
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuPaperShadowAdmission import PaperShadowAdmissionGate


class PaperShadowAdmissionGateTest(unittest.TestCase):

    def accepted(self):
        return {"passed": True, "status": "MINUTE_DATA_ONLY_ACCEPTED",
                "observations": [{"trading_session": 20261009, "passed": True}]}

    def test_pending_real_data_cannot_create_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "has not passed"):
                PaperShadowAdmissionGate.create(
                    Path(directory) / "gate.json",
                    {"passed": False, "status": "PENDING", "observations": []},
                    database_path=Path(directory) / "db", snapshot_root=directory,
                    source_commit="abc", created_at="2026-10-09T18:00:00+08:00")

    def test_accepted_result_creates_bound_tamper_evident_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "gate.json"
            created = PaperShadowAdmissionGate.create(
                path, self.accepted(), database_path=root / "db",
                snapshot_root=root / "snapshots", source_commit="abc",
                created_at="2026-10-09T18:00:00+08:00")
            verified = PaperShadowAdmissionGate.verify(
                path, database_path=root / "db", snapshot_root=root / "snapshots",
                source_commit="abc")
            self.assertEqual(created, verified)
            with self.assertRaisesRegex(ValueError, "source mismatch"):
                PaperShadowAdmissionGate.verify(
                    path, database_path=root / "db",
                    snapshot_root=root / "snapshots", source_commit="other")
            payload = json.loads(path.read_text())
            payload["status"] = "tampered"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                PaperShadowAdmissionGate.verify(
                    path, database_path=root / "db",
                    snapshot_root=root / "snapshots")


if __name__ == "__main__":
    unittest.main()
