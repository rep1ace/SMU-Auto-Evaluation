import configparser
from datetime import datetime, timedelta
import json
import logging
import re
from io import BytesIO
import ddddocr
from hashlib import md5
from PIL import Image
from bs4 import BeautifulSoup
import requests
import random

captcha_url = "https://uis.smu.edu.cn/imageServlet.do"
login_url = "https://uis.smu.edu.cn/login/login.do"
REQUEST_TIMEOUT = (10, 30)
REQUEST_RETRY_COUNT = 2
headers = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "Host": "zhjw.smu.edu.cn",
    "Referer": "https://zhjw.smu.edu.cn/",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}

logging.basicConfig(
    filename="evaluation.log",
    filemode="w",
    format="%(asctime)s | %(levelname)s:  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)


def request_with_retry(session, method, url, **kwargs):
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    last_error = None
    for attempt in range(1, REQUEST_RETRY_COUNT + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            logging.warning(
                "请求失败，第 %s/%s 次重试: %s %s 错误=%s",
                attempt,
                REQUEST_RETRY_COUNT,
                method.upper(),
                url,
                exc,
            )
    raise RuntimeError(f"请求失败: {method.upper()} {url} 错误={last_error}")


def get_captcha(session):
    headers_captcha = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Host": "uis.smu.edu.cn",
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
    ocr = ddddocr.DdddOcr(beta=True)
    result = ocr.classification(img)
    return result


# 登录
def login(account, password, captcha, session):
    password_md5 = md5(password.encode()).hexdigest()
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Content-Length": "234",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Host": "uis.smu.edu.cn",
        "Origin": "https://uis.smu.edu.cn",
        "Referer": "https://uis.smu.edu.cn/login.jsp?redirect=https%3A%2F%2Fzhjw.smu.edu.cn%2Fnew%2FssoLogin",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "X-KL-kis-Ajax-Request": "Ajax_Request",
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
    if response.status_code == 200 and "成功" in response.text:
        logging.info("登陆成功")
        resp_json = json.loads(response.text)
        ticket = resp_json["ticket"]
        return ticket

    else:
        logging.error(msg="登录失败， " + response.text + " 验证码：" + captcha)
        return "failed"


def redirect_login(session, ticket):
    url = "https://zhjw.smu.edu.cn/new/ssoLogin"
    params = {
        "ticket": ticket,
    }
    resp = request_with_retry(session, "get", url, headers=headers, params=params)
    print(resp.status_code)


def evaluate_course(session, teadm, dgksdm, ktpj):
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
            print(
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
        return
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
    option = [-1, -2, -3]
    scores = [25, 20, 15]
    dtjgs = ["★★★★★", "★★★★", "★★★"]
    score_total = 0
    for question in questions:
        combination = combinations[random.randint(0, 3)]
        val = random.randint(0, 2)
        while not combination[val]:
            val = random.randint(0, 2)
        combination[val] -= 1
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
    logging.info("开始提交评课: teadm=%s dgksdm=%s ktpj=%s", teadm, dgksdm, ktpj)
    print(f"正在提交评课: {data['teaxm']} {data['kcdm']}")
    response = request_with_retry(
        session, "post", save_url, data=dataa, headers=headers
    )
    print(f"评课提交完成: {data['teaxm']} {data['kcdm']}")
    logging.info(response.text)


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
    data = {
        "jsrq": target_date.strftime("%Y-%m-%d"),
        "page": 1,
        "rows": 60,
        "sort": "jsrq, jcdm2",
        "order": "desc",
    }
    response = request_with_retry(
        session, "post", url, headers=request_headers, data=data
    )
    courses_info = response.json()
    logging.info("%s 待评课程查询结果: %s", data["jsrq"], courses_info)
    return courses_info.get("rows", [])


def get_courses(session):
    today = datetime.now().date()
    query_dates = [today, today - timedelta(days=1)]
    processed_courses = set()

    for query_date in query_dates:
        for course in get_pending_courses_by_date(session, query_date):
            if course.get("pjdm") != "":
                continue

            course_key = (
                course.get("teadm"),
                course.get("dgksdm"),
                course.get("ktpj"),
            )
            if course_key in processed_courses:
                continue

            processed_courses.add(course_key)
            evaluate_course(session, course["teadm"], course["dgksdm"], course["ktpj"])


def main():
    config = configparser.ConfigParser()
    config.read("config.ini")
    account = config.get("login", "account")
    password = config.get("login", "password")
    attempt = 5
    session = None
    ticket = "failed"
    for i in range(attempt):
        session = requests.Session()
        captcha = get_captcha(session)
        ticket = login(account, password, captcha, session)
        if ticket != "failed":
            break
    if session is None or ticket == "failed":
        raise RuntimeError("登录失败，已达到最大重试次数")
    redirect_login(session, ticket)
    get_courses(session)


if __name__ == "__main__":
    main()
