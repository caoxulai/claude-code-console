"""Tests for B-3: _start_reaper startup hook exception handling in chat.py.

Verifies that _start_reaper logs an error (with exc_info) and re-raises when
manager.start_reaper() throws, so aiohttp stops boot but the error is diagnosable.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
import server.routes.chat as chat_mod  # noqa: E402


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


async def test_start_reaper_logs_and_reraises_on_failure(caplog):
    """When manager.start_reaper() raises, _start_reaper must:
    1. Log an error at ERROR level with exc_info=True
    2. Re-raise the exception (so aiohttp stops boot)
    """
    # Build a minimal app dict with a manager that raises
    app = {"_worker_registry": []}
    fake_manager = MagicMock()
    fake_manager.start_reaper.side_effect = RuntimeError("reaper init boom")
    app["session_manager"] = fake_manager

    # _start_reaper must re-raise
    with pytest.raises(RuntimeError, match="reaper init boom"):
        with caplog.at_level(logging.ERROR, logger="server.routes.chat"):
            await chat_mod._start_reaper(app)

    # Verify the error was logged
    assert any(
        "Failed to start session reaper" in record.message
        and record.levelno == logging.ERROR
        and record.exc_info is not None
        for record in caplog.records
    ), f"Expected a logger.error with 'Failed to start session reaper', got: {[r.message for r in caplog.records]}"


async def test_start_reaper_success_no_error_logged(caplog):
    """On the happy path, _start_reaper must NOT log any error."""
    app = {"_worker_registry": []}
    fake_manager = MagicMock()
    fake_manager.start_reaper.return_value = MagicMock()  # a fake task
    app["session_manager"] = fake_manager

    with caplog.at_level(logging.ERROR, logger="server.routes.chat"):
        await chat_mod._start_reaper(app)

    # No error logs on success
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"Unexpected error logs: {[r.message for r in error_records]}"

    # Verify the task was assigned
    assert app["session_reaper_task"] is fake_manager.start_reaper.return_value
