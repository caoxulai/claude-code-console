"""Fresh-clone import pin — `import server.config` must succeed with ZERO env setup (D-078 item 5).

Before this batch, ``server/config.py`` hard-raised at IMPORT time when
``CLAUDE_WEB_SECRET`` was unset, so a fresh clone could not even collect the
test suite. The local suite passed only because ``load_dotenv(ENV_PATH)`` picked
the value up from the developer's gitignored ``.env`` — the classic
"green because of the developer's machine" trap.

This test therefore refuses to trust the in-process import (this process was
started by a runner that may already carry the var and whose ``server.config``
is long since imported). It launches a CHILD interpreter in a FULLY SCRUBBED
environment and asserts on the child's observable result:

  * env is rebuilt from scratch — no ``CLAUDE_WEB_*`` var survives, ``HOME``
    points at ``tmp_path`` so the real ``~/.claude-web/config.json`` and the
    real ``~/.claude`` tree are unreachable;
  * ``server/`` is reached through a SYMLINK inside ``tmp_path``, so
    ``ENV_PATH = Path(config.__file__).parent.parent / ".env"`` resolves to
    ``<tmp_path>/.env`` — a path that does not exist. dotenv is therefore
    provably a no-op: the developer's repo-root ``.env`` is never a candidate,
    rather than merely being expected not to matter.

Assertions: the child exits 0 (import + ``cfg`` construction succeed) and the
built config exposes NO ``secret`` attribute (the field is gone, not merely
defaulted to "").
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Emitted by the child as a single JSON line so the parent asserts on data, not
# on scraped log text.
_CHILD_PROGRAM = """
import json, sys
import server.config as config

print("__RESULT__" + json.dumps({
    "config_file": config.__file__,
    "env_path": str(config.ENV_PATH),
    "env_path_exists": config.ENV_PATH.exists(),
    "has_secret_attr": hasattr(config.cfg, "secret"),
    "config_fields": sorted(config.cfg.__dataclass_fields__),
    "secret_in_env": "CLAUDE_WEB_SECRET" in __import__("os").environ,
}))
"""


def _run_fresh_import(tmp_path: Path) -> subprocess.CompletedProcess:
    """Import server.config in a child with a scrubbed env and a symlinked package."""
    stage = tmp_path / "fresh_clone"
    stage.mkdir()
    (stage / "server").symlink_to(_REPO_ROOT / "server")

    home = tmp_path / "home"
    home.mkdir()

    # Built from scratch (NOT os.environ.copy()) so no CLAUDE_WEB_* var leaks in.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "PYTHONPATH": str(stage),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM],
        cwd=str(stage),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _result_of(proc: subprocess.CompletedProcess) -> dict:
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise AssertionError(
        "child emitted no result line.\n"
        f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_import_server_config_succeeds_in_fully_scrubbed_env(tmp_path):
    proc = _run_fresh_import(tmp_path)
    assert proc.returncode == 0, (
        "importing server.config with zero environment setup must succeed "
        "(a fresh clone has no .env and no CLAUDE_WEB_SECRET).\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    result = _result_of(proc)
    # The scrub actually scrubbed: no secret var, and dotenv had no file to read.
    assert result["secret_in_env"] is False
    assert result["env_path_exists"] is False, (
        "ENV_PATH must not exist in the staged clone — otherwise this test could "
        f"be passing off the developer's .env: {result['env_path']}"
    )


def test_built_config_has_no_secret_field(tmp_path):
    proc = _run_fresh_import(tmp_path)
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    result = _result_of(proc)
    assert result["has_secret_attr"] is False, (
        "AppConfig.secret was removed in D-078 item 5 (its only consumer was the "
        "deleted root server.py); a surviving field would re-invite the "
        "import-time requirement."
    )
    assert "secret" not in result["config_fields"]


def test_config_module_source_has_no_secret_requirement():
    source = (_REPO_ROOT / "server" / "config.py").read_text()
    assert "CLAUDE_WEB_SECRET" not in source
