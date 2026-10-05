#!/usr/bin/env python3
"""Audit or publish a versioned daily snapshot from existing project data."""
from __future__ import absolute_import

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import (  # noqa: E402
    DailySnapshotBuilder, FieldDependencyPolicy, OperationalStore,
    SnapshotCatalog, components_from_source_config,
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trading-session", type=int, required=True)
    parser.add_argument("--decision-cutoff", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--source-available-at", required=True,
                        help="Auditable time when imported source files became available")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--content-root", type=Path)
    parser.add_argument("--policy", type=Path,
                        default=ROOT / "configs/service/daily_data_v1.json")
    parser.add_argument("--sources", type=Path,
                        default=ROOT / "configs/service/daily_sources_v1.json")
    parser.add_argument("--publish", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    policy = FieldDependencyPolicy.from_path(args.policy)
    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    components, details = components_from_source_config(
        sources, args.source_available_at)
    audit = {
        "trading_session": args.trading_session,
        "policy_version": policy.version,
        "component_details": details,
    }
    try:
        audit["resolution"] = policy.resolve(
            {item.component_id for item in components})
    except ValueError as error:
        audit["error"] = str(error)
    if not args.publish:
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if "error" in audit else 0
    if args.database is None or args.content_root is None:
        raise SystemExit("--database and --content-root are required with --publish")
    with OperationalStore(args.database) as store:
        catalog = SnapshotCatalog(store, args.content_root)
        snapshot, event, created = DailySnapshotBuilder(catalog, policy).build(
            args.trading_session, args.decision_cutoff, args.created_at, components)
    print(json.dumps({
        "snapshot_id": snapshot["snapshot_id"],
        "event_id": event["event_id"],
        "created": created,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
