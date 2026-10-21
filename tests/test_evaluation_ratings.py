import json
from types import SimpleNamespace
from unittest.mock import Mock

import main
import pytest


RATING_QUESTION = """
<div class="question" data-txdm="1" data-zbdm="rating">
  <h3>评分题</h3>
  <div class="raty" data-wtxm='[{"zbxmdm":"15"},{"zbxmdm":"20"},{"zbxmdm":"25"}]'></div>
</div>
"""
RADIO_QUESTION = """
<div class="question" data-txdm="2" data-zbdm="radio">
  <h3>判断题</h3><input type="radio" value="first"><input type="radio" value="last">
</div>
"""


def submit_questionnaire(monkeypatch, questions, error=None):
    html = """
    <script>entss.post({xnxqdm:'term',pjlxdm:'6',teaxm:'teacher',
    wjdm:'form',kcrwdm:'task',kcptdm:'platform',kcdm:'course',jxhjdm:'section'});</script>
    """ + questions
    request = Mock(side_effect=[
        SimpleNamespace(content=html.encode("utf-8")),
        SimpleNamespace(text="saved", status_code=200),
    ])
    monkeypatch.setattr(main, "request_with_retry", request)
    if error is None:
        main.evaluate_course(object(), "teacher", "period", "form")
    else:
        with pytest.raises(RuntimeError, match=error):
            main.evaluate_course(object(), "teacher", "period", "form")
    return request


@pytest.mark.parametrize("skipped_count", [0, 40])
@pytest.mark.parametrize("plan", [
    [0, 0, 1, 1],  # 90
    [0, 1, 1, 2],  # 80
    [1, 1, 1, 1],  # 80
    [0, 1, 1, 1],  # 85
])
def test_rating_allocation_finishes_with_target_score(monkeypatch, skipped_count, plan):
    # Force the old allocator to reuse one budget. Bound its retries so a
    # regression fails quickly instead of hanging the test process.
    attempts = 0

    def repeated_choice(low, high):
        nonlocal attempts
        attempts += 1
        assert attempts <= 100, "星级分配陷入重试循环"
        return 0

    monkeypatch.setattr(main.random, "randint", repeated_choice)

    def choose_plan(plans):
        assert plan in plans
        return plan.copy()

    monkeypatch.setattr(main.random, "choice", choose_plan)
    monkeypatch.setattr(main.random, "shuffle", lambda values: values.reverse())
    skipped = '<div class="question" data-txdm="3"><textarea></textarea></div>'
    request = submit_questionnaire(
        monkeypatch, skipped * skipped_count + RATING_QUESTION * 4 + RADIO_QUESTION
    )
    assert request.call_args.args[1] == "post"
    payload = request.call_args.kwargs["data"]
    answers = json.loads(payload["dt"])

    assert len(answers) == 5
    expected = [("25", 25, "★★★★★"), ("20", 20, "★★★★"), ("15", 15, "★★★")]
    assert [(answer["zbxmdm"], answer["fz"], answer["dtjg"]) for answer in answers[:4]] == [
        expected[tier] for tier in reversed(plan)
    ]
    assert 80 <= payload["wtpf"] <= 90
    assert payload["wtpf"] == sum(answer["fz"] for answer in answers)
    assert payload["pfsm"] == ""
    assert (answers[-1]["zbxmdm"], answers[-1]["fz"], answers[-1]["dtjg"]) == ("last", 0, "否")


@pytest.mark.parametrize("plan", [
    [1, 2, 2, 2, 2],  # 80
    [1, 1, 2, 2, 2],  # 85
    [0, 1, 2, 2, 2],  # 90
])
def test_question_limit_is_preserved(monkeypatch, plan):
    def choose_plan(plans):
        assert plan in plans
        return plan.copy()

    monkeypatch.setattr(main.random, "choice", choose_plan)
    request = submit_questionnaire(monkeypatch, RATING_QUESTION * 20)
    payload = request.call_args.kwargs["data"]
    answers = json.loads(payload["dt"])

    assert len(answers) == 5
    assert 80 <= payload["wtpf"] <= 90
    assert payload["wtpf"] == sum(answer["fz"] for answer in answers)


@pytest.mark.parametrize("rating_count", range(4))
def test_impossible_score_is_not_submitted(monkeypatch, caplog, rating_count):
    request = submit_questionnaire(
        monkeypatch, RATING_QUESTION * rating_count, error="无法生成 80–90 分的评分方案"
    )

    assert request.call_count == 1  # Only fetch the questionnaire; do not submit.
    assert "无法生成 80–90 分的评分方案" in caplog.text
