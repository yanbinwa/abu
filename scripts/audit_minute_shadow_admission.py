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
    OperationalStore, SnapshotCatalog,
    evaluate_minute_data_admission, load_minute_admission_config,
)


def trading_sessions(calendar_path, asof_session):
    payload = json.loads(Path(calendar_path).read_text(encoding="utf-8"))
    dates = payload.get("dates")
    if not isinstance(dates, list) or not dates:
        raise ValueError("trading calendar must contain non-empty dates")
    return sorted({int(session) for session in dates
                   if int(session) <= int(asof_session)})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit M5 minute data-only admission")
    parser.add_argument("--service-config", type=Path,
                        default=ROOT / "configs/service/service_v1.json")
    parser.add_argument("--admission-config", type=Path,
                        default=ROOT / "configs/service/minute_shadow_admission_v1.json")
    parser.add_argument("--minute-config", type=Path,
                        default=ROOT / "configs/service/minute_shadow_v1.json")
    parser.add_argument("--asof-session", type=int, required=True)
    args = parser.parse_args(argv)
    service = json.loads(args.service_config.read_text(encoding="utf-8"))
    admission = load_minute_admission_config(args.admission_config)
    minute = json.loads(args.minute_config.read_text(encoding="utf-8"))
    sessions = [session for session in trading_sessions(
        minute["trading_calendar_path"], args.asof_session)
        if int(admission["first_eligible_session"]) <= session]
    with OperationalStore(service["database_path"]) as store:
        catalog = SnapshotCatalog(store, service["snapshot_root"])
        result = evaluate_minute_data_admission(
            store, catalog, sessions, admission)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
