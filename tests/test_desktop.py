from datetime import datetime, timedelta

import pytest

from smu_auto_evaluation.scheduler import (
    IN_PROGRESS_TIMEOUT,
    RETRY_DELAY,
    MAX_AUTOMATIC_ATTEMPTS,
    SCHEDULE_RECHECK_INTERVAL,
    ScheduleState,
    next_run,
    schedule_wait_seconds,
)
from smu_auto_evaluation.settings import Settings


def test_next_run_today():
    assert next_run(datetime(2026, 1, 2, 8, 0), "09:30") == datetime(2026, 1, 2, 9, 30)


def test_next_run_tomorrow_after_schedule():
    assert next_run(datetime(2026, 1, 2, 10, 0), "09:30") == datetime(2026, 1, 3, 9, 30)


def test_settings_round_trip(tmp_path):
    path = tmp_path / "config.ini"
    Settings("student", "secret", "01:05", False).save(path)
    assert Settings.load(path) == Settings("student", "secret", "01:05", False)


@pytest.mark.parametrize(
    "password",
    ["abc%123", "abc%%123", " secret ", "密码含有中文"],
)
def test_settings_preserves_password_exactly(tmp_path, password):
    path = tmp_path / "config.ini"

    Settings("student", password, "01:05", False).save(path)

    assert Settings.load(path).password == password
    assert password not in path.read_text(encoding="utf-8")


def test_settings_loads_legacy_escaped_percent_password_and_migrates_it(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        "[login]\naccount = student\npassword = abc%%123\n"
        "[schedule]\ntime = 01:05\n"
        "[general]\nrun_at_startup = false\n",
        encoding="utf-8",
    )

    settings = Settings.load(path)

    assert settings == Settings("student", "abc%123", "01:05", False)
    settings.save(path)
    assert Settings.load(path) == settings


def test_invalid_encoded_password_does_not_echo_its_contents(tmp_path):
    path = tmp_path / "config.ini"
    secret = "this must not appear in an error"
    path.write_text(
        "[login]\npassword = " + secret + "\npassword_encoding = base64-utf8\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        Settings.load(path)

    assert secret not in str(exc_info.value)


def test_invalid_time():
    with pytest.raises(ValueError):
        Settings("student", "secret", "25:00").validate()


def test_starting_after_daily_time_claims_todays_catch_up(tmp_path):
    state = ScheduleState(tmp_path / "schedule-state.json")
    now = datetime(2026, 8, 30, 9, 0)

    slot = state.claim_due_run(now, "00:10")

    assert slot is not None
    assert slot.date == "2026-08-30"
    assert slot.run_time == "00:10"


def test_starting_before_daily_time_does_not_claim_run(tmp_path):
    state = ScheduleState(tmp_path / "schedule-state.json")
    now = datetime(2026, 8, 30, 0, 9)

    assert state.claim_due_run(now, "00:10") is None
    assert state.next_wakeup(now, "00:10") == datetime(2026, 8, 30, 0, 10)


def test_successful_catch_up_is_not_repeated_after_restart(tmp_path):
    path = tmp_path / "schedule-state.json"
    now = datetime(2026, 8, 30, 9, 0)
    state = ScheduleState(path)
    slot = state.claim_due_run(now, "00:10")
    assert slot is not None
    state.finish(slot, True, now)

    assert ScheduleState(path).claim_due_run(now, "00:10") is None


def test_failed_run_uses_delayed_bounded_retries(tmp_path):
    state = ScheduleState(tmp_path / "schedule-state.json")
    now = datetime(2026, 8, 30, 9, 0)

    for attempt in range(MAX_AUTOMATIC_ATTEMPTS):
        slot = state.claim_due_run(now, "00:10")
        assert slot is not None
        state.finish(slot, False, now)
        assert state.claim_due_run(now, "00:10") is None
        if attempt < MAX_AUTOMATIC_ATTEMPTS - 1:
            now += RETRY_DELAY

    assert state.claim_due_run(now + RETRY_DELAY, "00:10") is None


def test_stale_in_progress_slot_can_be_recovered_after_timeout(tmp_path):
    state = ScheduleState(tmp_path / "schedule-state.json")
    now = datetime(2026, 8, 30, 9, 0)
    assert state.claim_due_run(now, "00:10") is not None

    assert state.claim_due_run(now + IN_PROGRESS_TIMEOUT - timedelta(seconds=1), "00:10") is None
    assert state.claim_due_run(now + IN_PROGRESS_TIMEOUT, "00:10") is not None


def test_only_current_day_is_caught_up_after_long_downtime(tmp_path):
    state = ScheduleState(tmp_path / "schedule-state.json")

    slot = state.claim_due_run(datetime(2026, 9, 3, 9, 0), "00:10")

    assert slot is not None
    assert slot.date == "2026-09-03"


def test_scheduler_rechecks_soon_after_sleep_crosses_daily_time(tmp_path):
    state = ScheduleState(tmp_path / "schedule-state.json")
    before_sleep = datetime(2026, 8, 30, 23, 50)
    target = state.next_wakeup(before_sleep, "00:10")

    assert target == datetime(2026, 8, 31, 0, 10)
    assert schedule_wait_seconds(before_sleep, target) == SCHEDULE_RECHECK_INTERVAL.total_seconds()

    # Simulate the next polling wake-up occurring after a long suspend.  The
    # loop must re-read the current time and claim the missed slot immediately.
    after_resume = datetime(2026, 8, 31, 8, 0)
    slot = state.claim_due_run(after_resume, "00:10")

    assert slot is not None
    state.finish(slot, True, after_resume)
    assert state.claim_due_run(after_resume, "00:10") is None


def test_scheduler_wait_does_not_run_early_when_waking_before_target():
    now = datetime(2026, 8, 30, 23, 50)
    target = datetime(2026, 8, 31, 0, 10)

    assert schedule_wait_seconds(now, target) == SCHEDULE_RECHECK_INTERVAL.total_seconds()
    assert schedule_wait_seconds(target - timedelta(seconds=30), target) == 30
