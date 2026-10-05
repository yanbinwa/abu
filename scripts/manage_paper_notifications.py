#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import OperationalStore
from abupy.ServiceBu.ABuNotificationPipeline import NotificationWorker


class _NoSendTransport(object):
    def send(self, *unused):
        raise RuntimeError("operator CLI never sends messages")


def main():
    parser = argparse.ArgumentParser(description="Retry or abandon one notification part")
    parser.add_argument("action", choices=("retry", "abandon"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--notification-id", required=True)
    parser.add_argument("--part-kind", choices=("TEXT", "CHART_IMAGE"), required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--at", required=True)
    args = parser.parse_args()
    store = OperationalStore(args.database, target_schema_version=4)
    try:
        NotificationWorker(
            store, args.artifacts, _NoSendTransport()).operator_action(
                args.notification_id, args.part_kind, args.action, args.at,
                args.reason, args.operator)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
