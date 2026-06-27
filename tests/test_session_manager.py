"""Tests for B-3 (startup hook exception handling) and B-4 (bounded event queue).

B-4 — PersistentSession._events must have maxsize=2000 and a drop-oldest policy
    (get_nowait before put_nowait when full) with a logger.warning on each drop.

B-3 — Startup hooks (_start_reaper, _start_watcher, _start_cleanup_worker) must
    wrap their bodies in try/except Exception, log an error, and RE-RAISE — because
    aiohttp's Signal.send iterates receivers in a bare for-loop (no try/except), so
    an unhandled raise propagates up through app.startup() and stops boot. The
    try/except adds observability (the logger.error call) without changing behavior:
    the exception still kills boot just like before, but now it's logged.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from server.session_manager import PersistentSession  # noqa: E402
import server.routes.chat as chat_mod  # noqa: E402
import server.routes.sessions as sessions_mod  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# B-4: Bounded event queue tests
# ══════════════════════════════════════════════════════════════════════════════


class TestBoundedEventQueue:
    """Verify PersistentSession._events is bounded with drop-oldest semantics."""

    def test_events_queue_has_maxsize_2000(self):
        """The queue must be bounded at maxsize=2000."""
        s = PersistentSession(session_id="test-bounded")
        assert s._events.maxsize == 2000

    def test_enqueue_event_drops_oldest_when_full(self):
        """When the queue is full, a new put drops the oldest item first."""
        s = PersistentSession(session_id="test-drop")
        # Replace _events with a small bounded queue to test drop logic.
        s._events = asyncio.Queue(maxsize=3)
        # Fill the queue.
        s._events.put_nowait({"type": "assistant", "seq": 1})
        s._events.put_nowait({"type": "assistant", "seq": 2})
        s._events.put_nowait({"type": "assistant", "seq": 3})
        assert s._events.full()

        # Enqueue a 4th via the production logic path:
        # The _enqueue_event helper must drop oldest, then put.
        s._enqueue_event({"type": "assistant", "seq": 4})

        # The oldest (seq=1) should be gone; seq 2, 3, 4 remain.
        items = []
        while not s._events.empty():
            items.append(s._events.get_nowait())
        assert [i["seq"] for i in items] == [2, 3, 4]

    def test_enqueue_event_logs_warning_on_drop(self, caplog):
        """logger.warning must fire when a drop occurs."""
        s = PersistentSession(session_id="test-log-drop")
        s._events = asyncio.Queue(maxsize=2)
        s._events.put_nowait({"type": "assistant", "seq": 1})
        s._events.put_nowait({"type": "assistant", "seq": 2})

        with caplog.at_level(logging.WARNING, logger="server.session_manager"):
            s._enqueue_event({"type": "assistant", "seq": 3})

        assert any("drop" in r.message.lower() or "full" in r.message.lower()
                   for r in caplog.records), (
            f"Expected a WARNING about dropping; got: {[r.message for r in caplog.records]}"
        )

    def test_enqueue_event_no_warning_when_not_full(self, caplog):
        """No warning when the queue has room."""
        s = PersistentSession(session_id="test-no-warn")
        s._events = asyncio.Queue(maxsize=5)
        s._events.put_nowait({"type": "assistant", "seq": 1})

        with caplog.at_level(logging.WARNING, logger="server.session_manager"):
            s._enqueue_event({"type": "assistant", "seq": 2})

        drop_warnings = [r for r in caplog.records
                         if "drop" in r.message.lower() or "full" in r.message.lower()]
        assert drop_warnings == []


# ══════════════════════════════════════════════════════════════════════════════
# B-3: Startup hook exception handling tests
# ══════════════════════════════════════════════════════════════════════════════


def _minimal_app() -> web.Application:
    """A bare Application with just the keys the startup hooks need."""
    app = web.Application()
    app["ws_manager"] = MagicMock()
    app["_worker_registry"] = []
    return app


class TestStartReaperHook:
    """_start_reaper must log and re-raise on failure."""

    async def test_start_reaper_logs_and_reraises_on_failure(self, caplog):
        """When SessionManager.start_reaper raises, _start_reaper logs error and re-raises."""
        app = _minimal_app()
        mock_mgr = MagicMock()
        mock_mgr.start_reaper.side_effect = RuntimeError("reaper init boom")
        app["session_manager"] = mock_mgr

        with caplog.at_level(logging.ERROR, logger="server.routes.chat"):
            with pytest.raises(RuntimeError, match="reaper init boom"):
                await chat_mod._start_reaper(app)

        assert any("reaper" in r.message.lower() or "boom" in r.message.lower()
                   for r in caplog.records if r.levelno >= logging.ERROR), (
            f"Expected an ERROR log about the reaper failure; got: {[r.message for r in caplog.records]}"
        )

    async def test_start_reaper_happy_path_unchanged(self, caplog):
        """Happy path: start_reaper sets app['session_reaper_task'] and logs no error."""
        app = _minimal_app()
        mock_mgr = MagicMock()
        fake_task = MagicMock()
        mock_mgr.start_reaper.return_value = fake_task
        app["session_manager"] = mock_mgr

        with caplog.at_level(logging.ERROR, logger="server.routes.chat"):
            await chat_mod._start_reaper(app)

        assert app["session_reaper_task"] is fake_task
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Unexpected error logs on happy path: {[r.message for r in error_records]}"
        )


def _raising_create_task(error_msg):
    """Return a side_effect that closes the passed coroutine (avoiding warnings) then raises."""
    def _side_effect(coro, **kwargs):
        # Close the coroutine to avoid "was never awaited" warnings.
        coro.close()
        raise RuntimeError(error_msg)
    return _side_effect


def _fake_create_task(fake_task):
    """Return a side_effect that closes the coroutine and returns a fake task."""
    def _side_effect(coro, **kwargs):
        coro.close()
        return fake_task
    return _side_effect


class TestStartWatcherHook:
    """_start_watcher must log and re-raise on failure."""

    async def test_start_watcher_logs_and_reraises_on_failure(self, caplog, monkeypatch):
        """When asyncio.create_task raises inside _start_watcher, it logs and re-raises."""
        app = _minimal_app()
        monkeypatch.setattr(asyncio, "create_task",
                            MagicMock(side_effect=_raising_create_task("watcher task boom")))

        with caplog.at_level(logging.ERROR, logger="server.routes.sessions"):
            with pytest.raises(RuntimeError, match="watcher task boom"):
                await sessions_mod._start_watcher(app)

        assert any("watcher" in r.message.lower() or "boom" in r.message.lower()
                   for r in caplog.records if r.levelno >= logging.ERROR), (
            f"Expected an ERROR log; got: {[r.message for r in caplog.records]}"
        )

    async def test_start_watcher_happy_path_unchanged(self, caplog, monkeypatch):
        """Happy path: _start_watcher creates the task and registers the worker."""
        app = _minimal_app()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        monkeypatch.setattr(asyncio, "create_task",
                            MagicMock(side_effect=_fake_create_task(fake_task)))

        with caplog.at_level(logging.ERROR, logger="server.routes.sessions"):
            await sessions_mod._start_watcher(app)

        assert app.get("session_watch_task") is fake_task
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []


class TestStartCleanupWorkerHook:
    """_start_cleanup_worker must log and re-raise on failure."""

    async def test_start_cleanup_worker_logs_and_reraises_on_failure(self, caplog, monkeypatch):
        """When create_task raises, _start_cleanup_worker logs and re-raises."""
        app = _minimal_app()
        monkeypatch.setattr(asyncio, "create_task",
                            MagicMock(side_effect=_raising_create_task("cleanup boom")))

        with caplog.at_level(logging.ERROR, logger="server.routes.sessions"):
            with pytest.raises(RuntimeError, match="cleanup boom"):
                await sessions_mod._start_cleanup_worker(app)

        assert any("cleanup" in r.message.lower() or "boom" in r.message.lower()
                   for r in caplog.records if r.levelno >= logging.ERROR), (
            f"Expected an ERROR log; got: {[r.message for r in caplog.records]}"
        )

    async def test_start_cleanup_worker_happy_path_unchanged(self, caplog, monkeypatch):
        """Happy path: task is created, no error logged."""
        app = _minimal_app()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        monkeypatch.setattr(asyncio, "create_task",
                            MagicMock(side_effect=_fake_create_task(fake_task)))

        with caplog.at_level(logging.ERROR, logger="server.routes.sessions"):
            await sessions_mod._start_cleanup_worker(app)

        assert app.get("session_cleanup_task") is fake_task
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []
