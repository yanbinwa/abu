import io
import json
import unittest
from unittest import mock

from scripts.send_wecom_strategy import (
    DeliveryError, main, send_text, validate_content, validate_webhook,
)


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


if __name__ == "__main__":
    unittest.main()
