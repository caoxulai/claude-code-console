"""Tests for the worker registry module (server/routes/workers.py).

Covers:
  (a) GET /api/workers returns empty list initially
  (b) After register_worker the endpoint returns the entry with correct fields
  (c) mark_worker_run updates lastRunAt and lastResult
  (d) Status derivation: mock tasks in various states
  (e) Project filter returns union of matching + global workers
  (f) Nonexistent project filter returns only global workers
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.routes.workers import register, register_worker, mark_worker_run  # noqa: E402
from server.app import create_app  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


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
    """Reset Slack/email lazily-created loop-bound asyncio primitives between tests."""
    def _clear():
        for mod_name in ("server.routes.slack", "server.routes.email"):
            try:
                mod = sys.modules.get(mod_name) or __import__(mod_name, fromlist=["x"])
            except ImportError:
                continue
            for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
                if hasattr(mod, attr):
                    setattr(mod, attr, None)
    _clear()
    yield
    _clear()


# ─── (a) Empty list initially ────────────────────────────────────────────────


async def test_workers_returns_empty_list_initially(client):
    """GET /api/workers returns only built-in workers when no custom workers are registered."""
    resp = await client.get("/api/workers")
    assert resp.status == 200
    data = await resp.json()
    # create_app() registers 10 built-in workers at startup via on_startup callbacks
    _BUILTIN_NAMES = {
        "Session Watcher", "Session Cleanup", "Session Reaper",
        "Email Scanner", "Email Classify Worker", "Email Draft Worker",
        "Slack Watcher", "Slack Draft Worker", "Slack Classify Worker", "Slack Scanner",
    }
    names = {e["name"] for e in data}
    assert names == _BUILTIN_NAMES


# ─── (b) register_worker adds entry with correct fields ──────────────────────


async def test_register_worker_adds_entry(client, app):
    """After calling register_worker, GET /api/workers returns the entry."""
    register_worker(app, "test-scan", "test_scan_task", 300, project="myproj", description="Scans things")
    # Put a running task into the app so status resolves
    app["test_scan_task"] = asyncio.ensure_future(asyncio.sleep(9999))
    try:
        resp = await client.get("/api/workers")
        assert resp.status == 200
        data = await resp.json()
        entry = [e for e in data if e["name"] == "test-scan"][0]
        assert entry["name"] == "test-scan"
        assert entry["status"] == "running"
        assert entry["project"] == "myproj"
        assert entry["description"] == "Scans things"
        assert entry["intervalS"] == 300
        assert entry["startedAt"] is not None
        assert entry["startedAt"].endswith("Z")
        assert entry["lastRunAt"] is None
        assert entry["lastResult"] is None
    finally:
        app["test_scan_task"].cancel()
        try:
            await app["test_scan_task"]
        except asyncio.CancelledError:
            pass


# ─── (c) mark_worker_run updates lastRunAt and lastResult ─────────────────────


async def test_mark_worker_run_updates_fields(client, app):
    """mark_worker_run sets lastRunAt to current UTC time and lastResult."""
    register_worker(app, "test-draft", "test_draft_task", 7)
    app["test_draft_task"] = asyncio.ensure_future(asyncio.sleep(9999))
    try:
        # Before marking
        resp = await client.get("/api/workers")
        entry = [e for e in (await resp.json()) if e["name"] == "test-draft"][0]
        assert entry["lastRunAt"] is None
        assert entry["lastResult"] is None

        # Mark a run
        mark_worker_run(app, "test-draft", result="ok: 3 items drafted")

        resp = await client.get("/api/workers")
        entry = [e for e in (await resp.json()) if e["name"] == "test-draft"][0]
        assert entry["lastRunAt"] is not None
        assert entry["lastRunAt"].endswith("Z")
        assert entry["lastResult"] == "ok: 3 items drafted"
    finally:
        app["test_draft_task"].cancel()
        try:
            await app["test_draft_task"]
        except asyncio.CancelledError:
            pass


async def test_mark_worker_run_nonexistent_name_noop(app):
    """mark_worker_run with a name not in registry silently does nothing."""
    # Should not raise
    mark_worker_run(app, "does-not-exist", result="boom")


# ─── (d) Status derivation ───────────────────────────────────────────────────


async def test_status_running(client, app):
    """A live (not done) asyncio task reports 'running'."""
    register_worker(app, "w-running", "w_running_task", 60)
    app["w_running_task"] = asyncio.ensure_future(asyncio.sleep(9999))
    try:
        resp = await client.get("/api/workers")
        entry = [e for e in await resp.json() if e["name"] == "w-running"][0]
        assert entry["status"] == "running"
    finally:
        app["w_running_task"].cancel()
        try:
            await app["w_running_task"]
        except asyncio.CancelledError:
            pass


async def test_status_stopped_none_task(client, app):
    """When app[taskKey] is None, status is 'stopped'."""
    register_worker(app, "w-none", "w_none_task", 60)
    app["w_none_task"] = None

    resp = await client.get("/api/workers")
    entry = [e for e in await resp.json() if e["name"] == "w-none"][0]
    assert entry["status"] == "stopped"


async def test_status_stopped_cancelled_task(client, app):
    """A cancelled (done) task reports 'stopped'."""
    register_worker(app, "w-cancelled", "w_cancelled_task", 60)
    task = asyncio.ensure_future(asyncio.sleep(9999))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app["w_cancelled_task"] = task

    resp = await client.get("/api/workers")
    entry = [e for e in await resp.json() if e["name"] == "w-cancelled"][0]
    assert entry["status"] == "stopped"


async def test_status_errored_task(client, app):
    """A done task with an exception reports 'errored'."""
    register_worker(app, "w-errored", "w_errored_task", 60)

    async def _failing():
        raise RuntimeError("boom")

    task = asyncio.ensure_future(_failing())
    # Let the task finish so it holds the exception
    await asyncio.sleep(0.01)
    app["w_errored_task"] = task

    resp = await client.get("/api/workers")
    entry = [e for e in await resp.json() if e["name"] == "w-errored"][0]
    assert entry["status"] == "errored"


async def test_status_stopped_completed_task(client, app):
    """A done task that completed normally (no exception, not cancelled) reports 'stopped'."""
    register_worker(app, "w-done", "w_done_task", 60)

    async def _ok():
        return "done"

    task = asyncio.ensure_future(_ok())
    await asyncio.sleep(0.01)
    app["w_done_task"] = task

    resp = await client.get("/api/workers")
    entry = [e for e in await resp.json() if e["name"] == "w-done"][0]
    assert entry["status"] == "stopped"


async def test_status_uses_live_task_reference(client, app):
    """Status derivation uses the LIVE app[taskKey], not a stale registration snapshot."""
    register_worker(app, "w-live", "w_live_task", 60)

    # Initially running
    task1 = asyncio.ensure_future(asyncio.sleep(9999))
    app["w_live_task"] = task1

    resp = await client.get("/api/workers")
    entry = [e for e in await resp.json() if e["name"] == "w-live"][0]
    assert entry["status"] == "running"

    # Now replace with a cancelled task (simulating crash + no restart)
    task1.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass
    app["w_live_task"] = task1

    resp = await client.get("/api/workers")
    entry = [e for e in await resp.json() if e["name"] == "w-live"][0]
    assert entry["status"] == "stopped"


# ─── (e) Project filter returns union of matching + global workers ────────────


async def test_project_filter_returns_union(client, app):
    """?project=X returns workers for that project PLUS global (project=None) workers."""
    register_worker(app, "w-global", "w_global_task", 300, project=None, description="Global")
    register_worker(app, "w-proj-a", "w_proj_a_task", 60, project="alpha", description="Alpha worker")
    register_worker(app, "w-proj-b", "w_proj_b_task", 120, project="beta", description="Beta worker")

    # Ensure tasks exist (all running)
    for key in ("w_global_task", "w_proj_a_task", "w_proj_b_task"):
        app[key] = asyncio.ensure_future(asyncio.sleep(9999))

    try:
        resp = await client.get("/api/workers?project=alpha")
        assert resp.status == 200
        data = await resp.json()
        names = {e["name"] for e in data}
        # Should include global + alpha, NOT beta
        assert "w-global" in names, "Global worker missing from project filter result"
        assert "w-proj-a" in names, "Matching project worker missing"
        assert "w-proj-b" not in names, "Non-matching project worker should be excluded"
    finally:
        for key in ("w_global_task", "w_proj_a_task", "w_proj_b_task"):
            app[key].cancel()
            try:
                await app[key]
            except asyncio.CancelledError:
                pass


# ─── (f) Nonexistent project filter returns only global workers ───────────────


async def test_nonexistent_project_filter_returns_only_global(client, app):
    """?project=nonexistent returns only global workers (project=None)."""
    register_worker(app, "w-global2", "w_global2_task", 300, project=None)
    register_worker(app, "w-proj-c", "w_proj_c_task", 60, project="gamma")

    for key in ("w_global2_task", "w_proj_c_task"):
        app[key] = asyncio.ensure_future(asyncio.sleep(9999))

    try:
        resp = await client.get("/api/workers?project=nonexistent")
        assert resp.status == 200
        data = await resp.json()
        names = {e["name"] for e in data}
        assert "w-global2" in names, "Global worker should appear for nonexistent project"
        assert "w-proj-c" not in names, "Project-scoped worker should not appear for wrong project"
    finally:
        for key in ("w_global2_task", "w_proj_c_task"):
            app[key].cancel()
            try:
                await app[key]
            except asyncio.CancelledError:
                pass


# ─── No project filter returns all workers ────────────────────────────────────


async def test_no_project_filter_returns_all(client, app):
    """Without ?project param, all workers are returned regardless of project."""
    register_worker(app, "w-all-1", "w_all_1_task", 60, project=None)
    register_worker(app, "w-all-2", "w_all_2_task", 60, project="delta")
    register_worker(app, "w-all-3", "w_all_3_task", 60, project="epsilon")

    for key in ("w_all_1_task", "w_all_2_task", "w_all_3_task"):
        app[key] = asyncio.ensure_future(asyncio.sleep(9999))

    try:
        resp = await client.get("/api/workers")
        assert resp.status == 200
        data = await resp.json()
        names = {e["name"] for e in data}
        assert "w-all-1" in names
        assert "w-all-2" in names
        assert "w-all-3" in names
    finally:
        for key in ("w_all_1_task", "w_all_2_task", "w_all_3_task"):
            app[key].cancel()
            try:
                await app[key]
            except asyncio.CancelledError:
                pass
