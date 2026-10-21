from datetime import datetime, timedelta
import json
import logging
import re
from io import BytesIO
from hashlib import md5
from PIL import Image
from bs4 import BeautifulSoup
import requests
import random
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from captcha_predictor import predict_captcha
from smu_auto_evaluation.evaluation_state import EvaluationState, local_now
from smu_auto_evaluation.settings import Settings

captcha_url = "https://uis.smu.edu.cn/imageServlet.do"
login_url = "https://uis.smu.edu.cn/login/login.do"
REQUEST_TIMEOUT = (10, 30)
REQUEST_RETRY_COUNT = 2
headers = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://zhjw.smu.edu.cn/",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}

def request_with_retry(session, method, url, **kwargs):
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    retry_count = kwargs.pop("retry_count", REQUEST_RETRY_COUNT)
    # URLs and request exceptions can contain a one-time SSO ticket. Keep
    # query strings and upstream exception messages out of persistent logs.
    parsed = urlsplit(url)
    safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    last_error = None
    for attempt in range(1, retry_count + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            logging.warning(
                "请求失败，第 %s/%s 次重试: %s %s 错误=%s",
                attempt,
                retry_count,
                method.upper(),
                safe_url,
                last_error,
            )
    raise RuntimeError(f"请求失败: {method.upper()} {safe_url} 错误={last_error}") from None


def get_captcha(session):
    headers_captcha = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Referer": "https://uis.smu.edu.cn/login.jsp?outLine=0",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    captcha_response = request_with_retry(
        session, "get", captcha_url, headers=headers_captcha
    )
    img = Image.open(BytesIO(captcha_response.content))
    result = predict_captcha(img)
    return result


# 登录
def login(account, password, captcha, session):
    password_md5 = md5(password.encode()).hexdigest()
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://uis.smu.edu.cn",
        "Referer": "https://uis.smu.edu.cn/login.jsp?redirect=https%3A%2F%2Fzhjw.smu.edu.cn%2Fnew%2FssoLogin",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    data = {
        "loginName": account,
        "password": password_md5,
        "randcodekey": captcha,
        "locationBrowser": "谷歌浏览器[Chrome]",
        "appid": "3550176",
        "redirect": "https://zhjw.smu.edu.cn/new/ssoLogin",
        "strength": 3,
    }
    response = request_with_retry(
        session, "post", login_url, data=data, headers=headers
    )
    try:
        result = response.json()
    except ValueError:
        result = None
    if isinstance(result, dict) and result.get("success") is not False:
        ticket = result.get("ticket")
        if response.status_code == 200 and isinstance(ticket, str) and ticket.strip():
            logging.info("登录成功")
            return ticket
    logging.error("登录失败，未收到有效登录凭证（HTTP %s）", response.status_code)
    return "failed"


def redirect_login(session, ticket):
    url = "https://zhjw.smu.edu.cn/new/ssoLogin"
    params = {
        "ticket": ticket,
    }
    resp = request_with_retry(session, "get", url, headers=headers, params=params)
    logging.info("教务系统跳转登录状态: %s", resp.status_code)


