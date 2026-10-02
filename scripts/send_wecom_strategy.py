#!/usr/bin/env python3
"""Send a prepared daily strategy report to a WeCom group webhook.

This module only transports a report. Strategy generation belongs to the
caller, so a historical backtest cannot accidentally become a live signal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


MAX_TEXT_BYTES = 2048
WEBHOOK_ENV = "WECOM_WEBHOOK_URL"


class DeliveryError(Exception):
    """A report could not be delivered or was rejected by WeCom."""


def validate_webhook(webhook: str) -> str:
    """Accept only the WeCom group message endpoint; never echo its secret key."""
    parts = urlsplit(webhook)
    keys = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme != "https" or parts.netloc != "qyapi.weixin.qq.com"
            or parts.path != "/cgi-bin/webhook/send" or parts.fragment
            or set(keys) != {"key"} or len(keys["key"]) != 1
            or not keys["key"][0]):
        raise DeliveryError("无效的企业微信群机器人 Webhook 地址")
    return webhook


def validate_content(content: str) -> str:
    content = content.strip()
    if not content:
        raise DeliveryError("策略内容为空，未发送")
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise DeliveryError(f"策略内容超过 {MAX_TEXT_BYTES} 字节，请先精简日报")
    return content


def send_text(webhook: str, content: str, *, opener=urlopen) -> None:
    """Post one text message and require both HTTP and WeCom success."""
    validate_webhook(webhook)
    content = validate_content(content)
    payload = json.dumps({"msgtype": "text", "text": {"content": content}},
                         ensure_ascii=False).encode("utf-8")
    request = Request(webhook, data=payload,
                      headers={"Content-Type": "application/json; charset=utf-8"},
                      method="POST")
    try:
        with opener(request, timeout=10) as response:
            result = json.loads(response.read(8192).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        # HTTPError and URLError may contain the URL (and its secret key).
        raise DeliveryError(f"企业微信请求失败：{type(error).__name__}") from None
    if not isinstance(result, dict) or result.get("errcode") != 0:
        code = result.get("errcode") if isinstance(result, dict) else "invalid response"
        raise DeliveryError(f"企业微信拒绝消息，errcode={code}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="发送已生成的策略日报到企业微信群")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="UTF-8 策略日报文件")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取策略日报")
    parser.add_argument("--dry-run", action="store_true", help="校验并显示内容，不发送")
    args = parser.parse_args(argv)
    try:
        content = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        content = validate_content(content)
        if args.dry_run:
            print(content)
            return 0
        webhook = os.environ.get(WEBHOOK_ENV, "")
        if not webhook:
            raise DeliveryError(f"请先设置环境变量 {WEBHOOK_ENV}")
        send_text(webhook, content)
    except (DeliveryError, OSError, UnicodeError) as error:
        print(f"发送失败：{error}", file=sys.stderr)
        return 1
    print("策略日报已发送到企业微信群")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
