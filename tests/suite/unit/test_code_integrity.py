# tests/suite/unit/test_code_integrity.py
# =============================================================================
# Release D1 (Nov 2026). A cleanup commit deleted the header of
# subscription_status() together with nearby commented-out code. The rest of
# its body silently became unreachable code at the end of cancel_subscription,
# and GET /subscription_status disappeared. These checks catch that whole class
# of accident: endpoints served by the wrong function, and code that can never
# run because it sits after a return/raise.
# =============================================================================
import ast
from pathlib import Path

import pytest
from starlette.routing import Match

import main

BACKEND = Path(__file__).resolve().parents[3]

# (method, path) -> the function that must handle it
CORE_ENDPOINTS = {
    ("GET", "/subscription_status"): "subscription_status",
    ("POST", "/billing/cancel_subscription"): "cancel_subscription",
    ("POST", "/billing/create_checkout_session"): "create_checkout_session",
    ("DELETE", "/user/delete_account"): "delete_account_endpoint",
    ("POST", "/auth/forgot-password"): "forgot_password",
    ("POST", "/token_json"): "token_login_json",
    ("GET", "/users/me"): "read_users_me",
    ("GET", "/health/ready"): "health_ready",
}


def _handler(method, path):
    scope = {"type": "http", "method": method, "path": path, "root_path": "",
             "query_string": b"", "headers": []}
    for route in main.app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route.endpoint, "__name__", None)
    return None


@pytest.mark.parametrize("method, path", sorted(CORE_ENDPOINTS))
def test_core_endpoint_is_served_by_its_own_function(method, path):
    # A catch-all (for example the frontend fallback) answering instead
    # would return HTML with status 200 and fool status-code-only checks.
    assert _handler(method, path) == CORE_ENDPOINTS[(method, path)]


def _unreachable(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for i, st in enumerate(stmts[:-1]):
                if isinstance(st, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    nxt = stmts[i + 1]
                    yield f"{path.name}:{nxt.lineno} (after line {st.lineno})"


@pytest.mark.parametrize("filename", ["main.py", "batch.py", "email_utils.py"])
def test_no_code_after_return_or_raise(filename):
    path = BACKEND / filename
    if not path.exists():
        pytest.skip(f"{filename} not present")
    assert list(_unreachable(path)) == []
