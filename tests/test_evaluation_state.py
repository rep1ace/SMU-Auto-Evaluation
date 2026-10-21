from datetime import datetime, timedelta

import pytest

from smu_auto_evaluation import evaluation_state as module
from smu_auto_evaluation.evaluation_state import EvaluationState
from smu_auto_evaluation.scheduler import ScheduleState, RETRY_DELAY, MAX_AUTOMATIC_ATTEMPTS


def test_evening_time_is_randomized_once_and_restored(tmp_path):
    now = datetime(2026, 10, 21, 8)
    state = EvaluationState(tmp_path / 'state.json')
    initial = state.snapshot(now)
    evening = datetime.fromisoformat(initial['evening_at'])
    assert now.replace(hour=19) <= evening <= now.replace(hour=19, minute=30)
    assert EvaluationState(state.path).snapshot(now)['evening_at'] == initial['evening_at']


def test_retry_time_survives_repeated_queries_and_restart(tmp_path):
    now = datetime(2026, 10, 21, 8)
    end = now.replace(hour=12)
    state = EvaluationState(tmp_path / 'state.json')
    state.defer('course', end, now)
    pending = state.snapshot(now)['pending']
    restored = EvaluationState(state.path)
    restored.defer('course', end, now + timedelta(minutes=5))
    assert restored.snapshot(now)['pending'] == pending


def test_extra_schedule_is_independent_of_failed_daily_slot(tmp_path):
    now = datetime(2026, 10, 21, 18)
    state = EvaluationState(tmp_path / 'state.json')
    schedule = ScheduleState(tmp_path / 'schedule.json')
    for _ in range(MAX_AUTOMATIC_ATTEMPTS):
        slot = schedule.claim_due_run(now, '00:10')
        schedule.finish(slot, False, now)
        now += RETRY_DELAY
    evening = dict(state.extra_jobs(now))['evening']
    assert schedule.claim_target(evening - timedelta(seconds=1), 'evening', evening) is None
    slot = schedule.claim_target(evening, 'evening', evening)
    assert slot is not None
    schedule.finish(slot, True, evening)
    restored = ScheduleState(schedule.path)
    assert restored.claim_target(evening, 'evening', evening) is None


def test_late_start_catches_up_evening(tmp_path):
    state = EvaluationState(tmp_path / 'state.json')
    now = datetime(2026, 10, 21, 22)
    name, due = state.extra_jobs(now)[0]
    assert ScheduleState(tmp_path / 'schedule.json').claim_target(now, name, due)


def test_submission_interval_is_persisted_across_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(module.random, 'uniform', lambda low, high: 45)
    clock = [datetime(2026, 10, 21, 10)]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += timedelta(seconds=seconds)
    state = EvaluationState(tmp_path / 'state.json')
    state.before_submission(now=lambda: clock[0], sleep=sleep)
    assert waits == []
    EvaluationState(state.path).before_submission(now=lambda: clock[0], sleep=sleep)
    assert waits == [45]
    assert clock[0] == datetime(2026, 10, 21, 10, 0, 45)


