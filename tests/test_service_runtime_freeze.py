import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.freeze_abu_service_runtime import freeze_runtime


ROOT = Path(__file__).resolve().parents[1]


class ServiceRuntimeFreezeTest(unittest.TestCase):

    def test_release_is_minimal_immutable_and_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            release, manifest, created = freeze_runtime(
                Path(directory), commit="test-commit", source_root=ROOT)
            self.assertTrue(created)
            self.assertTrue((release / "abupy/ServiceBu/ABuServiceRuntime.py").is_file())
            self.assertTrue((release / "abupy/MarketBu/ABuMinuteBarStore.py").is_file())
            self.assertTrue((release / "abupy/MarketBu/ABuRealtimeMarket.py").is_file())
            subprocess.run(
                [sys.executable, "-B", "-c",
                 "from abupy.ServiceBu import MinuteSnapshotBuilder"],
                cwd=str(release), check=True)
            self.assertEqual(
                "# Minimal frozen package for ServiceBu only.\n",
                (release / "abupy/__init__.py").read_text(encoding="utf-8"))
            service = json.loads((release / "configs/service/service_v1.json").read_text(
                encoding="utf-8"))
            self.assertTrue(service["daily_data_policy_path"].startswith(str(release)))
            self.assertTrue(service["minute_shadow_config_path"].startswith(str(release)))
            self.assertTrue(service["intraday_sentiment_config_path"].startswith(
                str(release)))
            repeated, repeated_manifest, created_again = freeze_runtime(
                Path(directory), commit="test-commit", source_root=ROOT)
            self.assertFalse(created_again)
            self.assertEqual(release, repeated)
            self.assertEqual(manifest, repeated_manifest)

    def test_existing_release_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            release, unused_manifest, unused_created = freeze_runtime(
                Path(directory), commit="test-commit", source_root=ROOT)
            (release / "abupy/__init__.py").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash verification"):
                freeze_runtime(
                    Path(directory), commit="test-commit", source_root=ROOT)


if __name__ == "__main__":
    unittest.main()
