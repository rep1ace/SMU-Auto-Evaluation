"""Behavioral regression tests for authentication and durable evaluation."""
from datetime import datetime
from types import SimpleNamespace

import main
import pytest

from smu_auto_evaluation.evaluation_state import EvaluationState


@pytest.fixture
def state(tmp_path):
    return EvaluationState(tmp_path / 'progress.json')


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 10, 21, 10)]
    monkeypatch.setattr(main, 'local_now', lambda: value[0])
    return value


def course(period='period', end='2026-10-2109:00:00', **kwargs):
    return dict(pjdm='', teadm='teacher', dgksdm=period, ktpj='form',
                jsrq='2026-10-21', jksj=end, **kwargs)


def query_fake(monkeypatch, courses, submitted):
    def query(session, date):
        return [dict(item, pjdm='done' if item['dgksdm'] in submitted else item['pjdm'])
                for item in courses if item['jsrq'] == date.isoformat()]
    monkeypatch.setattr(main, 'get_pending_courses_by_date', query)
    monkeypatch.setattr(main, 'evaluate_course',
                        lambda session, teacher, period, form, **kwargs: submitted.append(period))


def test_wrapper_keeps_login_flow_and_closes_sessions(monkeypatch, state):
    sessions = []
    calls = []
    class Session:
        def __init__(self):
            sessions.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append('closed')
    tickets = iter(['failed', 'ticket'])
    monkeypatch.setattr(main.requests, 'Session', Session)
    monkeypatch.setattr(main.EvaluationState, 'for_account', lambda account: state)
    monkeypatch.setattr(main, 'get_captcha', lambda session: '1234')
    monkeypatch.setattr(main, 'login', lambda *args: next(tickets))
    monkeypatch.setattr(main, 'redirect_login', lambda session, ticket: calls.append(ticket))
    monkeypatch.setattr(main, 'get_courses', lambda session, progress: 7)
    assert main.run_evaluation('account', 'password') == 7
    assert len(sessions) == 2
    assert calls == ['closed', 'ticket', 'closed']


def test_wrapper_validates_credentials():
    with pytest.raises(ValueError, match='账号或密码为空'):
        main.run_evaluation('', 'password')


def test_only_finished_courses_are_submitted(monkeypatch, state, clock):
    submitted = []
    query_fake(monkeypatch, [course('finished'), course('future', '2026-10-2111:00:00')], submitted)
    assert main.get_courses(object(), state) == 1
    assert submitted == ['finished']
    snapshot = state.snapshot(clock[0])
    assert not snapshot['complete']
    assert len(snapshot['pending']) == 1
    retry = datetime.fromisoformat(next(iter(snapshot['pending'].values()))['retry_at'])
    assert datetime(2026, 10, 21, 11, 1) <= retry <= datetime(2026, 10, 21, 11, 15)


def test_completion_survives_restart(monkeypatch, state, clock):
    submitted = []
    query_fake(monkeypatch, [course('one'), course('two', '2026-10-2111:00:00')], submitted)
    assert main.get_courses(object(), state) == 1
    restarted = EvaluationState(state.path)
    original = restarted.snapshot(clock[0])
    assert original['pending'] == state.snapshot(clock[0])['pending']
    clock[0] = datetime(2026, 10, 21, 12)
    assert main.get_courses(object(), restarted) == 1
    assert submitted == ['one', 'two']
    assert restarted.snapshot(clock[0])['complete']
    assert [name for name, _ in restarted.extra_jobs(clock[0])] == ['evening']


def test_unknown_end_time_is_not_evaluated(monkeypatch, state, clock):
    submitted = []
    query_fake(monkeypatch, [course(end='bad')], submitted)
    assert main.get_courses(object(), state) == 0
    assert not submitted
    assert len(state.snapshot(clock[0])['pending']) == 1


def test_exact_end_time_can_be_evaluated(monkeypatch, state, clock):
    submitted = []
    query_fake(monkeypatch, [course(end='2026-10-2110:00:00')], submitted)
    assert main.get_courses(object(), state) == 1


def test_previous_day_is_included_and_duplicates_are_ignored(monkeypatch, state, clock):
    submitted = []
    item = course(end='2026-10-2009:00:00')
    item['jsrq'] = '2026-10-20'
    query_fake(monkeypatch, [item, item.copy()], submitted)
    assert main.get_courses(object(), state) == 1
    assert len(submitted) == 1


def test_rejected_submission_never_marks_success(monkeypatch, state, clock):
    item = course()
    monkeypatch.setattr(main, 'get_pending_courses_by_date', lambda *args: [item])
    monkeypatch.setattr(main, 'evaluate_course', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match='仍待评'):
        main.get_courses(object(), state)
    assert state.snapshot(clock[0])['completed'] == []
    assert not state.snapshot(clock[0])['complete']


