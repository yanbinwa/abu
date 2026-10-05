#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import (  # noqa: E402
    OperationalStore, benchmark_sessions, evaluate_daily_shadow_admission,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-config", type=Path,
                        default=ROOT / "configs/service/service_v1.json")
    parser.add_argument("--admission-config", type=Path,
                        default=ROOT / "configs/service/daily_shadow_admission_v1.json")
    parser.add_argument("--asof-session", type=int)
    args = parser.parse_args()
    service = json.loads(args.service_config.read_text(encoding="utf-8"))
    admission = json.loads(args.admission_config.read_text(encoding="utf-8"))
    asof = args.asof_session or int(datetime.now(
        ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"))
    with OperationalStore(service["database_path"], initialize=False) as store:
        result = evaluate_daily_shadow_admission(
            store, benchmark_sessions(admission["benchmark_pattern"]),
            first_eligible_session=admission["first_eligible_session"],
            asof_session=asof,
            required_days=admission["required_consecutive_sessions"],
            deadline=admission["daily_ready_deadline"])
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
