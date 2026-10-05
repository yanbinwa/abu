from __future__ import absolute_import

import base64
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .ABuNotificationPipeline import (
    DeliveryUnknown, PermanentDeliveryError, RetryableDeliveryError,
)


class WeComWebhookTransport(object):
    """WeCom group webhook adapter that never persists or reports its key."""

    def __init__(self, webhook_url, opener=None, timeout_seconds=10):
        self.webhook_url = self._validate(webhook_url)
        self.opener = opener or urlopen
        self.timeout_seconds = int(timeout_seconds)

    @staticmethod
    def _validate(value):
        parts = urlsplit(str(value))
        query = parse_qs(parts.query, keep_blank_values=True)
        if (parts.scheme != "https" or parts.netloc != "qyapi.weixin.qq.com" or
                parts.path != "/cgi-bin/webhook/send" or parts.fragment or
                set(query) != {"key"} or len(query["key"]) != 1 or
                not query["key"][0]):
            raise PermanentDeliveryError("invalid WeCom webhook configuration")
        return str(value)

    @staticmethod
    def _payload(part_kind, path):
        content = Path(path).read_bytes()
        if part_kind == "TEXT":
            text = content.decode("utf-8").strip()
            if not text:
                raise PermanentDeliveryError("empty notification text")
            return {"msgtype": "markdown", "markdown": {"content": text}}
        if part_kind == "CHART_IMAGE":
            if not content or len(content) > 2 * 1024 * 1024:
                raise PermanentDeliveryError("WeCom image must be 1..2MiB")
            return {"msgtype": "image", "image": {
                "base64": base64.b64encode(content).decode("ascii"),
                "md5": hashlib.md5(content).hexdigest(),  # WeCom protocol field.
            }}
        raise PermanentDeliveryError("unsupported WeCom notification part")

    def send(self, part_kind, path, idempotency_key):
        body = json.dumps(
            self._payload(part_kind, path), ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")
        request = Request(
            self.webhook_url, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST")
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read(8192).decode("utf-8"))
        except HTTPError as error:
            if int(error.code) >= 500:
                raise RetryableDeliveryError(
                    "WeCom HTTP service failure {}".format(error.code)) from None
            raise PermanentDeliveryError(
                "WeCom HTTP request rejected {}".format(error.code)) from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeError) as error:
            raise DeliveryUnknown(
                "WeCom request outcome unknown: {}".format(
                    type(error).__name__)) from None
        if not isinstance(result, dict):
            raise DeliveryUnknown("WeCom returned an invalid response")
        code = int(result.get("errcode", -1))
        if code == 0:
            return "wecom://{}".format(hashlib.sha256(
                idempotency_key.encode()).hexdigest()[:20])
        if code in (45009, 45011, 60020):
            raise RetryableDeliveryError(
                "WeCom temporarily rejected request errcode={}".format(code))
        raise PermanentDeliveryError(
            "WeCom rejected request errcode={}".format(code))
