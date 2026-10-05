#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import (  # noqa: E402
    DailyShadowSnapshotJob, MinuteShadowSnapshotJob, ServiceRuntime,
)


def runtime(args):
    return ServiceRuntime(
        args.config, args.jobs, repository_root=ROOT)


def _local_env(path):
    path = Path(path)
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def build_handlers(service, args):
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
    if args.enable_paper_shadow:
        if not service.config.get("account_writes_enabled"):
            raise ValueError(
                "paper shadow execution requires account_writes_enabled")
        if not args.paper_account_id:
            raise ValueError("--paper-account-id is required")
        minute_config = json.loads(Path(
            service.config["minute_shadow_config_path"]).read_text(
                encoding="utf-8"))
        execution_job = []

        def run_paper_shadow():
            if not execution_job:
                from abupy.ServiceBu.ABuTransactionalMinuteJob import (  # noqa: E402
                    TransactionalMinuteExecutionJob,
                )
                execution_job.append(TransactionalMinuteExecutionJob(
                    service.store, service.config["snapshot_root"],
                    minute_config["minute_store_root"],
                    minute_config["trading_calendar_path"],
                    args.paper_account_id, args.execution_policy_id))
            return execution_job[0]()

        handlers["minute.execute_paper_shadow"] = run_paper_shadow
    if args.enable_notifications:
        _local_env(args.wecom_env)
        from abupy.ServiceBu.ABuNotificationPipeline import NotificationWorker
        from abupy.ServiceBu.ABuWeComLongConnectionTransport import (
            wecom_transport_from_environment,
        )
        transport = wecom_transport_from_environment(os.environ)
        notification_worker = []

        def run_notifications():
            if not notification_worker:
                notification_worker.append(NotificationWorker(
                    service.store,
                    service.config["notification_artifact_root"], transport))
                notification_worker[0].recover_unknown(
                    datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
            now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            outcomes = notification_worker[0].drain_once(now)
            return {"status": "NOTIFICATIONS_DRAINED",
                    "processed_parts": len(outcomes),
                    "output_snapshot_ids": []}

        handlers["notification.deliver"] = run_notifications
    if args.enable_dashboard:
        from abupy.ServiceBu.ABuDashboard import (
            DashboardQuery, StaticDashboardRenderer,
        )

        def build_dashboard():
            with DashboardQuery(service.config["database_path"]) as query:
                result = StaticDashboardRenderer().build(
                    query, service.config["dashboard_root"])
            result.update({"status": "DASHBOARD_BUILT",
                           "output_snapshot_ids": []})
            return result

        handlers["dashboard.build"] = build_dashboard
    return handlers


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the research-only ABu paper service v1 kernel")
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/service/service_v1.json")
    parser.add_argument("--jobs", type=Path, default=ROOT / "configs/service/jobs_v1.json")
    parser.add_argument("--enable-daily-shadow", action="store_true")
    parser.add_argument("--enable-minute-shadow", action="store_true")
    parser.add_argument("--enable-paper-shadow", action="store_true")
    parser.add_argument("--paper-account-id")
    parser.add_argument("--execution-policy-id", choices=("M1", "M2"), default="M1")
    parser.add_argument("--enable-notifications", action="store_true")
    parser.add_argument("--wecom-env", type=Path, default=ROOT / ".env.wecom")
    parser.add_argument("--enable-dashboard", action="store_true")
    args = parser.parse_args(argv)
    service = runtime(args)
    if args.command == "check":
        result = service.start()
        result["integrity"] = service.store.integrity_check()
        service.stop("check_complete")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    handlers = build_handlers(service, args)
    service.run_forever(handlers=handlers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
