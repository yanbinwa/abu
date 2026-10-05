from __future__ import absolute_import

from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


class ProjectScheduler(object):
    """Thin APScheduler adapter; project JobStore remains the source of truth."""

    def __init__(self, timezone="Asia/Shanghai"):
        self.timezone = ZoneInfo(timezone)
        self.scheduler = BackgroundScheduler(timezone=self.timezone)

    def register(self, job_definitions, handlers):
        registered = []
        deferred = []
        for job in job_definitions:
            handler = handlers.get(job["job_id"])
            schedule = job.get("schedule")
            if handler is None or not schedule:
                deferred.append(job["job_id"])
                continue
            if "interval_seconds" in schedule:
                trigger = IntervalTrigger(
                    seconds=int(schedule["interval_seconds"]), timezone=self.timezone)
            elif "hour" in schedule and "minute" in schedule:
                trigger = CronTrigger(
                    hour=schedule["hour"], minute=schedule["minute"],
                    timezone=self.timezone)
            else:
                deferred.append(job["job_id"])
                continue
            self.scheduler.add_job(
                handler, trigger=trigger, id=job["job_id"], replace_existing=False,
                max_instances=1, coalesce=True, misfire_grace_time=300)
            registered.append(job["job_id"])
        return {"registered": registered, "deferred": deferred}

    def start(self):
        self.scheduler.start()

    def shutdown(self, wait=True):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)
