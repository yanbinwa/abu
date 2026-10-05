#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import (  # noqa: E402
    OperationalStore, SnapshotCatalog, benchmark_sessions,
    evaluate_minute_data_admission, load_minute_admission_config,
)
from abupy.ServiceBu.ABuPaperShadowAdmission import PaperShadowAdmissionGate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Create paper-shadow activation only after real minute admission")
    parser.add_argument("--service-config", type=Path, required=True)
    parser.add_argument("--admission-config", type=Path, required=True)
    parser.add_argument("--asof-session", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    service = json.loads(args.service_config.read_text(encoding="utf-8"))
    admission = load_minute_admission_config(args.admission_config)
    sessions = [item for item in benchmark_sessions(
        service["daily_benchmark_pattern"]) if item <= args.asof_session]
    with OperationalStore(service["database_path"]) as store:
        result = evaluate_minute_data_admission(
            store, SnapshotCatalog(store, service["snapshot_root"]),
            sessions, admission)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    certificate = PaperShadowAdmissionGate.create(
        args.output, result, database_path=service["database_path"],
        snapshot_root=service["snapshot_root"], source_commit=commit,
        created_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
    print(json.dumps(certificate, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
