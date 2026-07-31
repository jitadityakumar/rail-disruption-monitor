import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler(timezone="Europe/London")

_DOW_LABELS = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def setup_scheduler() -> None:
    from scanner import scan_all_routes
    dow = os.environ.get("SCAN_DOW", "sun")
    hour = int(os.environ.get("SCAN_HOUR", "6"))
    minute = int(os.environ.get("SCAN_MINUTE", "0"))
    _scheduler.add_job(
        scan_all_routes,
        trigger=CronTrigger(day_of_week=dow, hour=hour, minute=minute),
        id="weekly_scan",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("Scheduler started: weekly scan %s %02d:%02d", dow, hour, minute)


def shutdown_scheduler() -> None:
    _scheduler.shutdown(wait=False)


def get_schedule_label() -> str | None:
    dow = os.environ.get("SCAN_DOW", "sun")
    hour = int(os.environ.get("SCAN_HOUR", "6"))
    minute = int(os.environ.get("SCAN_MINUTE", "0"))
    dow_label = _DOW_LABELS.get(dow.lower(), dow)
    return f"{dow_label} at {hour:02d}:{minute:02d}"


def get_next_run() -> str | None:
    job = _scheduler.get_job("weekly_scan")
    if job and job.next_run_time:
        dt = job.next_run_time
        dow_label = _DOW_LABELS.get(dt.strftime("%a").lower(), dt.strftime("%a"))
        return f"{dow_label} {_ordinal(dt.day)} {dt.strftime('%B %Y %H:%M')}"
    return None
