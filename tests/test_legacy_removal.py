"""Legacy-surface removal pins (D-078 items 21 + 24).

Two vestigial surfaces are removed in this batch and must STAY removed:

  * the parallel root implementation — root ``server.py`` (a 285-line legacy
    server with its own auth and a shell-executing chat handler), root
    ``run.py``, and the pre-React root ``static/`` UI.  Nothing in-repo
    referenced them: the ``import server.routes...`` statements bind to the
    ``server/`` PACKAGE, which shadows root server.py.
  * ``CLAUDE_WEB_SECRET`` — root server.py was its ONLY consumer, and the
    import-time hard-raise in server/config.py meant a fresh clone could not
    even collect the test suite.

These are tracked-file / source-text invariants, so they are pinned against
``git ls-files`` and the checked-in text rather than against runtime behaviour.
Fully hermetic: no network, no writes, only reads under the repo root.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> set[str]:
    """Repo-relative paths git currently tracks, skipping a non-git checkout."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed — cannot inspect tracked files")
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.skip(f"not a git checkout ({result.stderr.strip()})")
    return {line for line in result.stdout.splitlines() if line}


# ─── the parallel root implementation is gone ────────────────────────────────

@pytest.mark.parametrize("path", ["server.py", "run.py"])
def test_legacy_root_module_not_tracked(path):
    assert path not in _tracked_files(), (
        f"root {path} is the deleted legacy parallel implementation — it must "
        "not be tracked again (the live server is the server/ package)."
    )


def test_legacy_root_static_dir_not_tracked():
    tracked = _tracked_files()
    stale = sorted(p for p in tracked if p == "static" or p.startswith("static/"))
    assert stale == [], (
        f"root static/ is the deleted pre-React UI; still tracked: {stale}. "
        "The shipped UI is built from frontend/ into server/static/."
    )


def test_legacy_root_files_absent_from_worktree():
    """Deleted via `git rm`, so they must not linger as untracked leftovers."""
    for name in ("server.py", "run.py", "static"):
        assert not (REPO_ROOT / name).exists(), (
            f"{name} still exists in the worktree — it should have been removed, "
            "not merely untracked."
        )


def test_server_imports_bind_to_the_package():
    """`import server...` resolves to the package, not a root module."""
    import server

    assert Path(server.__file__).resolve() == (REPO_ROOT / "server" / "__init__.py").resolve()


# ─── CLAUDE_WEB_SECRET is gone end to end ────────────────────────────────────

def test_env_example_has_no_secret():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "CLAUDE_WEB_SECRET" not in text, (
        "CLAUDE_WEB_SECRET was removed entirely (its only consumer was the "
        "deleted root server.py) — .env.example must not advertise it."
    )


def test_env_example_has_no_stale_server_py_comment():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "server.py will auto-generate" not in text, (
        ".env.example's header still credits the deleted root server.py with "
        "auto-generating a secret."
    )
    assert "server.py" not in text, (
        ".env.example must not reference the deleted root server.py at all."
    )


def test_server_package_has_no_secret_references():
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in sorted((REPO_ROOT / "server").rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "CLAUDE_WEB_SECRET" in line
    ]
    assert hits == [], f"CLAUDE_WEB_SECRET still referenced under server/: {hits}"


def test_config_imports_with_a_fully_scrubbed_environment():
    """A fresh clone (no .env, no CLAUDE_WEB_* env) must import server.config.

    Runs in a subprocess with an EMPTY environment and HOME repointed at a tmp
    dir so neither the developer's .env nor the real ~/.claude-web/config.json
    can supply anything.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import server.config as c; "
            "assert not hasattr(c.cfg, 'secret'); print('ok')",
            str(REPO_ROOT),
        ],
        cwd=str(REPO_ROOT),
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-claude-web-test-home"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "importing server.config with a scrubbed environment must succeed.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ok" in result.stdout
