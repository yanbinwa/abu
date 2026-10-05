#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import OperationalStore  # noqa: E402
from abupy.ServiceBu.ABuNotificationPipeline import NotificationWorker  # noqa: E402
from abupy.ServiceBu.ABuWeComTransport import WeComWebhookTransport  # noqa: E402


def _local_env(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main():
    parser = argparse.ArgumentParser(description="Drain transactional paper notifications")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--env", type=Path, default=ROOT / ".env.wecom")
    parser.add_argument("--recover-unknown", action="store_true")
    args = parser.parse_args()
    _local_env(args.env)
    webhook = os.environ.get("WECOM_WEBHOOK_URL", "")
    worker_store = OperationalStore(args.database, target_schema_version=4)
    try:
        worker = NotificationWorker(
            worker_store, args.artifacts, WeComWebhookTransport(webhook))
        now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        if args.recover_unknown:
            worker.recover_unknown(now)
        outcomes = worker.drain_once(now)
        print("processed {} notification parts".format(len(outcomes)))
    finally:
        worker_store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
