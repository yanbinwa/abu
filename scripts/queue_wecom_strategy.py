#!/usr/bin/env python3
"""Atomically queue a strategy report for the long-running WeCom bot."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def local_config():
    """Read simple KEY=VALUE settings without executing the file as shell code."""
    values = {}
    config_path = ROOT / ".env.wecom"
    if not config_path.exists():
        return values
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def default_outbox():
    configured = os.environ.get("WECOM_RUNTIME_DIR") or local_config().get("WECOM_RUNTIME_DIR")
    runtime = Path(configured) if configured else ROOT / "runtime" / "wecom"
    return runtime / "outbox"


def main(argv=None):
    parser = argparse.ArgumentParser(description="将策略日报加入企业微信智能机器人发送队列")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="UTF-8 策略日报文件")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取")
    parser.add_argument("--target", help="可选的企业微信 userid 或群 chatid；默认使用已绑定用户")
    parser.add_argument("--outbox", type=Path, default=default_outbox())
    args = parser.parse_args(argv)

    content = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    content = content.strip()
    if not content:
        parser.error("策略内容为空")
    identifier = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    job = {"id": identifier, "content": content, "createdAt": time.time()}
    if args.target:
        job["target"] = args.target
    args.outbox.mkdir(parents=True, exist_ok=True)
    temporary = args.outbox / f".{identifier}.tmp"
    destination = args.outbox / f"{identifier}.json"
    temporary.write_text(json.dumps(job, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    print(f"已加入企业微信发送队列：{identifier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
