"""Subprocess hardening for the READ-ONLY /api/oscron mirror (D-078 items 25, 27d).

Three properties are pinned here:

  * TIMEOUT — ``_run`` bounds ``proc.communicate()`` with
    ``asyncio.wait_for(..., _SUBPROCESS_TIMEOUT_S)``. A wedged
    ``crontab``/``systemctl``/``journalctl`` used to hang the request forever.
    ``wait_for`` cancels ``communicate()`` but leaves the child alive, so the
    timeout path must ``kill()`` AND ``await wait()`` (reap) — otherwise every
    timeout orphans a process still holding its pipes.
  * GRACEFUL DEGRADE — a timed-out call returns the SAME ``(127, "")`` tuple the
    missing-binary path returns, so every caller keeps degrading to an empty
    list / "not available" instead of 500-ing.
  * BOUNDED JOURNAL READS — the journalctl seams pass ``-n <_JOURNAL_MAX_LINES>``
    so a busy unit's entire journal is never slurped into memory.

No real ``crontab``/``systemctl``/``journalctl`` is ever spawned: the fake lives
at ``asyncio.create_subprocess_exec`` (which the conftest hermeticity trap also
wraps — these tests override it for their own duration, exactly as that trap's
docstring sanctions).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from server.app import create_app  # noqa: E402
import server.routes.oscron as oscron_mod  # noqa: E402

# Small enough that a wedged-subprocess test finishes instantly, large enough to
# never flake on a loaded box.
_FAST_TIMEOUT_S = 0.05


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    application["allowed_cwd_roots"] = [tmp_path]
    application["default_cwd"] = tmp_path
    application["permission_mode"] = "bypassPermissions"
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture(autouse=True)
def _reset_worker_primitives():
    """create_app() starts the slack/email workers; their lazily loop-bound
    asyncio primitives leak across tests otherwise ("bound to a different event
    loop" at the NEXT test's teardown)."""
    def _clear():
        for mod_name in ("server.routes.slack", "server.routes.email"):
            mod = sys.modules.get(mod_name)
            if mod is None:
                continue
            for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
                if hasattr(mod, attr):
                    setattr(mod, attr, None)
    _clear()
    yield
    _clear()


class _WedgedProc:
    """A subprocess whose ``communicate()`` never returns; records kill/wait."""

    def __init__(self):
        self.returncode = None
        self.killed = 0
        self.waited = 0

    async def communicate(self):
        await asyncio.sleep(3600)
        raise AssertionError("communicate() should have been cancelled by wait_for")

    def kill(self):
        self.killed += 1

    async def wait(self):
        self.waited += 1
        self.returncode = -9
        return self.returncode


@pytest.fixture
def wedged(monkeypatch) -> list[_WedgedProc]:
    """Every subprocess spawn yields a process that hangs forever."""
    spawned: list[_WedgedProc] = []

    async def fake_exec(*argv, **kwargs):
        proc = _WedgedProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(oscron_mod, "_SUBPROCESS_TIMEOUT_S", _FAST_TIMEOUT_S)
    return spawned


# --------------------------------------------------------------------------- #
# Timeout + kill/reap
# --------------------------------------------------------------------------- #
async def _run_bounded(argv):
    """Call the seam under an OUTER deadline so an unbounded _run fails the test
    fast instead of hanging the suite for an hour (there is no pytest-timeout)."""
    return await asyncio.wait_for(oscron_mod._run(argv), timeout=5)


async def test_run_times_out_and_degrades_to_127(wedged):
    started = time.monotonic()
    rc, out = await _run_bounded(["crontab", "-l"])
    elapsed = time.monotonic() - started

    assert (rc, out) == (127, "")  # same graceful-degrade tuple as missing-binary
    assert elapsed < 2.0, f"a wedged call must not hang the caller (took {elapsed:.2f}s)"


async def test_run_timeout_kills_and_reaps_the_child(wedged):
    await _run_bounded(["journalctl", "--user", "-u", "x.service"])

    assert len(wedged) == 1
    proc = wedged[0]
    assert proc.killed == 1, "wait_for cancels communicate() but leaves the child running"
    assert proc.waited == 1, "a killed child must be reaped or it lingers as a zombie"


async def test_run_timeout_logs_warning_naming_the_binary(wedged, caplog):
    with caplog.at_level("WARNING", logger="server.routes.oscron"):
        await _run_bounded(["systemctl", "--user", "list-timers"])
    assert any("systemctl" in rec.message for rec in caplog.records)


@pytest.mark.parametrize("path_tmpl", [
    "/api/oscron",
    "/api/oscron/{job_id}/runs",
    "/api/oscron/{job_id}/log",
])
async def test_oscron_endpoints_return_when_subprocess_wedges(client, wedged, path_tmpl):
    url = path_tmpl.format(job_id="deadbeef")
    started = time.monotonic()
    resp = await asyncio.wait_for(client.get(url), timeout=10)
    elapsed = time.monotonic() - started

    assert resp.status == 200
    body = await resp.json()
    assert elapsed < 5.0, f"{url} hung on a wedged subprocess ({elapsed:.2f}s)"
    # Bounded body: each source degraded to empty rather than 500-ing.
    if url == "/api/oscron":
        assert body == {"jobs": []}
    elif url.endswith("/runs"):
        assert body["runs"] == [] and body["parsed"] is False
    else:
        assert body["lines"] == [] and body["error"] == "not available"


async def test_wedged_subprocesses_are_all_killed_and_reaped(client, wedged):
    resp = await asyncio.wait_for(client.get("/api/oscron"), timeout=10)
    assert resp.status == 200
    assert wedged, "expected the three /api/oscron sources to spawn"
    for proc in wedged:
        assert proc.killed == 1
        assert proc.waited == 1


# --------------------------------------------------------------------------- #
# journalctl is line-bounded
# --------------------------------------------------------------------------- #
@pytest.fixture
def captured_argv(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_run(argv):
        calls.append(list(argv))
        return 127, ""

    monkeypatch.setattr(oscron_mod, "_run", fake_run)
    return calls


@pytest.mark.parametrize("seam", ["_run_journalctl_user", "_run_journalctl_system"])
async def test_journalctl_argv_is_line_bounded(captured_argv, seam):
    await getattr(oscron_mod, seam)("backup.service")

    assert len(captured_argv) == 1
    argv = captured_argv[0]
    assert argv[0] == "journalctl"
    assert "-n" in argv, f"{seam} must bound the read with -n"
    assert argv[argv.index("-n") + 1] == str(oscron_mod._JOURNAL_MAX_LINES)
    assert oscron_mod._JOURNAL_MAX_LINES > 0


# --------------------------------------------------------------------------- #
# Numeric query params never 500 (27d)
# --------------------------------------------------------------------------- #
def _patch_sources(monkeypatch, crontab_text: str):
    async def fake_crontab():
        return 0, crontab_text

    async def fake_empty():
        return 1, ""

    monkeypatch.setattr(oscron_mod, "_run_crontab", fake_crontab)
    monkeypatch.setattr(oscron_mod, "_run_systemctl_user", fake_empty)
    monkeypatch.setattr(oscron_mod, "_run_systemctl_system", fake_empty)


async def _crontab_job_id(client, monkeypatch, log: Path):
    _patch_sources(monkeypatch, f"# job\n0 3 * * * /bin/run.sh >> {log} 2>&1\n")
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    return next(j["id"] for j in jobs if j["source"] == "crontab")


@pytest.mark.parametrize("query", [
    "tail=abc",
    "tail=",
    "tail=1.5",
    "lineStart=abc&lineEnd=xyz",
    "lineStart=1&lineEnd=nope",
])
async def test_log_bad_numeric_query_falls_back_to_default(client, monkeypatch, tmp_path, query):
    log = tmp_path / "job.log"
    log.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    job_id = await _crontab_job_id(client, monkeypatch, log)

    resp = await client.get(f"/api/oscron/{job_id}/log?{query}")
    assert resp.status == 200, f"?{query} must not 500"
    body = await resp.json()
    assert body["lines"] == ["alpha", "beta", "gamma"]
