import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from abupy.ServiceBu.ABuNotificationPipeline import (
    DeliveryUnknown, PermanentDeliveryError,
)
from abupy.ServiceBu.ABuWeComTransport import WeComWebhookTransport


WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-value"


class Response(object):
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, unused_limit):
        return json.dumps(self.payload).encode()


class WeComTransactionalTransportTest(unittest.TestCase):

    def test_text_and_image_use_real_wecom_payload_shapes(self):
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data.decode()))
            return Response({"errcode": 0, "errmsg": "ok"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = root / "trade.md"
            image = root / "trade.png"
            text.write_text("交易通知", encoding="utf-8")
            image.write_bytes(b"png-fixture")
            transport = WeComWebhookTransport(WEBHOOK, opener=opener)
            transport.send("TEXT", text, "id:text")
            transport.send("CHART_IMAGE", image, "id:image")
        self.assertEqual("markdown", requests[0]["msgtype"])
        self.assertEqual("image", requests[1]["msgtype"])
        self.assertIn("base64", requests[1]["image"])
        self.assertIn("md5", requests[1]["image"])

    def test_network_error_is_unknown_and_never_leaks_key(self):
        def broken(unused_request, timeout):
            raise URLError(WEBHOOK)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trade.md"
            path.write_text("trade", encoding="utf-8")
            with self.assertRaises(DeliveryUnknown) as raised:
                WeComWebhookTransport(WEBHOOK, opener=broken).send(
                    "TEXT", path, "id")
        self.assertNotIn("secret-value", str(raised.exception))

    def test_invalid_endpoint_fails_closed(self):
        with self.assertRaises(PermanentDeliveryError):
            WeComWebhookTransport("https://example.com/hook?key=x")


if __name__ == "__main__":
    unittest.main()
