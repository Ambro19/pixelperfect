# tests/suite/unit/test_log_safety.py
# =============================================================================
# Release C: the database password must never reach the logs.
# Before this release the startup banner logged DATABASE_URL in full.
# =============================================================================
import ast
import logging
from pathlib import Path

import pytest

from log_safety import mask_db_url

BACKEND = Path(__file__).resolve().parents[3]
SECRET = "S3cr3t-Pa55word"


# ------------------------------------------------------------- mask_db_url
@pytest.mark.parametrize("url", [
    f"postgresql+psycopg2://pp_user:{SECRET}@dpg-abc123-a/pixelperfect_db",
    f"postgres://pp_user:{SECRET}@dpg-abc123-a.oregon-postgres.render.com:5432/db",
    f"postgresql://pp_user:{SECRET}@dpg-abc123-a/db?sslmode=require",
])
def test_password_is_replaced_and_the_rest_is_kept(url):
    out = mask_db_url(url)
    assert SECRET not in out
    assert "***" in out
    assert "pp_user" in out and "dpg-abc123-a" in out   # still useful to read


def test_url_encoded_special_characters_are_masked_too():
    out = mask_db_url("postgresql://u:p%40ss%3Aw%2Frd@host/db")
    assert "p%40ss" not in out and "p@ss" not in out and "***" in out


def test_a_string_sqlalchemy_cannot_parse_is_still_masked():
    out = mask_db_url(f"not a url but has ://admin:{SECRET}@10.0.0.1 in it")
    assert SECRET not in out and "***" in out


@pytest.mark.parametrize("url", ["sqlite:///./pixelperfect.db", "postgresql://u@host/db"])
def test_urls_without_a_password_are_unchanged(url):
    assert mask_db_url(url) == url


@pytest.mark.parametrize("value", [None, ""])
def test_empty_values_never_raise(value):
    assert mask_db_url(value) == str(value)


# ----------------------------------------------------------- startup banner
async def test_startup_banner_does_not_log_the_password(monkeypatch, caplog):
    import main

    monkeypatch.setattr(main, "DATABASE_URL",
                        f"postgresql+psycopg2://pp_user:{SECRET}@dpg-abc123-a/pixelperfect_db")
    for name in ("initialize_database", "run_startup_migrations",
                 "run_api_key_migration", "_verify_required_routes"):
        monkeypatch.setattr(main, name, lambda *a, **k: None)

    async def no_browser():
        return None
    monkeypatch.setattr(main.screenshot_service, "initialize", no_browser)

    with caplog.at_level(logging.DEBUG):
        await main.on_startup()

    banner = [r.getMessage() for r in caplog.records if "PixelPerfect starting" in r.getMessage()]
    assert banner, "startup banner not found - update this test"
    assert SECRET not in caplog.text
    assert "***" in banner[0] and "dpg-abc123-a" in banner[0]


# ------------------------------------------------------- source-level guard
def _calls_passing_database_url(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name not in {"print", "debug", "info", "warning", "error", "exception", "critical", "log"}:
            continue
        for arg in list(node.args) + [k.value for k in node.keywords]:
            # A bare DATABASE_URL (or one inside an f-string) is a leak;
            # mask_db_url(DATABASE_URL) is fine.
            parts = arg.values if isinstance(arg, ast.JoinedStr) else [arg]
            for p in parts:
                v = p.value if isinstance(p, ast.FormattedValue) else p
                if isinstance(v, ast.Name) and v.id == "DATABASE_URL":
                    yield f"{path.name}:{node.lineno}"


@pytest.mark.parametrize("filename", ["main.py", "delete_user.py"])
def test_no_log_or_print_call_passes_the_raw_database_url(filename):
    assert list(_calls_passing_database_url(BACKEND / filename)) == []
