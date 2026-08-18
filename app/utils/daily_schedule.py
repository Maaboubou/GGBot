"""Helpers for timezone-aware daily schedules."""

from datetime import datetime, timedelta, timezone, tzinfo
import time


BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def next_daily_run_at(
    *,
    hour: int,
    now: float | None = None,
    schedule_timezone: tzinfo = BEIJING_TIMEZONE,
) -> float:
    """Return the first daily occurrence strictly after ``now``."""
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be between 0 and 23, got {hour}")

    now_timestamp = time.time() if now is None else float(now)
    local_now = datetime.fromtimestamp(now_timestamp, schedule_timezone)
    next_run = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if next_run <= local_now:
        next_run += timedelta(days=1)
    return next_run.timestamp()