def test_corrupt_state_does_not_silently_erase_progress(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{broken')
    with pytest.raises(ValueError, match='损坏'):
        EvaluationState(path).snapshot()
    assert path.read_text() == '{broken'


def test_concurrent_run_is_rejected_and_lock_is_released(tmp_path):
    state = EvaluationState(tmp_path / 'state.json')
    with state.run_lock():
        with pytest.raises(OSError):
            with EvaluationState(state.path).run_lock():
                pytest.fail('concurrent evaluation')
    with state.run_lock():
        pass


def test_accounts_have_separate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'app_data_dir', lambda: tmp_path)
    first = EvaluationState.for_account('first')
    second = EvaluationState.for_account('second')
    assert first.path != second.path
    assert 'first' not in str(first.path)
    assert 'second' not in str(second.path)


def test_course_job_and_completion_recover_after_restart(tmp_path):
    now = datetime(2026, 10, 21, 10)
    path = tmp_path / 'state.json'
    state = EvaluationState(path)
    state.defer('2026-10-21:course', now.replace(hour=12), now)
    jobs = dict(EvaluationState(path).extra_jobs(now))
    name = next(key for key in jobs if key.startswith('course:'))
    due = jobs[name]
    schedule = ScheduleState(tmp_path / 'schedule.json')
    assert schedule.claim_target(due - timedelta(seconds=1), name, due) is None
    slot = ScheduleState(schedule.path).claim_target(due, name, due)
    assert slot is not None
    schedule = ScheduleState(schedule.path)
    schedule.finish(slot, True, due)
    assert ScheduleState(schedule.path).claim_target(due, name, due) is None


def test_cross_midnight_course_retry_is_retained(tmp_path):
    now = datetime(2026, 10, 21, 23, 59)
    state = EvaluationState(tmp_path / 'state.json')
    state.defer('2026-10-21:course', now, now)
    tomorrow = datetime(2026, 10, 22, 0, 20)
    jobs = dict(EvaluationState(state.path).extra_jobs(tomorrow))
    name = next(key for key in jobs if key.startswith('course:'))
    due = jobs[name]
    assert due.date() == tomorrow.date()
    assert ScheduleState(tmp_path / 'schedule.json').claim_target(tomorrow, name, due)


def test_beijing_clock_is_independent_of_host_timezone():
    from datetime import timezone
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((module.local_now() - utc).total_seconds() - 8 * 3600) < 2


def test_exhausted_early_retry_does_not_hide_later_course(tmp_path):
    now = datetime(2026, 10, 21, 8)
    state = EvaluationState(tmp_path / 'state.json')
    state.defer('2026-10-21:early', now.replace(hour=9), now)
    state.defer('2026-10-21:later', now.replace(hour=13), now)
    jobs = [(name, due) for name, due in state.extra_jobs(now) if name.startswith('course:')]
    assert len(jobs) == 2
    schedule = ScheduleState(tmp_path / 'schedule.json')
    name, due = jobs[0]
    for attempt in range(MAX_AUTOMATIC_ATTEMPTS):
        instant = due + attempt * RETRY_DELAY
        slot = schedule.claim_target(instant, name, due)
        assert slot is not None
        schedule.finish(slot, False, instant)
    later_name, later_due = jobs[1]
    assert schedule.claim_target(later_due, name, due) is None
    assert schedule.claim_target(later_due, later_name, later_due) is not None


def test_explicit_restart_recovery_can_retry_interrupted_evening(tmp_path):
    path = tmp_path / 'schedule.json'
    now = datetime(2026, 10, 21, 19, 5)
    schedule = ScheduleState(path)
    assert schedule.claim_target(now, 'evening', now)
    restored = ScheduleState(path)
    resumed = now + timedelta(minutes=1)
    restored.recover_interrupted_runs(resumed)
    assert restored.claim_target(resumed, 'evening', now)


def test_corrupt_schedule_does_not_silently_reset_completed_slots(tmp_path):
    path = tmp_path / 'schedule.json'
    path.write_text('{corrupt')
    with pytest.raises(ValueError, match='状态'):
        ScheduleState(path)
    assert path.read_text() == '{corrupt'


def test_empty_morning_query_does_not_cancel_evening_verification(tmp_path):
    now = datetime(2026, 10, 21, 0, 10)
    state = EvaluationState(tmp_path / 'state.json')
    state.finish(set(), now)
    assert state.snapshot(now)['complete']
    jobs = dict(EvaluationState(state.path).extra_jobs(now))
    assert 'evening' in jobs
    due = jobs['evening']
    schedule = ScheduleState(tmp_path / 'schedule.json')
    slot = schedule.claim_target(due, 'evening', due)
    assert slot
    schedule.finish(slot, True, due)
    assert ScheduleState(schedule.path).claim_target(due, 'evening', due) is None


@pytest.mark.parametrize('offset', [timedelta(), timedelta(minutes=17, seconds=23), timedelta(minutes=30)])
def test_saved_old_evening_time_moves_one_hour_without_resetting_progress(tmp_path, offset):
    import json
    now = datetime(2026, 10, 21, 18)
    path = tmp_path / 'state.json'
    old_due = datetime(2026, 10, 21, 20) + offset
    original = {
        'complete': False, 'completed': ['already-done'],
        'pending': {'later-course': {'end_at': '2026-10-21T21:00:00', 'retry_at': '2026-10-21T21:05:00'}},
        'evening_at': old_due.isoformat(),
    }
    path.write_text(json.dumps({'version': 1, 'days': {'2026-10-21': original}}))
    migrated = EvaluationState(path).snapshot(now)
    assert datetime.fromisoformat(migrated['evening_at']) == old_due - timedelta(hours=1)
    assert migrated['completed'] == original['completed']
    assert migrated['pending'] == original['pending']
    assert migrated['complete'] == original['complete']
    assert EvaluationState(path).snapshot(now) == migrated
    assert json.loads(path.read_text())['days']['2026-10-21'] == migrated
