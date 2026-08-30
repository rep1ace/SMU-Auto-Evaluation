from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# A failed request can be transient, but continuously retrying it while the
# tray process is running (or each time it is restarted) is surprising and can
# cause duplicate submissions.  A schedule slot therefore has a small, bounded
# retry budget.  A manual run is intentionally not part of this state: it is a
# separate user action and does not mark a scheduled slot as complete.
RETRY_DELAY = timedelta(minutes=15)
IN_PROGRESS_TIMEOUT = timedelta(hours=2)
MAX_AUTOMATIC_ATTEMPTS = 3


@dataclass(frozen=True)
class ScheduledRun:
    """The one daily schedule slot currently being executed."""

    date: str
    run_time: str

    @property
    def key(self) -> str:
        return f"{self.date} {self.run_time}"


def _parse_run_time(run_time: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in run_time.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("运行时间超出有效范围")
    return hour, minute


def next_run(now: datetime, run_time: str) -> datetime:
    hour, minute = _parse_run_time(run_time)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class ScheduleState:
    """Persisted status for automatic daily runs.

    Only today's slot is eligible for a catch-up.  This prevents a computer
    that was off for several days from replaying stale evaluations.  Completed
    slots are retained briefly so a restart cannot repeat today's work.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._slots = self._read()

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            slots = data.get("slots", {})
            return slots if isinstance(slots, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps({"version": 1, "slots": self._slots}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _slot(now: datetime, run_time: str) -> tuple[ScheduledRun, datetime]:
        hour, minute = _parse_run_time(run_time)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return ScheduledRun(now.date().isoformat(), run_time), target

    @staticmethod
    def _timestamp(now: datetime) -> str:
        return now.isoformat(timespec="seconds")

    @staticmethod
    def _record_time(record: dict[str, object]) -> datetime | None:
        value = record.get("updated_at")
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _prune(self, now: datetime) -> None:
        oldest = (now.date() - timedelta(days=7)).isoformat()
        for key, record in list(self._slots.items()):
            if not isinstance(record, dict) or str(record.get("date", "")) < oldest:
                del self._slots[key]

    def claim_due_run(self, now: datetime, run_time: str) -> ScheduledRun | None:
        """Atomically claim today's due automatic run, if it is eligible."""
        slot, target = self._slot(now, run_time)
        if now < target:
            return None
        with self._lock:
            self._prune(now)
            record = self._slots.get(slot.key)
            if isinstance(record, dict):
                status = record.get("status")
                attempts = record.get("attempts", 0)
                attempts = attempts if isinstance(attempts, int) else 0
                updated_at = self._record_time(record)
                if status == "succeeded" or attempts >= MAX_AUTOMATIC_ATTEMPTS:
                    return None
                if status == "failed" and updated_at and now < updated_at + RETRY_DELAY:
                    return None
                if status == "in_progress" and updated_at and now < updated_at + IN_PROGRESS_TIMEOUT:
                    return None
            else:
                attempts = 0

            self._slots[slot.key] = {
                "date": slot.date,
                "run_time": slot.run_time,
                "status": "in_progress",
                "attempts": attempts + 1,
                "updated_at": self._timestamp(now),
            }
            self._write()
            return slot

    def finish(self, slot: ScheduledRun, succeeded: bool, now: datetime) -> None:
        """Record the actual outcome without ever treating a failure as success."""
        with self._lock:
            record = self._slots.get(slot.key)
            if not isinstance(record, dict):
                return
            record["status"] = "succeeded" if succeeded else "failed"
            record["updated_at"] = self._timestamp(now)
            self._write()

    def next_wakeup(self, now: datetime, run_time: str) -> datetime:
        """Return the next scheduled time or an eligible retry time."""
        slot, target = self._slot(now, run_time)
        with self._lock:
            record = self._slots.get(slot.key)
            if now >= target and isinstance(record, dict):
                status = record.get("status")
                attempts = record.get("attempts", 0)
                attempts = attempts if isinstance(attempts, int) else 0
                updated_at = self._record_time(record)
                if status == "failed" and updated_at and attempts < MAX_AUTOMATIC_ATTEMPTS:
                    return max(now, updated_at + RETRY_DELAY)
                if status == "in_progress" and updated_at:
                    return max(now, updated_at + IN_PROGRESS_TIMEOUT)
        return next_run(now, run_time)