def test_query_failure_never_marks_day_complete(monkeypatch, state, clock):
    def bad(*args):
        raise RuntimeError('network')
    monkeypatch.setattr(main, 'get_pending_courses_by_date', bad)
    with pytest.raises(RuntimeError):
        main.get_courses(object(), state)
    assert not state.snapshot(clock[0])['complete']


def test_malformed_query_is_not_an_empty_day(monkeypatch):
    monkeypatch.setattr(main, 'request_with_retry', lambda *args, **kwargs: SimpleNamespace(json=lambda: {'login': False}))
    with pytest.raises(RuntimeError, match='响应无效'):
        main.get_pending_courses_by_date(object(), datetime(2026, 10, 21).date())


def test_query_reads_all_pages(monkeypatch):
    pages = []
    def request(*args, **kwargs):
        page = kwargs['data']['page']
        pages.append(page)
        return SimpleNamespace(json=lambda: {'total': 61, 'rows': [course()] * (60 if page == 1 else 1)})
    monkeypatch.setattr(main, 'request_with_retry', request)
    assert len(main.get_pending_courses_by_date(object(), datetime(2026, 10, 21).date())) == 61
    assert pages == [1, 2]


def test_one_failed_course_does_not_block_others(monkeypatch, state, clock):
    submitted = []
    query_fake(monkeypatch, [course('bad'), course('good')], submitted)
    def evaluate(session, teacher, period, form, **kwargs):
        if period == 'bad':
            raise RuntimeError('failure')
        submitted.append(period)
    monkeypatch.setattr(main, 'evaluate_course', evaluate)
    with pytest.raises(RuntimeError, match='1 门课程'):
        main.get_courses(object(), state)
    assert submitted == ['good']
    day = state.snapshot(clock[0])
    assert len(day['completed']) == 1
    assert len(day['pending']) == 1


def test_evening_still_skips_courses_ending_later(monkeypatch, state, clock):
    clock[0] = datetime(2026, 10, 21, 19, 15)
    submitted = []
    query_fake(monkeypatch, [course('evening', '2026-10-2120:00:00')], submitted)
    assert main.get_courses(object(), state) == 0
    assert not submitted
    jobs = dict(state.extra_jobs(clock[0]))
    assert any(name.startswith('course:') and due > datetime(2026, 10, 21, 20)
               for name, due in jobs.items())


def form_html():
    import json
    params = ','.join(f"{key}:'{key}'" for key in
                      ('teaxm', 'xnxqdm', 'pjlxdm', 'wjdm', 'kcrwdm', 'kcptdm', 'kcdm', 'jxhjdm'))
    options = json.dumps([{'zbxmdm': str(i)} for i in range(5)])
    rating = f'''<div class="question" data-txdm="1" data-zbdm="id">
      <h3>Rating</h3><div class="raty" data-wtxm='{options}'></div></div>'''
    return f'<script>entss.post({{{params}}})</script>' + rating * 4


def test_submit_waits_before_post_and_uses_browser_headers(monkeypatch):
    events = []
    posted = []
    def request(session, method, url, **kwargs):
        if method == 'get':
            return SimpleNamespace(content=form_html().encode())
        events.append('post')
        posted.append(kwargs)
        return SimpleNamespace(text='ok', status_code=200)
    state = SimpleNamespace(before_submission=lambda: events.append('wait'))
    monkeypatch.setattr(main, 'request_with_retry', request)
    main.evaluate_course(object(), 'teacher', 'period', 'form', state=state)
    assert events == ['wait', 'post']
    assert posted[0]['retry_count'] == 1
    headers = posted[0]['headers']
    assert headers['User-Agent'] == main.headers['User-Agent']
    assert headers['sec-ch-ua'] == main.headers['sec-ch-ua']
    assert 'showXsktpjwj.page' in headers['Referer']
    assert headers['Origin'] == 'https://zhjw.smu.edu.cn'
    assert 'Content-Length' not in headers
    assert 'Host' not in headers
    import json
    assert len(json.loads(posted[0]['data']['dt'])) == 4


def test_submission_request_is_never_blindly_retried():
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        raise main.requests.Timeout('timeout')
    with pytest.raises(RuntimeError):
        main.request_with_retry(SimpleNamespace(request=request), 'post', 'https://example.test/save', retry_count=1)
    assert len(calls) == 1


def test_another_run_does_not_submit_before_persisted_retry(monkeypatch, state, clock):
    submitted = []
    query_fake(monkeypatch, [course('later', '2026-10-2111:00:00')], submitted)
    main.get_courses(object(), state)
    retry = datetime.fromisoformat(next(iter(state.snapshot(clock[0])['pending'].values()))['retry_at'])
    clock[0] = datetime(2026, 10, 21, 11, 0, 1)
    assert main.get_courses(object(), EvaluationState(state.path)) == 0
    assert not submitted
    clock[0] = retry
    assert main.get_courses(object(), EvaluationState(state.path)) == 1


