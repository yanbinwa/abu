import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts.send_wecom_strategy import (
    DeliveryError, main, send_text, validate_content, validate_webhook,
)
from scripts.queue_wecom_strategy import queue_message


WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret"


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body[:size]


class SendWecomStrategyTest(unittest.TestCase):
    def test_posts_utf8_text_and_accepts_success(self):
        def opener(request, timeout):
            self.assertEqual(10, timeout)
            self.assertEqual("POST", request.get_method())
            self.assertEqual(
                {"msgtype": "text", "text": {"content": "今日策略：观望"}},
                json.loads(request.data),
            )
            return FakeResponse(b'{"errcode":0,"errmsg":"ok"}')

        send_text(WEBHOOK, "今日策略：观望", opener=opener)

    def test_rejects_wecom_error_and_does_not_echo_key(self):
        with self.assertRaisesRegex(DeliveryError, "errcode=93000") as caught:
            send_text(WEBHOOK, "日报", opener=lambda *_a, **_kw: FakeResponse(
                b'{"errcode":93000,"errmsg":"invalid webhook"}'))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_validates_endpoint_and_utf8_byte_limit(self):
        with self.assertRaises(DeliveryError):
            validate_webhook("https://example.com/cgi-bin/webhook/send?key=secret")
        with self.assertRaises(DeliveryError):
            validate_content("中" * 683)
        with self.assertRaises(DeliveryError):
            validate_content("  ")

    def test_dry_run_needs_no_webhook(self):
        with mock.patch("sys.stdin", io.StringIO("日报内容\n")), \
                mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(0, main(["--stdin", "--dry-run"]))
        self.assertEqual("日报内容\n", out.getvalue())

    def test_queue_message_is_atomic_and_keeps_target(self):
        with TemporaryDirectory() as directory:
            identifier = queue_message(
                "模拟盘买入提醒", Path(directory), target="user-1")
            paths = list(Path(directory).glob("*.json"))
            self.assertEqual(len(paths), 1)
            payload = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["id"], identifier)
            self.assertEqual(payload["content"], "模拟盘买入提醒")
            self.assertEqual(payload["target"], "user-1")
            self.assertFalse(list(Path(directory).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
