"""Tests for the E2 session-transcript file-watcher in server/routes/sessions.py.

The watcher broadcasts 'session_changed' when the NEWEST transcript's mtime
advances, so SessionsPage can refetch the open transcript promptly instead of
relying on its (raised-to-30s) backstop poll. These tests drive _session_watcher
directly with a tiny interval and a tmp CLAUDE_PROJECTS_BASE, asserting:

  (a) it is INERT — no broadcast at startup and none across several idle ticks
      when nothing changes [E2-2];
  (b) it broadcasts EXACTLY ONCE within ~one tick when the newest transcript's
      mtime strictly advances [E2-1];
  (c) it tears down cleanly via the on_startup/on_cleanup lifecycle — no lingering
      task, no CancelledError leak, no 'Task was destroyed' warning [E2-4].
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from aiohttp import web

import server.routes.sessions as sessions_mod


class _RecordingWsManager:
    """Minimal ws_manager stand-in that records broadcast (event_type, data)."""

    def __init__(self):
        self.events: list = []

    async def broadcast(self, event_type, data=None):
        self.events.append((event_type, data))


def _build_app() -> tuple[web.Application, _RecordingWsManager]:
    """A bare Application with sessions.register() wired and a recording ws_manager.

    register() installs the watcher's on_startup/on_cleanup hooks but does NOT
    start the task — the task only starts under the on_startup signal (driven
    here directly or via aiohttp_client in the lifecycle test).
    """
    app = web.Application()
    ws = _RecordingWsManager()
    app["ws_manager"] = ws
    app["_worker_registry"] = []
    sessions_mod.register(app)
    return app, ws


def _write_transcript(base: Path, name: str, mtime_ns: int | None = None) -> Path:
    """Write a minimal transcript .jsonl under a project bucket in `base`."""
    proj = base / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "hi"}) + "\n",
                    encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


async def _drive_one_cycle(app, watcher_coro, *, settle=0.2):
    """Start the watcher task with a fast interval, let it run, then cancel+await."""
    task = asyncio.create_task(watcher_coro(app))
    try:
        await asyncio.sleep(settle)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return task


# ── (a) inert: no broadcast at startup and none across idle ticks [E2-2] ──────


async def test_session_watcher_inert_with_no_change(tmp_path, monkeypatch):
    base = tmp_path / "projects"
    _write_transcript(base, "sess-idle")
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(sessions_mod, "SESSION_WATCH_INTERVAL_S", 0.01)

    app, ws = _build_app()
    # Let the watcher seed its baseline and run several ticks with NO file change.
    await _drive_one_cycle(app, sessions_mod._session_watcher, settle=0.25)

    assert ws.events == [], (
        "watcher broadcast on startup / across idle ticks with no change: "
        f"{ws.events}"
    )


async def test_session_watcher_inert_with_empty_base(tmp_path, monkeypatch):
    """An absent/empty base must seed None and never broadcast (no fabrication)."""
    base = tmp_path / "does-not-exist"  # never created
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(sessions_mod, "SESSION_WATCH_INTERVAL_S", 0.01)

    app, ws = _build_app()
    await _drive_one_cycle(app, sessions_mod._session_watcher, settle=0.15)

    assert ws.events == []
    # The probe itself degrades to None rather than raising on a missing base.
    assert sessions_mod._newest_transcript_mtime() is None


# ── (b) exactly one broadcast when the newest mtime advances [E2-1] ───────────


async def test_session_watcher_broadcasts_once_on_mtime_advance(tmp_path, monkeypatch):
    base = tmp_path / "projects"
    path = _write_transcript(base, "sess-live")
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(sessions_mod, "SESSION_WATCH_INTERVAL_S", 0.01)

    app, ws = _build_app()
    task = asyncio.create_task(sessions_mod._session_watcher(app))
    try:
        # Let the watcher seed its baseline mtime from the existing file.
        await asyncio.sleep(0.05)
        assert ws.events == [], "watcher broadcast before any change (baseline tick)"

        # Simulate the CLI appending a line: bump mtime to a strictly-later ns.
        bumped = path.stat().st_mtime_ns + 1_000_000
        os.utime(path, ns=(bumped, bumped))

        # Within ~one tick the watcher should observe the advance and broadcast.
        for _ in range(50):
            if any(ev[0] == "session_changed" for ev in ws.events):
                break
            await asyncio.sleep(0.02)

        session_changed = [ev for ev in ws.events if ev[0] == "session_changed"]
        assert len(session_changed) == 1, (
            f"expected exactly one session_changed broadcast, got {ws.events}"
        )

        # No further change → no further broadcasts across additional ticks.
        await asyncio.sleep(0.1)
        session_changed = [ev for ev in ws.events if ev[0] == "session_changed"]
        assert len(session_changed) == 1, (
            f"watcher broadcast again with no further change: {ws.events}"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_session_watcher_picks_newest_across_files(tmp_path, monkeypatch):
    """A brand-new transcript that is the newest file triggers a broadcast."""
    base = tmp_path / "projects"
    now = time.time_ns()
    _write_transcript(base, "old-a", mtime_ns=now - 5_000_000_000)
    _write_transcript(base, "old-b", mtime_ns=now - 4_000_000_000)
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(sessions_mod, "SESSION_WATCH_INTERVAL_S", 0.01)

    app, ws = _build_app()
    task = asyncio.create_task(sessions_mod._session_watcher(app))
    try:
        await asyncio.sleep(0.05)  # seed baseline from the two old files
        assert ws.events == []

        # A new session file written now is the newest mtime → one broadcast.
        _write_transcript(base, "new-live", mtime_ns=time.time_ns())
        for _ in range(50):
            if any(ev[0] == "session_changed" for ev in ws.events):
                break
            await asyncio.sleep(0.02)

        assert [ev for ev in ws.events if ev[0] == "session_changed"], (
            f"watcher did not broadcast after a newer transcript appeared: {ws.events}"
        )
        # The payload is informational only; the frontend refetches on the event.
        evt = next(ev for ev in ws.events if ev[0] == "session_changed")
        assert evt[1] == {"watched": True}
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── (c) clean lifecycle teardown — no lingering task / CancelledError leak ─────


async def test_session_watcher_lifecycle_teardown(aiohttp_client, tmp_path, monkeypatch):
    """on_startup starts the task; on_cleanup cancels+awaits it with no leak [E2-4]."""
    base = tmp_path / "projects"
    _write_transcript(base, "sess-lifecycle")
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    # Keep the real 5s interval here: it stays asleep for the test's lifetime,
    # proving the watcher is inert on startup under aiohttp_client(app).
    monkeypatch.setattr(sessions_mod, "SESSION_WATCH_INTERVAL_S", 5)

    app, ws = _build_app()
    client = await aiohttp_client(app)

    # on_startup fired → the watcher task exists and is running (not yet done).
    task = app.get("session_watch_task")
    assert task is not None and not task.done()
    # Inert on startup: the 5s sleep means no broadcast has fired yet.
    assert ws.events == []

    # on_cleanup fires here → the task is cancelled and awaited.
    await client.close()

    assert task.done(), "watcher task still pending after cleanup"
    assert app.get("session_watch_task") is None, "watcher key not popped on cleanup"
    # A cancelled task that re-raised CancelledError reports itself cancelled,
    # not as having raised an unhandled exception.
    assert task.cancelled()


async def test_stop_watcher_is_a_noop_when_never_started(monkeypatch):
    """_stop_watcher tolerates an app that never started the watcher (idempotent)."""
    app, _ws = _build_app()
    assert app.get("session_watch_task") is None
    await sessions_mod._stop_watcher(app)  # must not raise
    assert app.get("session_watch_task") is None
