"""Regression guards for the pre-desktop evaluation implementation.

The fingerprints were generated from commit 5bc1948, immediately before the
Windows desktop refactor. Logging calls and the processed-course counter are
deliberately ignored: they are desktop observability improvements rather than
business rules. Everything that talks to SMU or constructs answers/payloads is
still covered.
"""

import ast
import hashlib
from pathlib import Path

import main
import pytest

CORE_FINGERPRINTS = {
    "request_with_retry": "cb1166ab69b9be9f68e294bb1dee4ba38360c10db0f417142b37dae68872b5d5",
    "get_captcha": "8dda4fc6d7336a24b2e552cf74e528c60c5e59002fc9ad1d11db4e6d5c6d1eed",
    "login": "f42c3280ee6da40b1d4dc9e058b60c6ac4e658396daadc15026890b91c7bb7c9",
    "redirect_login": "9c68ffbc483bba519bddfb10c7eec7c8936dd5d4287e47be497e74394fb2cbb2",
    "evaluate_course": "7a69ad0a61133d6bdcb282d314daee90cf29310a0cc47c61215c45d2d59db06f",
    "get_pending_courses_by_date": "e4639a230f3686cf7c7461995c4778fc457f1b8796b3e65edcd17b1a436a8a1f",
    "get_courses": "82842aa1e18f000b6d6693a37845c6e5147f1547f185b9b9cf9f3fb3ac3a3741",
}


class BusinessLogicNormalizer(ast.NodeTransformer):
    """Remove logging and course-count instrumentation before fingerprinting."""

    def visit_Expr(self, node):
        node = self.generic_visit(node)
        call = node.value
        if isinstance(call, ast.Call):
            function = call.func
            if isinstance(function, ast.Name) and function.id == "print":
                return None
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "logging"
            ):
                return None
        return node

    def visit_Assign(self, node):
        if any(
            isinstance(target, ast.Name) and target.id == "evaluated_count"
            for target in node.targets
        ):
            return None
        return self.generic_visit(node)

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Name) and node.target.id == "evaluated_count":
            return None
        return self.generic_visit(node)

    def visit_Return(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == "evaluated_count":
            return None
        return self.generic_visit(node)


def test_core_logic_matches_original_script():
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    tree = BusinessLogicNormalizer().visit(tree)
    ast.fix_missing_locations(tree)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in CORE_FINGERPRINTS
    }
    actual = {
        name: hashlib.sha256(
            ast.dump(node, include_attributes=False).encode()
        ).hexdigest()
        for name, node in functions.items()
    }
    assert actual == CORE_FINGERPRINTS


def test_desktop_wrapper_preserves_original_login_flow(monkeypatch):
    sessions = [object(), object()]
    created = iter(sessions)
    captchas = iter(["1111", "2222"])
    tickets = iter(["failed", "ticket"])
    calls = []

    monkeypatch.setattr(main.requests, "Session", lambda: next(created))
    monkeypatch.setattr(main, "get_captcha", lambda session: next(captchas))
    monkeypatch.setattr(
        main, "login", lambda account, password, captcha, session: next(tickets)
    )
    monkeypatch.setattr(
        main, "redirect_login", lambda session, ticket: calls.append((session, ticket))
    )
    monkeypatch.setattr(main, "get_courses", lambda session: calls.append(session))

    main.run_evaluation("account", "password")

    assert calls == [(sessions[1], "ticket"), sessions[1]]


def test_desktop_wrapper_validates_credentials():
    with pytest.raises(ValueError, match="账号或密码为空"):
        main.run_evaluation("", "password")


def test_course_query_returns_processed_count(monkeypatch):
    course = {"pjdm": "", "teadm": "teacher", "dgksdm": "period", "ktpj": "form"}
    monkeypatch.setattr(
        main,
        "get_pending_courses_by_date",
        lambda session, target_date: [course],
    )
    submitted = []
    monkeypatch.setattr(main, "evaluate_course", lambda *args: submitted.append(args))

    assert main.get_courses(object()) == 1
    assert len(submitted) == 1  # duplicate from the second queried date is ignored
