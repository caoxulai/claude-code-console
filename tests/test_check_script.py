"""Behavioral tests for scripts/check.sh — the one-command repo check gate.

These run the ACTUAL script bytes against FAKE tool stubs in a throwaway repo
root (so no real 900-test suite / npm build is invoked, and there is no
recursion). They pin the two properties that make the gate trustworthy:

  * CHECK-SCRIPT-FALSE-GREEN: a failing FATAL step (pytest / npm test / build)
    must propagate a non-zero exit — the script may never report success while a
    step is red.
  * ESLINT-FATAL: the eslint step is NON-FATAL — a lint failure alone must NOT
    fail the gate (the repo has ~50 pre-existing lint errors scheduled for a
    later batch).

They also confirm the script resolves the repo root from its own location
(runnable from any cwd) and that README documents it.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check.sh"

_PYTHON_STUB = """#!/usr/bin/env bash
# fake .venv/bin/python: only ever invoked as `python -m pytest tests/ -q`
exit "${FAKE_PYTEST_RC:-0}"
"""

_NPM_STUB = """#!/usr/bin/env bash
# fake npm: handles `npm test` and `npm run build`
case "$1" in
  test) exit "${FAKE_NPM_TEST_RC:-0}";;
  run)  exit "${FAKE_NPM_BUILD_RC:-0}";;
  *)    exit 0;;
esac
"""

_NPX_STUB = """#!/usr/bin/env bash
# fake npx: only ever invoked as `npx eslint .`
exit "${FAKE_ESLINT_RC:-0}"
"""


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_tree(tmp_path):
    """A throwaway repo root with a copy of check.sh and fake tool stubs.

    Returns (repo_root, run) where run(**env) executes the copied script from a
    cwd OUTSIDE the tree (to prove cwd-independence) and returns the
    CompletedProcess.
    """
    if not CHECK_SCRIPT.exists():
        pytest.fail(f"{CHECK_SCRIPT} does not exist — the gate script must be created")

    root = tmp_path / "repo"
    # Copy the real script under test into the fake root.
    _write_exec(root / "scripts" / "check.sh", CHECK_SCRIPT.read_text())
    # Fake .venv/bin/python (invoked by absolute repo-root path).
    _write_exec(root / ".venv" / "bin" / "python", _PYTHON_STUB)
    # frontend dir must exist for the `cd frontend` steps.
    (root / "frontend").mkdir(parents=True, exist_ok=True)
    # Fake npm / npx on PATH.
    fakebin = tmp_path / "fakebin"
    _write_exec(fakebin / "npm", _NPM_STUB)
    _write_exec(fakebin / "npx", _NPX_STUB)

    # Run from a cwd that is NOT the repo root, to prove self-location resolution.
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    def run(**stub_env):
        env = dict(os.environ)
        env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
        env.update({k: str(v) for k, v in stub_env.items()})
        return subprocess.run(
            ["bash", str(root / "scripts" / "check.sh")],
            cwd=str(outside),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return root, run


def test_all_steps_pass_exits_zero(fake_tree):
    _root, run = fake_tree
    result = run()
    assert result.returncode == 0, (
        f"all-green run must exit 0, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_failing_pytest_propagates_nonzero(fake_tree):
    """CHECK-SCRIPT-FALSE-GREEN: a red backend test must fail the gate."""
    _root, run = fake_tree
    result = run(FAKE_PYTEST_RC=7)
    assert result.returncode != 0, (
        "a failing pytest step must NOT be swallowed — the gate must exit "
        f"non-zero.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_failing_npm_test_propagates_nonzero(fake_tree):
    """CHECK-SCRIPT-FALSE-GREEN: a red frontend test must fail the gate."""
    _root, run = fake_tree
    result = run(FAKE_NPM_TEST_RC=3)
    assert result.returncode != 0, (
        "a failing frontend `npm test` must fail the gate.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_failing_build_propagates_nonzero(fake_tree):
    """CHECK-SCRIPT-FALSE-GREEN: a broken frontend build must fail the gate."""
    _root, run = fake_tree
    result = run(FAKE_NPM_BUILD_RC=5)
    assert result.returncode != 0, (
        "a failing frontend build must fail the gate.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_eslint_failure_is_nonfatal(fake_tree):
    """ESLINT-FATAL: lint errors alone must NOT fail the gate (reported only)."""
    _root, run = fake_tree
    result = run(FAKE_ESLINT_RC=1)
    assert result.returncode == 0, (
        "eslint must be NON-FATAL — a lint failure alone must not fail the gate "
        f"(got exit {result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # ...and it must actually have been reported, not silently skipped.
    assert "eslint" in result.stdout.lower(), (
        "eslint step should be reported in output.\n"
        f"stdout:\n{result.stdout}"
    )


def test_script_is_executable():
    assert CHECK_SCRIPT.exists(), f"{CHECK_SCRIPT} must exist"
    mode = CHECK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/check.sh must be executable (chmod +x)"


def test_script_uses_strict_bash_flags():
    """set -euo pipefail is the structural guarantee against false greens."""
    text = CHECK_SCRIPT.read_text()
    assert "set -euo pipefail" in text, (
        "check.sh must start with `set -euo pipefail` so any failing step "
        "propagates a non-zero exit (anti CHECK-SCRIPT-FALSE-GREEN)."
    )


def test_script_marks_eslint_flip_to_fatal_point():
    """A contributor must be able to find exactly where to make eslint fatal."""
    text = CHECK_SCRIPT.read_text().lower()
    assert "fatal" in text and "eslint" in text, (
        "check.sh must document the eslint flip-to-fatal point in-script."
    )


def test_readme_documents_check_script():
    readme = (REPO_ROOT / "README.md").read_text()
    assert "scripts/check.sh" in readme, (
        "README must document scripts/check.sh so contributors can discover the "
        "one-command gate without reading the script (AC-15)."
    )
