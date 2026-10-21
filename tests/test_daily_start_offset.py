import json
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from smu_auto_evaluation.scheduler import RETRY_DELAY, ScheduleState


@pytest.fixture
def random_delay(monkeypatch):
    sample = Mock(return_value=900)
    monkeypatch.setattr("smu_auto_evaluation.scheduler.random.randint", sample)
    return sample


@pytest.mark.parametrize("seconds", [0, 1, 900, 1800])
def test_daily_run_waits_for_offset_including_both_boundaries(tmp_path, random_delay, seconds):
    random_delay.return_value = seconds
    state = ScheduleState(tmp_path / "schedule.json")
    base = datetime(2026, 10, 21, 9)
    target = base + timedelta(seconds=seconds)

    assert state.next_wakeup(base - timedelta(hours=1), "09:00") == target
    assert state.claim_due_run(target - timedelta(seconds=1), "09:00") is None
    slot = state.claim_due_run(target, "09:00")
    assert slot is not None
    state.finish(slot, True, target)
    assert state.claim_due_run(target, "09:00") is None
    random_delay.assert_called_once_with(0, 1800)


def test_polling_and_restart_reuse_the_saved_target(tmp_path, random_delay):
    path = tmp_path / "schedule.json"
    now = datetime(2026, 10, 21, 9)
    target = now + timedelta(minutes=15)

    for _ in range(3):
        state = ScheduleState(path)
        assert state.claim_due_run(now, "09:00") is None
        assert state.next_wakeup(now, "09:00") == target
    record = json.loads(path.read_text())["slots"]["2026-10-21 09:00"]
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    slot = ScheduleState(path).claim_due_run(target, "09:00")
    assert slot is not None
    ScheduleState(path).finish(slot, True, target)
    assert ScheduleState(path).claim_due_run(target, "09:00") is None
    random_delay.assert_called_once()


def test_next_day_gets_a_new_persisted_offset(tmp_path, random_delay):
    random_delay.side_effect = [0, 1800]
    state = ScheduleState(tmp_path / "schedule.json")
    now = datetime(2026, 10, 21, 10)
    slot = state.claim_due_run(now, "09:00")
    state.finish(slot, True, now)

    assert state.next_wakeup(now, "09:00") == datetime(2026, 10, 22, 9, 30)
    assert state.next_wakeup(now, "09:00") == datetime(2026, 10, 22, 9, 30)
    assert random_delay.call_count == 2


def test_setting_changes_get_separate_targets_and_reuse_them_when_changed_back(tmp_path, random_delay):
    random_delay.side_effect = [60, 1800]
    state = ScheduleState(tmp_path / "schedule.json")
    now = datetime(2026, 10, 21, 8)

    assert state.next_wakeup(now, "09:00") == datetime(2026, 10, 21, 9, 1)
    assert state.next_wakeup(now, "10:00") == datetime(2026, 10, 21, 10, 30)
    assert state.next_wakeup(now, "09:00") == datetime(2026, 10, 21, 9, 1)
    assert random_delay.call_count == 2


def test_starting_after_the_shifted_deadline_catches_up_immediately(tmp_path, random_delay):
    random_delay.return_value = 1800
    state = ScheduleState(tmp_path / "schedule.json")

    assert state.claim_due_run(datetime(2026, 10, 21, 10), "09:00") is not None


def test_failure_retries_keep_the_offset_and_existing_retry_delay(tmp_path, random_delay):
    path = tmp_path / "schedule.json"
    state = ScheduleState(path)
    target = datetime(2026, 10, 21, 9, 15)
    slot = state.claim_due_run(target, "09:00")
    assert slot is not None
    state.finish(slot, False, target)

    state = ScheduleState(path)
    assert state.claim_due_run(target + timedelta(minutes=1), "09:00") is None
    assert state.next_wakeup(target, "09:00") == target + RETRY_DELAY
    assert state.claim_due_run(target + RETRY_DELAY, "09:00") == slot
    random_delay.assert_called_once_with(0, 1800)


def test_daily_offset_survives_midnight_and_is_not_repeated(tmp_path, random_delay):
    random_delay.return_value = 1800
    path = tmp_path / "schedule.json"
    state = ScheduleState(path)
    assert state.next_wakeup(datetime(2026, 10, 21, 23, 40), "23:50") == datetime(2026, 10, 22, 0, 20)

    state = ScheduleState(path)
    before = datetime(2026, 10, 22, 0, 5)
    target = datetime(2026, 10, 22, 0, 20)
    assert state.claim_due_run(before, "23:50") is None
    assert state.next_wakeup(before, "23:50") == target
    slot = state.claim_due_run(target, "23:50")
    assert slot is not None
    assert slot.date == "2026-10-21"
    state.finish(slot, True, target)
    state = ScheduleState(path)
    assert state.claim_due_run(target, "23:50") is None
    assert state.next_wakeup(target, "23:50") == datetime(2026, 10, 23, 0, 20)
    assert random_delay.call_count == 2


def test_interrupted_midnight_run_is_recovered_without_a_new_offset(tmp_path, random_delay):
    random_delay.return_value = 1800
    path = tmp_path / "schedule.json"
    state = ScheduleState(path)
    state.next_wakeup(datetime(2026, 10, 21, 23, 40), "23:50")
    slot = state.claim_due_run(datetime(2026, 10, 22, 0, 20), "23:50")

    state = ScheduleState(path)
    now = datetime(2026, 10, 22, 0, 21)
    state.recover_interrupted_runs(now)
    assert state.claim_due_run(now, "23:50") == slot
    assert random_delay.call_count == 2


def test_overdue_days_are_not_replayed(tmp_path, random_delay):
    path = tmp_path / "schedule.json"
    ScheduleState(path).next_wakeup(datetime(2026, 10, 18, 8), "09:00")

    slot = ScheduleState(path).claim_due_run(datetime(2026, 10, 21, 10), "09:00")
    assert slot is not None
    assert slot.date == "2026-10-21"


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_upgrade_preserves_existing_daily_completion_and_retry(tmp_path, random_delay, status):
    path = tmp_path / "schedule.json"
    now = datetime(2026, 10, 21, 9)
    path.write_text(json.dumps({"version": 1, "slots": {"2026-10-21 09:00": {
        "date": "2026-10-21", "run_time": "09:00", "status": status,
        "attempts": 1, "updated_at": now.isoformat(),
    }}}))
    state = ScheduleState(path)
    assert state.claim_due_run(now, "09:00") is None
    later = state.claim_due_run(now + RETRY_DELAY, "09:00")
    assert (later is not None) == (status == "failed")
    random_delay.assert_not_called()


def test_evening_and_course_targets_do_not_get_the_daily_offset(tmp_path, random_delay):
    state = ScheduleState(tmp_path / "schedule.json")
    now = datetime(2026, 10, 21, 19)

    assert state.claim_target(now, "evening", now) is not None
    assert state.claim_target(now, "course:retry", now) is not None
    random_delay.assert_not_called()
