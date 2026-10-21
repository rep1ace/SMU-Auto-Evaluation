from __future__ import annotations

import json
import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .evaluation_state import atomic_write


# A failed request can be transient, but continuously retrying it while the
# tray process is running (or each time it is restarted) is surprising and can
# cause duplicate submissions.  A schedule slot therefore has a small, bounded
# retry budget.  A manual run is intentionally not part of this state: it is a
# separate user action and does not mark a scheduled slot as complete.
RETRY_DELAY = timedelta(minutes=15)
IN_PROGRESS_TIMEOUT = timedelta(hours=2)
MAX_AUTOMATIC_ATTEMPTS = 3
DAILY_DELAY_SECONDS = (0, 30 * 60)
# Windows does not count time spent in low-power states towards some thread
# waits.  Keep the scheduler's waits short so it rechecks the wall clock soon
# after a resume instead of waiting for the pre-sleep remainder to expire.
SCHEDULE_RECHECK_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class ScheduledRun:
    """A daily, course retry, or evening schedule slot being executed."""

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


def schedule_wait_seconds(now: datetime, wake_at: datetime) -> float:
    """Return a bounded scheduler wait, based on the current wall clock."""
    remaining = max(0.0, (wake_at - now).total_seconds())
    return min(remaining, SCHEDULE_RECHECK_INTERVAL.total_seconds())


class ScheduleState:
    """Persisted status for automatic daily runs.

    Only targets falling today are eligible for a catch-up. This includes a
    previous evening's daily slot delayed past midnight, but prevents a computer
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
        except FileNotFoundError:
            return {}
        except (ValueError, UnicodeError) as exc:
            raise ValueError("调度状态文件损坏，未重置已完成任务") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("slots"), dict):
            raise ValueError("调度状态文件格式无效")
        return data["slots"]

    def _write(self) -> None:
        atomic_write(self.path, {"version": 1, "slots": self._slots})

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

    def _daily_target(self, now: datetime, run_time: str) -> tuple[ScheduledRun, datetime]:
        slot, base = self._slot(now, run_time)
        with self._lock:
            self._prune(now)
            record = self._slots.get(slot.key)
            if isinstance(record, dict) and "target_at" in record:
                target = datetime.fromisoformat(str(record["target_at"]))
                if not base <= target <= base + timedelta(seconds=DAILY_DELAY_SECONDS[1]):
                    raise ValueError("每日任务的随机运行时间超出有效范围")
                return slot, target
            if isinstance(record, dict):
                # Upgrades must preserve the retry/completion state of a slot
                # which already started under the old, unshifted schedule.
                target = base
            else:
                target = base + timedelta(seconds=random.randint(*DAILY_DELAY_SECONDS))
                record = {"date": slot.date, "run_time": run_time,
                          "status": "pending", "attempts": 0}
            record["target_at"] = self._timestamp(target)
            self._slots[slot.key] = record
            self._write()
            logging.info("每日评课计划: 设定=%s 实际=%s", slot.key, self._timestamp(target))
            return slot, target

    def _daily_candidates(self, now: datetime, run_time: str) -> list[tuple[ScheduledRun, datetime]]:
        candidates = [self._daily_target(now, run_time)]
        previous, _ = self._slot(now - timedelta(days=1), run_time)
        with self._lock:
            record = self._slots.get(previous.key)
            if isinstance(record, dict) and isinstance(record.get("target_at"), str):
                target = datetime.fromisoformat(record["target_at"])
                if target.date() == now.date():
                    candidates.insert(0, (previous, target))
        return candidates

    def claim_due_run(self, now: datetime, run_time: str) -> ScheduledRun | None:
        """Claim a daily run at its persisted, randomly delayed target."""
        for slot, target in self._daily_candidates(now, run_time):
            claimed = self._claim_target(now, slot, target)
            if claimed is not None:
                return claimed
        return None

    def claim_target(self, now: datetime, name: str, target: datetime) -> ScheduledRun | None:
        """Claim a persisted course retry or evening job using the same retry budget."""
        slot = ScheduledRun(now.date().isoformat(), name)
        if target.date() != now.date():
            return None
        return self._claim_target(now, slot, target)

    def _claim_target(self, now: datetime, slot: ScheduledRun, target: datetime) -> ScheduledRun | None:
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
                **(record if isinstance(record, dict) else {}),
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

    def recover_interrupted_runs(self, now: datetime) -> None:
        """Recover at process startup, while holding this account's run lock.

        The OS lock proves no old worker is still submitting. An interrupted
        attempt can therefore be checked immediately, without a two-hour wait.
        """
        with self._lock:
            changed = False
            for record in self._slots.values():
                if (isinstance(record, dict)
                        and (record.get("date") == now.date().isoformat()
                             or str(record.get("target_at", "")).startswith(now.date().isoformat() + "T"))
                        and record.get("status") == "in_progress"):
                    record["status"] = "failed"
                    attempts = record.get("attempts", 1)
                    record["attempts"] = max(0, attempts - 1) if isinstance(attempts, int) else 0
                    record["updated_at"] = self._timestamp(now - RETRY_DELAY)
                    changed = True
            if changed:
                self._write()

    def target_wakeup(self, now: datetime, name: str, target: datetime) -> datetime | None:
        slot = ScheduledRun(now.date().isoformat(), name)
        return self._target_wakeup(now, slot, target)

    def _target_wakeup(self, now: datetime, slot: ScheduledRun, target: datetime) -> datetime | None:
        with self._lock:
            record = self._slots.get(slot.key)
            if isinstance(record, dict):
                status = record.get("status")
                attempts = record.get("attempts", 0)
                attempts = attempts if isinstance(attempts, int) else 0
                updated_at = self._record_time(record)
                if status == "succeeded" or attempts >= MAX_AUTOMATIC_ATTEMPTS:
                    return None
                if status == "failed" and updated_at:
                    return max(now, updated_at + RETRY_DELAY)
                if status == "in_progress" and updated_at:
                    return max(now, updated_at + IN_PROGRESS_TIMEOUT)
        return max(now, target)

    def next_wakeup(self, now: datetime, run_time: str) -> datetime:
        """Return the next scheduled time or an eligible retry time."""
        wakeups = [wake for slot, target in self._daily_candidates(now, run_time)
                   if (wake := self._target_wakeup(now, slot, target)) is not None]
        if wakeups:
            return min(wakeups)
        # Persist tomorrow's offset too, so repeated polling does not move it.
        _, target = self._daily_target(now + timedelta(days=1), run_time)
        return target
