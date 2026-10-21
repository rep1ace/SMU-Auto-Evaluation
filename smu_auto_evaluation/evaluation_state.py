"""Durable, account-scoped course progress and submission pacing."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import tempfile
import time

from .settings import app_data_dir

BEIJING = timezone(timedelta(hours=8))
COURSE_RETRY_SECONDS = (60, 15 * 60)


def local_now() -> datetime:
    # Naive Beijing time also works on Windows without an external tzdata package.
    return datetime.now(BEIJING).replace(tzinfo=None)


@contextmanager
def file_lock(path: Path, *, blocking: bool = False):
    """OS releases this lock on exit, including a crash or power interruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EvaluationState:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def for_account(cls, account: str) -> 'EvaluationState':
        identity = sha256(account.strip().encode('utf-8')).hexdigest()[:24]
        return cls(app_data_dir() / f'evaluation-{identity}.json')

    def run_lock(self):
        return file_lock(self.path.with_suffix('.run.lock'))

    @contextmanager
    def _edit(self):
        with file_lock(self.path.with_suffix('.state.lock'), blocking=True):
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                data = {'version': 1, 'days': {}}
            except (ValueError, UnicodeError) as exc:
                raise ValueError('评课状态文件损坏，请检查备份后恢复；未重置完成记录') from exc
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('days'), dict):
                raise ValueError('评课状态文件格式无效')
            yield data
            atomic_write(self.path, data)

    @staticmethod
    def _day(data: dict, now: datetime) -> dict:
        date = now.date().isoformat()
        if date not in data['days']:
            yesterday = (now.date() - timedelta(days=1)).isoformat()
            previous = data['days'].get(yesterday, {})
            carried = {key: value for key, value in previous.get('pending', {}).items()
                       if key.startswith(yesterday + ':')}
            evening = now.replace(hour=19, minute=0, second=0, microsecond=0)
            evening += timedelta(seconds=random.uniform(0, 1800))
            data['days'][date] = {
                'complete': False,
                'completed': [key for key in previous.get('completed', [])
                              if key.startswith(yesterday + ':')],
                'pending': carried,
                'evening_at': evening.isoformat(),
            }
        # Preserve the old random offset when upgrading an already saved day.
        # The schedule slot name stays unchanged, so completed checks stay done.
        day = data['days'][date]
        evening = datetime.fromisoformat(day['evening_at'])
        old_start = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if old_start <= evening <= old_start + timedelta(minutes=30):
            day['evening_at'] = (evening - timedelta(hours=1)).isoformat()
        # Retain a short history, never replay old scheduled jobs.
        oldest = (now.date() - timedelta(days=7)).isoformat()
        data['days'] = {key: value for key, value in data['days'].items() if key >= oldest}
        return data['days'][date]

    def snapshot(self, now: datetime | None = None) -> dict:
        with self._edit() as data:
            day = self._day(data, now or local_now())
            return json.loads(json.dumps(day))

    def begin(self, now: datetime) -> None:
        with self._edit() as data:
            self._day(data, now)['complete'] = False

    def defer(self, key: str, end: datetime | None, now: datetime) -> None:
        with self._edit() as data:
            day = self._day(data, now)
            previous = day['pending'].get(key)
            end_at = end.isoformat() if end else None
            if not previous or previous['end_at'] != end_at:
                retry = max(now, end) + timedelta(seconds=random.uniform(*COURSE_RETRY_SECONDS)) if end else None
                day['pending'][key] = {
                    'end_at': end_at, 'retry_at': retry.isoformat() if retry else None,
                }
            day['complete'] = False

    def completed(self, key: str, now: datetime) -> None:
        with self._edit() as data:
            day = self._day(data, now)
            if key not in day['completed']:
                day['completed'].append(key)
            day['pending'].pop(key, None)

    def finish(self, pending: set[str], now: datetime) -> None:
        with self._edit() as data:
            day = self._day(data, now)
            day['pending'] = {key: value for key, value in day['pending'].items() if key in pending}
            day['complete'] = not pending
            day['checked_at'] = now.isoformat()

    def extra_jobs(self, now: datetime) -> list[tuple[str, datetime]]:
        day = self.snapshot(now)
        # A successful morning query can be empty because today's courses have
        # not appeared yet. Keep one independent evening verification even if
        # every course seen so far is complete; only pending courses are sent.
        jobs = [('evening', datetime.fromisoformat(day['evening_at']))]
        retries = sorted({item['retry_at'] for item in day['pending'].values() if item['retry_at']})
        # A failed early job must not hide later courses behind its exhausted
        # retry budget. The scheduler considers every distinct due time.
        for due in retries:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            jobs.append((f'course:{due}', max(midnight, datetime.fromisoformat(due))))
        return jobs

    def before_submission(self, *, now=local_now, sleep=time.sleep) -> None:
        # Reserve the NEXT slot before sending. A crash cannot erase the delay.
        with self._edit() as data:
            value = data.get('next_submission_at')
        if value:
            deadline = datetime.fromisoformat(value)
            while (remaining := (deadline - now()).total_seconds()) > 0:
                sleep(min(remaining, 60))
        with self._edit() as data:
            data['next_submission_at'] = (
                now() + timedelta(seconds=random.uniform(30, 60))
            ).isoformat()
