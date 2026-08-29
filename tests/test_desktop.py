from datetime import datetime

import pytest

from smu_auto_evaluation.scheduler import next_run
from smu_auto_evaluation.settings import Settings


def test_next_run_today():
    assert next_run(datetime(2026, 1, 2, 8, 0), "09:30") == datetime(2026, 1, 2, 9, 30)


def test_next_run_tomorrow_after_schedule():
    assert next_run(datetime(2026, 1, 2, 10, 0), "09:30") == datetime(2026, 1, 3, 9, 30)


def test_settings_round_trip(tmp_path):
    path = tmp_path / "config.ini"
    Settings("student", "secret", "01:05", False).save(path)
    assert Settings.load(path) == Settings("student", "secret", "01:05", False)


def test_invalid_time():
    with pytest.raises(ValueError):
        Settings("student", "secret", "25:00").validate()
