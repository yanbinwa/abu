import json
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuNotificationPipeline import DeliveryUnknown
from abupy.ServiceBu.ABuWeComLongConnectionTransport import (
    WeComLongConnectionOutboxTransport,
)


class WeComLongConnectionTransportTest(unittest.TestCase):

    def test_text_job_is_deterministic_and_receipt_completes_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "message.md"
            source.write_text("hello", encoding="utf-8")
            transport = WeComLongConnectionOutboxTransport(
                root / "runtime", timeout_seconds=0)
            with self.assertRaises(DeliveryUnknown):
                transport.send("TEXT", source, "event:TEXT")
            jobs = list((root / "runtime" / "outbox").glob("*.json"))
            self.assertEqual(1, len(jobs))
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertEqual("TEXT", job["deliveryKind"])
            jobs[0].rename(root / "runtime" / ("sent-" + jobs[0].name))
            remote = transport.send("TEXT", source, "event:TEXT")
            self.assertTrue(remote.startswith("wecom-long://txn-"))

    def test_image_is_copied_to_content_addressed_runtime_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chart.png"
            source.write_bytes(b"not-real-png-but-transport-is-format-agnostic")
            transport = WeComLongConnectionOutboxTransport(
                root / "runtime", timeout_seconds=0)
            with self.assertRaises(DeliveryUnknown):
                transport.send("CHART_IMAGE", source, "event:CHART_IMAGE")
            job_path = next((root / "runtime" / "outbox").glob("*.json"))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            asset = root / "runtime" / "outbox-assets" / job["assetName"]
            self.assertTrue(asset.is_file())
            self.assertEqual(source.read_bytes(), asset.read_bytes())


if __name__ == "__main__":
    unittest.main()
