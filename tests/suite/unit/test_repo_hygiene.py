# tests/suite/unit/test_repo_hygiene.py
# =============================================================================
# Release D: the public repo must not carry virtualenvs, binaries, release
# zips, local databases, or real-looking credentials. Before Release D it held
# all of these, including R2 keys in .env.example. These checks read the git
# index, so they catch a bad `git add` before it is committed and pushed.
# =============================================================================
import re
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]

FORBIDDEN = ["venv/*", ".venv/*", "*.exe", "*.zip", "*.db", "*.db-shm", "*.db-wal",
             "*.sqlite", "*.sqlite3", ".env", ".batch_jobs_snapshot.json"]


def _tracked_files():
    if not shutil.which("git"):
        pytest.skip("git not installed")
    r = subprocess.run(["git", "ls-files"], cwd=BACKEND, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("not a git checkout")
    return [f for f in r.stdout.splitlines() if f]


def test_no_virtualenvs_binaries_zips_or_databases_are_tracked():
    bad = [f for f in _tracked_files() if any(fnmatch(f, p) for p in FORBIDDEN)]
    assert bad == [], f"remove with: git rm --cached <file>  ->  {bad[:10]}"


def test_env_example_holds_placeholders_only():
    text = (BACKEND / ".env.example").read_text(encoding="utf-8")
    # 32+ hex characters is the shape of a real R2 / AWS-style key or account id
    assert not re.search(r"[0-9a-f]{32,}", text)
    assert not re.search(r"sk_(live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}", text)
    # A generated key (secrets.token_urlsafe) is long and mixes digits and
    # upper case; a placeholder like "replace-with-..." has neither.
    for m in re.finditer(r"^#?\s*SECRET_KEY=(\S+)", text, re.M):
        v = m.group(1)
        looks_generated = (len(v) >= 32 and sum(c.isdigit() for c in v) >= 4
                           and sum(c.isupper() for c in v) >= 4)
        assert not looks_generated, "SECRET_KEY in .env.example looks like a real key"


def test_dockerignore_keeps_secrets_and_tests_out_of_the_image():
    lines = {l.strip() for l in (BACKEND / ".dockerignore").read_text().splitlines()}
    for must in (".env", ".git", "venv/", "tests/", "*.db-wal", "*.exe", "*.zip"):
        assert must in lines, must