def evaluate_course(session, teadm, dgksdm, ktpj, *, state=None, before_submit=None):
    eval_url = f"https://zhjw.smu.edu.cn/new/student/ktpj/showXsktpjwj.page?pjlxdm=6&teadm={teadm}&dgksdm={dgksdm}&wjdm={ktpj}"
    eval_response = request_with_retry(
        session,
        "get",
        eval_url,
        headers=headers,
    )
    soup = BeautifulSoup(eval_response.content, "lxml")
    scripts = soup.find_all("script")
    data = None
    for script in scripts:
        if "entss.post" in script.text:
            matches = re.findall(r"(\w+):'([^']+)'", script.text)
            data = {match[0]: match[1] for match in matches}
            logging.info(
                "课程信息: %s %s %s %s %s",
                data["teaxm"],
                data["kcrwdm"],
                data["kcptdm"],
                data["kcdm"],
                data["pjlxdm"],
            )
            break
    if data is None:
        logging.error(
            "未找到评课页面提交参数，课程无法提交: teadm=%s dgksdm=%s ktpj=%s",
            teadm,
            dgksdm,
            ktpj,
        )
        raise RuntimeError("未找到评课页面提交参数")
    save_url = "https://zhjw.smu.edu.cn/new/student/ktpj/savePj"
    questions = soup.find_all("div", class_="question")
    data_list = []
    count = 0
    combinations = [
        [2, 2, 0],
        [1, 2, 1],
        [0, 4, 0],
        [1, 3, 0],
    ]  # [25分选项个数，20分选项个数，15分选项个数]
    combination = random.choice(combinations).copy()
    option = [-1, -2, -3]
    scores = [25, 20, 15]
    dtjgs = ["★★★★★", "★★★★", "★★★"]
    score_total = 0
    for question in questions:
        txdm = int(str(question.get("data-txdm", "0")))
        zbdm = str(question.get("data-zbdm", ""))
        title = question.find("h3")
        zbmc = (
            title.get_text(strip=True).replace("\n", "").replace("\t", "")
            if title
            else ""
        )
        # 对于选择题和非选择题的处理
        if question.find("div", class_="raty"):
            # 处理有评分选项的问题
            if not any(combination):
                raise RuntimeError("评分题目数量与问卷结构不符")
            val = random.choice([i for i, remaining in enumerate(combination) if remaining])
            combination[val] -= 1
            raty_div = question.find("div", class_="raty")
            if raty_div is None:
                continue
            options = json.loads(str(raty_div.get("data-wtxm", "[]")))
            zbxmdm = options[option[val]]["zbxmdm"]  # 比较满意
            fz = scores[val]
            dtjg = dtjgs[val]
            score_total += fz
        elif question.find("input", type="radio") and txdm != 3:
            # 处理选择题，例如是/否的单选题
            radio_inputs = question.find_all("input", type="radio")
            zbxmdm = radio_inputs[-1]["value"]
            fz = 0
            dtjg = "否"
        else:
            continue
        if count == 5:
            break
        data_list.append(
            {
                "txdm": txdm,
                "zbdm": zbdm,
                "zbmc": zbmc,
                "zbxmdm": zbxmdm,
                "fz": fz,
                "dtjg": dtjg,
            }
        )
        count += 1

    # 将数据列表转换为JSON格式
    json_output = json.dumps(data_list, ensure_ascii=False, indent=4)
    logging.info(json_output)

    dataa = {
        "xnxqdm": data["xnxqdm"],
        "pjlxdm": data["pjlxdm"],
        "teadm": teadm,  # 教师代码
        "teabh": teadm,  # 教师编号
        "teaxm": data["teaxm"],  # 教师姓名
        "wjdm": data["wjdm"],  # 问卷代码
        "kcrwdm": data["kcrwdm"],  # 课程任务代码
        "kcptdm": data["kcptdm"],  # 课程平台代码
        "kcdm": data["kcdm"],  # 课程代码
        "dgksdm": dgksdm,  # 大纲课时代码
        "jxhjdm": data["jxhjdm"],  # 教学环节代码
        "wtpf": score_total,  # 问卷评分
        "pfsm": "",  # 评分说明
        # 根据实际页面中的题目结构构造dt字段
        "dt": json_output,
    }
    if not data_list:
        raise RuntimeError("评课问卷没有可提交的题目")
    if state is not None:
        state.before_submission()
    if before_submit is not None and not before_submit():
        return False
    submit_headers = headers.copy()
    submit_headers.pop("Upgrade-Insecure-Requests", None)
    submit_headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://zhjw.smu.edu.cn",
        "Referer": eval_url,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    logging.info("开始提交评课: teadm=%s dgksdm=%s ktpj=%s", teadm, dgksdm, ktpj)
    logging.info("正在提交评课: %s %s", data["teaxm"], data["kcdm"])
    response = request_with_retry(
        session, "post", save_url, data=dataa, headers=submit_headers,
        retry_count=1, allow_redirects=False,
    )
    logging.info("评课提交完成: %s %s", data["teaxm"], data["kcdm"])
    logging.info("提交请求响应状态: %s", response.status_code)
    return True


