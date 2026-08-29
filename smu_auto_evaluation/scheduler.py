from __future__ import annotations

from datetime import datetime, timedelta


def next_run(now: datetime, run_time: str) -> datetime:
    hour, minute = (int(part) for part in run_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