def test_missing_course_after_submission_is_not_confirmation(monkeypatch, state, clock):
    item = course()
    replies = iter([[item], [], []])
    monkeypatch.setattr(main, 'get_pending_courses_by_date', lambda *args: next(replies))
    monkeypatch.setattr(main, 'evaluate_course', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match='无法确认'):
        main.get_courses(object(), state)
    day = state.snapshot(clock[0])
    assert not day['complete']
    assert not day['completed']
    assert len(day['pending']) == 1


def test_temporarily_missing_course_keeps_saved_pending(monkeypatch, state, clock):
    state.defer('2026-10-21:' + main.course_identity(course()), datetime(2026, 10, 21, 12), clock[0])
    original = state.snapshot(clock[0])['pending']
    monkeypatch.setattr(main, 'get_pending_courses_by_date', lambda *args: [])
    with pytest.raises(RuntimeError, match='缺少已保存'):
        main.get_courses(object(), state)
    day = state.snapshot(clock[0])
    assert day['pending'] == original
    assert not day['complete']


def test_error_response_with_empty_rows_is_not_an_empty_day(monkeypatch):
    monkeypatch.setattr(main, 'request_with_retry', lambda *args, **kwargs: SimpleNamespace(json=lambda: {'success': False, 'rows': []}))
    with pytest.raises(RuntimeError):
        main.get_pending_courses_by_date(object(), datetime(2026, 10, 21).date())


def test_incomplete_pagination_is_rejected(monkeypatch):
    replies = iter([{'total': 2, 'rows': [course()]}, {'total': 2, 'rows': []}])
    monkeypatch.setattr(main, 'request_with_retry', lambda *args, **kwargs: SimpleNamespace(json=lambda: next(replies)))
    with pytest.raises(RuntimeError, match='分页'):
        main.get_pending_courses_by_date(object(), datetime(2026, 10, 21).date())


def test_login_does_not_log_raw_response_or_captcha(monkeypatch, caplog):
    response = SimpleNamespace(status_code=200, text='password-and-ticket-secret', json=lambda: {'success': False})
    monkeypatch.setattr(main, 'request_with_retry', lambda *args, **kwargs: response)
    assert main.login('account', 'password', 'captcha-secret', object()) == 'failed'
    assert response.text not in caplog.text
    assert 'captcha-secret' not in caplog.text


def test_cli_decodes_same_password_format_as_desktop(monkeypatch, tmp_path):
    from smu_auto_evaluation.settings import Settings
    source = tmp_path / 'main.py'
    source.touch()
    Settings('account', 'contains%percent', '00:10').save(tmp_path / 'config.ini')
    monkeypatch.setattr(main, '__file__', str(source))
    monkeypatch.setattr(main, 'configure_cli_logging', lambda: None)
    calls = []
    monkeypatch.setattr(main, 'run_evaluation', lambda *args: calls.append(args))
    main.main()
    assert calls == [('account', 'contains%percent')]


@pytest.mark.parametrize('change', ['already_evaluated', 'postponed'])
def test_recheck_after_wait_prevents_stale_submission(monkeypatch, state, clock, change):
    item = course()
    latest = dict(item, pjdm='done') if change == 'already_evaluated' else dict(item, jksj='2026-10-2112:00:00')
    replies = iter([[item], [], [latest]])
    monkeypatch.setattr(main, 'get_pending_courses_by_date', lambda *args: next(replies))
    waits = []
    monkeypatch.setattr(state, 'before_submission', lambda: waits.append('waited'))
    posts = []
    def request(session, method, url, **kwargs):
        if method == 'get':
            return SimpleNamespace(content=form_html().encode())
        posts.append(kwargs)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(main, 'request_with_retry', request)
    assert main.get_courses(object(), state) == 0
    assert waits == ['waited']
    assert not posts
    day = state.snapshot(clock[0])
    if change == 'already_evaluated':
        assert day['complete']
        assert len(day['completed']) == 1
    else:
        assert not day['complete']
        assert len(day['pending']) == 1


def test_sso_ticket_is_not_written_to_logs_or_error_text(caplog):
    def request(*args, **kwargs):
        raise main.requests.ConnectionError('https://example.test/?ticket=secret-ticket')
    with pytest.raises(RuntimeError) as error:
        main.request_with_retry(SimpleNamespace(request=request), 'get',
                                'https://example.test/?ticket=secret-ticket', retry_count=1)
    assert 'secret-ticket' not in str(error.value)
    assert 'secret-ticket' not in caplog.text


def test_evening_discovers_courses_absent_in_morning(monkeypatch, state, clock):
    submitted = []
    clock[0] = datetime(2026, 10, 21, 0, 10)
    query_fake(monkeypatch, [], submitted)
    assert main.get_courses(object(), state) == 0
    clock[0] = datetime(2026, 10, 21, 19, 31)
    jobs = dict(EvaluationState(state.path).extra_jobs(clock[0]))
    assert jobs['evening'] <= clock[0]
    query_fake(monkeypatch, [course('new-in-evening', '2026-10-2119:00:00')], submitted)
    assert main.get_courses(object(), state) == 1
    assert submitted == ['new-in-evening']
