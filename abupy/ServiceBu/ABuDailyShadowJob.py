from __future__ import absolute_import

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .ABuDailyDataCenter import (
    DailySnapshotBuilder, FieldDependencyPolicy,
    components_from_source_config, write_coverage_report,
)
from .ABuMarketSnapshotCatalog import SnapshotCatalog


def _last_csv_date(path):
    path = Path(path)
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        handle.seek(max(0, end - 16384))
        lines = handle.read().decode("utf-8").splitlines()
    with path.open("r", encoding="utf-8") as source:
        header = next(csv.reader(source))
    last = next(csv.reader([next(line for line in reversed(lines) if line.strip())]))
    return int(float(dict(zip(header, last))["date"]))


def latest_benchmark_session(pattern):
    matches = sorted(Path().glob(pattern)) if not Path(pattern).is_absolute() else sorted(
        Path(pattern).parent.glob(Path(pattern).name))
    if not matches:
        raise FileNotFoundError("benchmark source is missing: {}".format(pattern))
    return _last_csv_date(matches[-1])


class DailyShadowSnapshotJob(object):
    """Publish data-only daily snapshots from legacy-updated source files."""

    def __init__(self, operational_store, content_root, policy_path, sources_path,
                 benchmark_pattern, clock=None):
        self.store = operational_store
        self.catalog = SnapshotCatalog(operational_store, content_root)
        self.policy = FieldDependencyPolicy.from_path(policy_path)
        self.sources = json.loads(Path(sources_path).read_text(encoding="utf-8"))
        self.benchmark_pattern = benchmark_pattern
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))

    def __call__(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("daily shadow clock must be timezone-aware")
        today = int(now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"))
        session = latest_benchmark_session(self.benchmark_pattern)
        if session != today:
            return {
                "status": "RETRYABLE_NOT_READY",
                "latest_benchmark_session": session,
                "today": today,
                "output_snapshot_ids": [],
            }
        existing = self.catalog.list_committed("DAILY", session)
        if existing:
            return {
                "status": "ALREADY_COMMITTED",
                "snapshot_id": existing[-1]["snapshot_id"],
                "output_snapshot_ids": [existing[-1]["snapshot_id"]],
            }
        timestamp = now.isoformat()
        components, details = components_from_source_config(self.sources, timestamp)
        snapshot, event, unused_created = DailySnapshotBuilder(
            self.catalog, self.policy).build(
                session, timestamp, timestamp, components)
        coverage = write_coverage_report(
            self.catalog.content, session, components, self.policy)
        return {
            "status": "COMMITTED",
            "snapshot_id": snapshot["snapshot_id"],
            "event_id": event["event_id"],
            "coverage_sha256": coverage["sha256"],
            "component_status": {name: value["status"]
                                 for name, value in sorted(details.items())},
            "output_snapshot_ids": [snapshot["snapshot_id"]],
        }
