from __future__ import absolute_import

import hashlib
import json
import os
import platform
import socket
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .ABuJobStore import JobStore
from .ABuOperationalStore import OperationalStore, _atomic_json
from .ABuScheduler import ProjectScheduler
from .ABuServiceLock import ServiceLock


def _canonical_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _git_head(root):
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "UNKNOWN"


class ServiceRuntime(object):

    def __init__(self, config_path, jobs_path, repository_root=None):
        self.config_path = Path(config_path)
        self.jobs_path = Path(jobs_path)
        self.repository_root = Path(repository_root or Path(__file__).resolve().parents[2])
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self._assert_safe_config()
        self.instance_id = "service-{}".format(uuid.uuid4().hex)
        self.lock = ServiceLock(self.config["lock_path"])
        self.store = None
        self.job_store = None
        self.scheduler = ProjectScheduler(self.config["timezone"])
        self.active_schedule = {"registered": [], "deferred": []}
        self.heartbeat_path = Path(self.config["runtime_root"]) / "run" / "heartbeat.json"

    def _assert_safe_config(self):
        if self.config.get("timezone") != "Asia/Shanghai":
            raise ValueError("v1 service timezone must be Asia/Shanghai")
        if not self.config.get("research_only"):
            raise ValueError("v1 service must remain research_only")
        if self.config.get("broker_connected"):
            raise ValueError("v1 service cannot connect to a broker")
        if self.config.get("account_writes_enabled"):
            raise ValueError("M1 cannot enable account writes")
        if self.config.get("minute_execution_admission") != "ACCEPT_DATA_ONLY":
            raise ValueError("M1 minute execution must remain data-only")

    def start(self, handlers=None, start_scheduler=False):
        self.lock.acquire()
        try:
            self.store = OperationalStore(self.config["database_path"])
            self.job_store = JobStore(self.store)
            started_at = _now()
            self.store.record_service_start(
                self.instance_id, socket.gethostname(), os.getpid(), started_at,
                _git_head(self.repository_root), _canonical_hash(self.config))
            recovered = self.job_store.recover_running_attempts(started_at)
            for job in self.jobs["jobs"]:
                self.job_store.register_definition(job)
            supplied = handlers or {}
            wrapped = {
                job["job_id"]: self._wrap_job(job, supplied[job["job_id"]])
                for job in self.jobs["jobs"] if job["job_id"] in supplied
            }
            schedule = self.scheduler.register(self.jobs["jobs"], wrapped)
            self.active_schedule = schedule
            self.write_heartbeat("RUNNING", recovered_attempts=recovered,
                                 schedule=schedule)
            if start_scheduler:
                self.scheduler.start()
            return {"service_instance_id": self.instance_id, "schedule": schedule,
                    "recovered_attempts": recovered}
        except Exception:
            if self.store is not None:
                self.store.close()
                self.store = None
            self.lock.release()
            raise

    def _wrap_job(self, definition, handler):
        def execute():
            scheduled_at = _now()
            day = scheduled_at[:10].replace("-", "")
            if "interval_seconds" in definition.get("schedule", {}):
                idempotency_key = "{}:{}".format(
                    definition["job_id"], scheduled_at[:16])
            else:
                idempotency_key = "{}:{}".format(definition["job_id"], day)
            run, unused_created = self.job_store.ensure_run(
                definition["job_id"], definition["job_version"], idempotency_key,
                scheduled_at, scheduled_at)
            if run["status"] == "SUCCEEDED":
                return {"status": "ALREADY_SUCCEEDED", "job_run_id": run["job_run_id"]}
            attempt = self.job_store.start_attempt(
                run["job_run_id"], self.instance_id, scheduled_at)
            try:
                result = handler() or {}
                outputs = result.get("output_snapshot_ids", [])
                if result.get("status") == "RETRYABLE_NOT_READY":
                    self.job_store.finish_attempt(
                        attempt["attempt_id"], "RETRYABLE_FAILED", _now(),
                        error_code="INPUT_NOT_READY",
                        error_detail=json.dumps(result, sort_keys=True))
                    return result
                self.job_store.finish_attempt(
                    attempt["attempt_id"], "SUCCEEDED", _now(),
                    output_snapshot_ids=outputs)
                return result
            except Exception as error:
                self.job_store.finish_attempt(
                    attempt["attempt_id"], "RETRYABLE_FAILED", _now(),
                    error_code=type(error).__name__, error_detail=str(error))
                raise
        return execute

    def write_heartbeat(self, status, **extra):
        payload = {
            "schema_version": "service_heartbeat_v1",
            "service_instance_id": self.instance_id,
            "status": status,
            "at": _now(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "python": platform.python_version(),
            "research_only": True,
            "broker_connected": False,
            "account_writes_enabled": False,
            "schedule": self.active_schedule,
        }
        payload.update(extra)
        _atomic_json(self.heartbeat_path, payload)
        return payload

    def run_forever(self, handlers=None):
        self.start(handlers=handlers, start_scheduler=True)
        try:
            while True:
                self.write_heartbeat("RUNNING")
                time.sleep(30)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop("requested")

    def stop(self, reason="normal"):
        if self.store is None:
            self.lock.release()
            return
        self.scheduler.shutdown(wait=False)
        stopped_at = _now()
        self.store.record_service_stop(self.instance_id, stopped_at, reason)
        self.write_heartbeat("STOPPED", reason=reason)
        self.store.close()
        self.store = None
        self.job_store = None
        self.lock.release()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop("error" if exc_type else "normal")
