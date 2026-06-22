"""Tests for the session-cleanup endpoint and background worker.

The cleanup logic deletes stale print-mode session files (first record type
'queue-operation', mtime strictly older than 30 days) while preserving:
  - Interactive sessions (regardless of age)
  - Recent print-mode sessions (<=30 days old)
  - Unparseable files (ambiguous — never delete)

These tests drive the cleanup via the DELETE /api/sessions/cleanup endpoint
and the worker lifecycle, using a monkeypatched CLAUDE_PROJECTS_BASE pointed
at tmp_path with directly-created session files. Pattern mirrors
test_session_watcher.py.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from aiohttp import web

import server.routes.sessions as sessions_mod


# ─── Helpers ────────────────────────────────────────────────────────────────


class _RecordingWsManager:
    """Minimal ws_manager stand-in that records broadcast (event_type, data)."""

    def __init__(self):
        self.events: list = []

    async def broadcast(self, event_type, data=None):
        self.events.append((event_type, data))


def _build_app() -> tuple[web.Application, _RecordingWsManager]:
    """A bare Application with sessions.register() wired and a recording ws_manager."""
    app = web.Application()
    ws = _RecordingWsManager()
    app["ws_manager"] = ws
    app["default_cwd"] = Path("/tmp/test-cwd")
    app["_worker_registry"] = []
    sessions_mod.register(app)
    return app, ws


def _write_session(base: Path, name: str, records: list[dict],
                   mtime_days_ago: float | None = None) -> Path:
    """Write a session .jsonl with the given records under a project bucket in `base`.

    If mtime_days_ago is provided, set the file's mtime to that many days in the past.
    """
    # Use a simple project bucket name that won't be filtered by EXCLUDED_DIRS
    proj = base / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    content = "\n".join(json.dumps(r) for r in records) + "\n"
    path.write_text(content, encoding="utf-8")

    if mtime_days_ago is not None:
        old_time = time.time() - (mtime_days_ago * 86400)
        os.utime(path, (old_time, old_time))
    return path


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_sessions_base(tmp_path, monkeypatch):
    """Point CLAUDE_PROJECTS_BASE at tmp_path for every test in this file."""
    base = tmp_path / "projects"
    base.mkdir()
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    return base


@pytest.fixture
def base(tmp_path):
    """The projects base directory under tmp_path (matches the autouse fixture)."""
    return tmp_path / "projects"


# ─── (1) test_cleanup_deletes_old_print_mode_sessions ───────────────────────


async def test_cleanup_deletes_old_print_mode_sessions(base, aiohttp_client):
    """A print-mode session older than 30 days is deleted by the cleanup endpoint."""
    path = _write_session(base, "old-print", [{"type": "queue-operation"}],
                          mtime_days_ago=31)
    assert path.exists()

    app, _ws = _build_app()
    client = await aiohttp_client(app)
    resp = await client.delete("/api/sessions/cleanup")
    assert resp.status == 200
    body = await resp.json()
    assert body["deleted"] == 1
    assert not path.exists(), "stale print-mode file should have been deleted"


# ─── (2) test_cleanup_preserves_interactive_sessions ────────────────────────


async def test_cleanup_preserves_interactive_sessions(base, aiohttp_client):
    """An interactive session (first record type='mode', has user turn) survives cleanup."""
    path = _write_session(base, "interactive-old", [
        {"type": "mode", "mode": "interactive"},
        {"type": "user", "message": "hello"},
    ], mtime_days_ago=60)
    assert path.exists()

    app, _ws = _build_app()
    client = await aiohttp_client(app)
    resp = await client.delete("/api/sessions/cleanup")
    assert resp.status == 200
    body = await resp.json()
    assert body["deleted"] == 0
    assert path.exists(), "interactive session must NOT be deleted"


# ─── (3) test_cleanup_preserves_recent_print_mode ───────────────────────────


async def test_cleanup_preserves_recent_print_mode(base, aiohttp_client):
    """A print-mode session with recent mtime (15 days ago) is not deleted."""
    path = _write_session(base, "recent-print", [{"type": "queue-operation"}],
                          mtime_days_ago=15)
    assert path.exists()

    app, _ws = _build_app()
    client = await aiohttp_client(app)
    resp = await client.delete("/api/sessions/cleanup")
    assert resp.status == 200
    body = await resp.json()
    assert body["deleted"] == 0
    assert path.exists(), "recent print-mode session must NOT be deleted"


# ─── (4) test_cleanup_preserves_boundary_30_days ────────────────────────────


async def test_cleanup_preserves_boundary_30_days(base, aiohttp_client, monkeypatch):
    """A print-mode session exactly 30 days old (boundary) is NOT deleted.

    'Older than 30 days' means strictly more than 30 days elapsed (> 30*86400).
    A file at exactly the boundary is preserved. The implementation uses
    st.st_mtime >= cutoff (cutoff = now - 30*86400), so mtime == cutoff passes
    the >= check and the file is preserved.

    We freeze time.time inside the cleanup helper to eliminate wall-clock drift
    between os.utime and the cutoff computation.
    """
    # Pin a fixed "now" so the test is deterministic regardless of execution time.
    fixed_now = 1_750_000_000.0  # arbitrary epoch timestamp
    boundary_mtime = fixed_now - (30 * 86400)  # exactly 30 days before fixed_now

    proj = base / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "boundary-print.jsonl"
    path.write_text(json.dumps({"type": "queue-operation"}) + "\n", encoding="utf-8")
    os.utime(path, (boundary_mtime, boundary_mtime))
    assert path.exists()

    # Freeze time.time for the cleanup helper so cutoff = fixed_now - 30*86400
    # == boundary_mtime. The file's st_mtime == cutoff → >= holds → preserved.
    monkeypatch.setattr(time, "time", lambda: fixed_now)

    app, _ws = _build_app()
    client = await aiohttp_client(app)
    resp = await client.delete("/api/sessions/cleanup")
    assert resp.status == 200
    body = await resp.json()
    assert body["deleted"] == 0
    assert path.exists(), "boundary (exactly 30 days) file must NOT be deleted"


# ─── (5) test_cleanup_skips_unparseable_files ───────────────────────────────


async def test_cleanup_skips_unparseable_files(base, aiohttp_client):
    """A file with invalid JSON on line 1 is never deleted (ambiguous = preserved)."""
    proj = base / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "corrupt.jsonl"
    path.write_text("this is not valid json {{{\n", encoding="utf-8")
    old_time = time.time() - (60 * 86400)
    os.utime(path, (old_time, old_time))
    assert path.exists()

    app, _ws = _build_app()
    client = await aiohttp_client(app)
    resp = await client.delete("/api/sessions/cleanup")
    assert resp.status == 200
    body = await resp.json()
    assert body["deleted"] == 0
    assert path.exists(), "unparseable file must NOT be deleted"


# ─── (6) test_cleanup_no_body_required ──────────────────────────────────────


async def test_cleanup_no_body_required(base, aiohttp_client):
    """DELETE /api/sessions/cleanup with no body returns 200 (no params needed)."""
    app, _ws = _build_app()
    client = await aiohttp_client(app)
    resp = await client.delete("/api/sessions/cleanup")
    assert resp.status == 200
    body = await resp.json()
    assert body["deleted"] == 0


# ─── (7) test_cleanup_worker_lifecycle ──────────────────────────────────────


async def test_cleanup_worker_lifecycle(aiohttp_client, base):
    """on_startup starts the cleanup task; on_cleanup cancels+awaits it cleanly.

    Mirrors test_session_watcher.py lifecycle test: the cleanup worker sleeps
    FIRST (86400s) so it is inert on startup — no files are touched during the
    test. The task exists and is running after startup, then is cancelled and
    done after cleanup.
    """
    app, _ws = _build_app()
    client = await aiohttp_client(app)

    # on_startup fired → the cleanup task exists and is running (sleeping).
    task = app.get("session_cleanup_task")
    assert task is not None, "session_cleanup_task must be created on startup"
    assert not task.done(), "cleanup task should be sleeping (not yet done)"

    # on_cleanup fires here → the task is cancelled and awaited.
    await client.close()

    assert task.done(), "cleanup task still pending after cleanup"
    assert task.cancelled(), "cleanup task should be cancelled on shutdown"