def get_pending_courses_by_date(session, target_date):
    url = "https://zhjw.smu.edu.cn/new/student/ktpj/xsktpjData"
    request_headers = headers.copy()
    request_headers.update(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://zhjw.smu.edu.cn",
            "Referer": "https://zhjw.smu.edu.cn/new/student/ktpj",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    request_headers.pop("Upgrade-Insecure-Requests", None)
    courses = []
    page = 1
    while True:
        data = {
            "jsrq": target_date.strftime("%Y-%m-%d"),
            "page": page, "rows": 60, "sort": "jsrq, jcdm2", "order": "desc",
        }
        response = request_with_retry(
            session, "post", url, headers=request_headers, data=data
        )
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            raise RuntimeError("待评课程查询响应无效，可能需要重新登录")
        if result.get("success") is False or str(result.get("state", "")).lower() in ("error", "failed", "failure"):
            raise RuntimeError("待评课程查询返回失败状态")
        rows = result["rows"]
        if any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("待评课程查询包含无效课程记录")
        total = result.get("total")
        if total is not None:
            try:
                if isinstance(total, bool) or str(int(total)) != str(total) or int(total) < 0:
                    raise ValueError
                total = int(total)
            except (ValueError, TypeError, OverflowError):
                raise RuntimeError("课程查询分页总数无效") from None
        courses.extend(rows)
        if not rows and total is not None and len(courses) < total:
            raise RuntimeError("课程查询分页数据不完整，未视为当日完成")
        if not rows or (total is not None and len(courses) >= total):
            break
        if total is None and len(rows) < 60:
            break
        page += 1
        if page > 100:
            raise RuntimeError("课程查询分页超出范围")
    return courses


def course_identity(course):
    return json.dumps([str(course.get(field, "")) for field in
                       ("teadm", "dgksdm", "ktpj")], ensure_ascii=False)


def course_end_time(course):
    """The SMU endpoint returns jksj as e.g. 2026-04-1612:00:00."""
    value = str(course.get("jksj") or "").strip()
    for pattern in ("%Y-%m-%d%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d%H:%M",
                    "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def is_pending(course):
    if "pjdm" not in course:
        raise RuntimeError("课程记录缺少评课状态字段")
    return course["pjdm"] in ("", None)


def matching_courses(courses, identity, date, query_date):
    return [item for item in courses if course_identity(item) == identity
            and str(item.get("jsrq") or query_date.isoformat())[:10] == date]


def get_courses(session, state):
    now = local_now()
    today = now.date()
    state.begin(now)
    snapshot = state.snapshot(now)
    completed = set(snapshot["completed"])
    previously_pending = snapshot["pending"]
    processed = set()
    # An omitted row is not evidence of completion: preserve saved pending
    # courses until the server explicitly confirms their evaluated status.
    deferred = set(previously_pending)
    evaluated_count = 0
    errors = []
    # Query both dates before submitting anything, so a failed query cannot be
    # mistaken for a day without courses.
    dated_courses = [(day, get_pending_courses_by_date(session, day))
                     for day in (today, today - timedelta(days=1))]
    for query_date, courses in dated_courses:
        for course in courses:
            identity = course_identity(course)
            date = str(course.get("jsrq") or query_date.isoformat())[:10]
            key = date + ":" + identity
            if key in processed:
                continue
            processed.add(key)
            if not is_pending(course):
                state.completed(key, local_now())
                deferred.discard(key)
                continue
            if key in completed:
                deferred.discard(key)
                continue
            end = course_end_time(course)
            previous = previously_pending.get(key, {})
            retry_at = previous.get("retry_at")
            waiting_for_retry = (end is not None and previous.get("end_at") == end.isoformat()
                                 and retry_at and local_now() < datetime.fromisoformat(retry_at))
            if end is None or end > local_now() or waiting_for_retry:
                state.defer(key, end, local_now())
                deferred.add(key)
                logging.info("课程尚未结束、时间不明或未到重试时间，延后评课: %s 结束=%s", key, end)
                continue
            if date > today.isoformat() or end.date() > today:
                raise RuntimeError("课程日期与查询日期不符")
            try:
                def before_submit():
                    # Form loading and earlier submissions can take minutes.
                    # Recheck after the pacing wait, just before the actual POST:
                    # the user may have evaluated elsewhere, or a class moved.
                    latest = matching_courses(
                        get_pending_courses_by_date(session, query_date), identity, date, query_date)
                    if not latest:
                        raise RuntimeError("提交前查询缺少对应课程，无法确认当前状态")
                    pending_flags = {is_pending(item) for item in latest}
                    if len(pending_flags) != 1:
                        raise RuntimeError("课程状态不一致，未提交评价")
                    if pending_flags == {False}:
                        state.completed(key, local_now())
                        deferred.discard(key)
                        return False
                    ends = {course_end_time(item) for item in latest}
                    latest_end = next(iter(ends)) if len(ends) == 1 else None
                    if latest_end is None or latest_end > local_now():
                        state.defer(key, latest_end, local_now())
                        deferred.add(key)
                        return False
                    return True

                sent = evaluate_course(session, course["teadm"], course["dgksdm"], course["ktpj"],
                                       state=state, before_submit=before_submit)
                if sent is False:
                    continue
                refreshed = get_pending_courses_by_date(session, query_date)
                matching = matching_courses(refreshed, identity, date, query_date)
                if not matching:
                    raise RuntimeError("提交后查询缺少对应课程，无法确认评课成功")
                if any(is_pending(item) for item in matching):
                    raise RuntimeError("提交后课程仍待评，未记录为成功")
                state.completed(key, local_now())
                deferred.discard(key)
                evaluated_count += 1
            except Exception as exc:
                logging.exception("课程未确认完成，将保留待评状态: %s", key)
                state.defer(key, end, local_now())
                deferred.add(key)
                errors.append(str(exc))
    if set(previously_pending) - processed:
        errors.append("查询缺少已保存的待处理课程，无法确认完成；保留记录并延迟重试")
    state.finish(deferred, local_now())
    if errors:
        raise RuntimeError(f"{len(errors)} 门课程未完成：{errors[0]}")
    return evaluated_count


def run_evaluation(account: str, password: str) -> int:
    """Run once; every invocation shares the same account's durable progress."""
    if not account or not password:
        raise ValueError("账号或密码为空")
    state = EvaluationState.for_account(account)
    with state.run_lock():
        # Record an incomplete attempt before network activity, including login.
        state.begin(local_now())
        for _ in range(5):
            with requests.Session() as session:
                captcha = get_captcha(session)
                ticket = login(account, password, captcha, session)
                if ticket == "failed":
                    continue
                redirect_login(session, ticket)
                return get_courses(session, state)
        raise RuntimeError("登录失败，已达到最大重试次数")


def configure_cli_logging() -> None:
    logging.basicConfig(
        filename=Path(__file__).resolve().parent / "evaluation.log",
        filemode="w",
        format="%(asctime)s | %(levelname)s:  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )


def main():
    configure_cli_logging()
    settings = Settings.load(Path(__file__).resolve().parent / "config.ini")
    settings.validate()
    run_evaluation(settings.account, settings.password)


if __name__ == "__main__":
    main()
