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
    DailyShadowSnapshotJob, MinuteShadowSnapshotJob, ServiceRuntime,
)


def runtime(args):
    return ServiceRuntime(
        args.config, args.jobs, repository_root=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the research-only ABu paper service v1 kernel")
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/service/service_v1.json")
    parser.add_argument("--jobs", type=Path, default=ROOT / "configs/service/jobs_v1.json")
    parser.add_argument("--enable-daily-shadow", action="store_true")
    parser.add_argument("--enable-minute-shadow", action="store_true")
    args = parser.parse_args(argv)
    service = runtime(args)
    if args.command == "check":
        result = service.start()
        result["integrity"] = service.store.integrity_check()
        service.stop("check_complete")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    handlers = {}
    if args.enable_daily_shadow:
        handlers["daily.snapshot_shadow"] = lambda: DailyShadowSnapshotJob(
            service.store, service.config["snapshot_root"],
            service.config["daily_data_policy_path"],
            service.config["daily_source_config_path"],
            service.config["daily_benchmark_pattern"])()
    if args.enable_minute_shadow:
        minute_job = []

        def run_minute_shadow():
            if not minute_job:
                minute_job.append(MinuteShadowSnapshotJob(
                    service.store, service.config["snapshot_root"],
                    service.config["minute_shadow_config_path"]))
            return minute_job[0]()

        handlers["minute.collect"] = run_minute_shadow
    service.run_forever(handlers=handlers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
