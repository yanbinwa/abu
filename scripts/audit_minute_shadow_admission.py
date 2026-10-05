#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import (  # noqa: E402
    OperationalStore, SnapshotCatalog, benchmark_sessions,
    evaluate_minute_data_admission, load_minute_admission_config,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit M5 minute data-only admission")
    parser.add_argument("--service-config", type=Path,
                        default=ROOT / "configs/service/service_v1.json")
    parser.add_argument("--admission-config", type=Path,
                        default=ROOT / "configs/service/minute_shadow_admission_v1.json")
    parser.add_argument("--asof-session", type=int, required=True)
    args = parser.parse_args(argv)
    service = json.loads(args.service_config.read_text(encoding="utf-8"))
    admission = load_minute_admission_config(args.admission_config)
    sessions = [
        session for session in benchmark_sessions(service["daily_benchmark_pattern"])
        if int(admission["first_eligible_session"]) <= session <= args.asof_session
    ]
    with OperationalStore(service["database_path"]) as store:
        catalog = SnapshotCatalog(store, service["snapshot_root"])
        result = evaluate_minute_data_admission(
            store, catalog, sessions, admission)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
