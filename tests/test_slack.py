"""Slack route tests — pins the actionable-count contract (E4).

These tests live in a SEPARATE file from tests/test_server.py (the contention
hotspot) and mirror tests/test_email.py's harness: importorskip the module, an
app/client fixture, and a slack_file fixture that repoints the sidecar at a
tmp_path JSON file. They use pytest.importorskip so the suite SKIPS (not errors)
if the slack module is unavailable.

The headline test feeds a queue spanning EVERY known status (the terminal pair
plus several active ones plus a missing/empty status) and asserts GET
/api/slack/queue/count equals the count of NON-terminal items — pinning the
denylist so a future active status can't silently desync the NavBar badge from
the page's "N to review". The frontend twin is countActionable in
frontend/src/lib/slackQueue.js (kept in lockstep with _count_actionable).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Guard: skip the entire file if the slack module is unavailable.
slack_mod = pytest.importorskip(
    "server.routes.slack",
    reason="server/routes/slack.py not importable — tests will activate once it is",
)

from server.app import create_app  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path, Path("/etc/__never__")])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    application["allowed_cwd_roots"] = [tmp_path, Path("/etc/__never__")]
    application["default_cwd"] = tmp_path
    application["permission_mode"] = "bypassPermissions"
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture
def slack_file(tmp_path: Path, monkeypatch) -> Path:
    """Repoint the Slack sidecar at a tmp_path JSON file."""
    p = tmp_path / "slack" / "slack_threads.json"
    monkeypatch.setattr(slack_mod, "SLACK_PATH", p)
    return p


def _reset_loop_bound_primitives():
    """Null the lazily-created, loop-bound asyncio primitives in BOTH the slack
    AND email modules.

    create_app() starts the slack workers AND the email workers (both register
    on_startup hooks), which lazily create loop-bound asyncio.Lock/Event inside the
    running loop. Without a reset, a primitive created in one test's event loop is
    reused by the next test and raises 'RuntimeError: ... is bound to a different
    event loop'. Resetting only slack is NOT enough — the email scan worker's
    Event leaks the same way. Mirrors tests/test_server.py:_reset_slack_scan_guard.
    """
    for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
        if hasattr(slack_mod, attr):
            setattr(slack_mod, attr, None)
    try:
        from server.routes import email as email_mod
    except ImportError:
        return
    for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
        if hasattr(email_mod, attr):
            setattr(email_mod, attr, None)


@pytest.fixture(autouse=True)
def _reset_slack_primitives():
    _reset_loop_bound_primitives()
    yield
    _reset_loop_bound_primitives()


def _seed(slack_file, items, **extra):
    """Write a seeded sidecar and return the path."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    data = {"items": items, **extra}
    slack_file.write_text(json.dumps(data), encoding="utf-8")
    return slack_file


def _make_item(id="i1", status="needs-review", **overrides):
    """Build a minimal Slack queue item with sensible defaults."""
    base = {
        "id": id,
        "channelId": "D0123",
        "userId": "U0123",
        "sender": "John Doe",
        "channel": "DM with John Doe",
        "channelType": "im",
        "snippet": "hey, ping",
        "draft": "auto draft",
        "generatedDraft": "auto draft",
        "status": status,
        "ts": 1718000000000,
    }
    base.update(overrides)
    return base


# ─── Test cases ──────────────────────────────────────────────────────────────


async def test_slack_queue_count_matches_actionable(client, slack_file):
    """GET /api/slack/queue/count returns {count: N} == the actionable count.

    Seeds a queue spanning EVERY known status — the four core active statuses
    (needs-classify, needs-draft, needs-review, edited), an extra active one
    ('fyi'), the terminal pair (sent + dismissed), and a missing/empty status
    ('') — then asserts the lightweight /count endpoint equals the count of
    NON-terminal items in the full /queue response. This PINS the denylist so a
    future active status addition can't silently desync the NavBar bubble from
    the page's "N to review". Terminal = sent or dismissed; a missing/empty
    status counts.
    """
    _seed(slack_file, [
        _make_item("c1", status="needs-classify"),
        _make_item("c2", status="needs-draft"),
        _make_item("c3", status="needs-review"),
        _make_item("c4", status="edited"),
        _make_item("c5", status="fyi"),          # extra active status — still counts
        _make_item("c6", status="sent"),         # terminal — excluded
        _make_item("c7", status="dismissed"),    # terminal — excluded
        _make_item("c8", status=""),             # empty status — counts
    ])

    # Compute the expected count straight from the full queue the UI consumes.
    items = (await (await client.get("/api/slack/queue")).json())["items"]
    expected = sum(1 for it in items if it.get("status") not in ("sent", "dismissed"))
    assert expected == 6  # c1..c5 + c8

    resp = await client.get("/api/slack/queue/count")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"count": expected}
    assert isinstance(body["count"], int)
    # The lightweight endpoint carries NO item payloads.
    assert "items" not in body


async def test_slack_queue_count_empty_is_zero(client, slack_file):
    """With no sidecar at all, the count endpoint returns {count: 0} (not 500)."""
    resp = await client.get("/api/slack/queue/count")
    assert resp.status == 200
    assert await resp.json() == {"count": 0}


async def test_slack_count_helper_pins_terminal_denylist():
    """Unit-pin the helper directly: only 'sent'/'dismissed' are terminal.

    Guards the constant + helper without going through HTTP, so a future edit
    that swaps the terminal tuple (e.g. back to email's 'approved') fails here.
    """
    assert slack_mod._TERMINAL_STATUSES == ("sent", "dismissed")

    items = [
        {"status": "needs-classify"},
        {"status": "needs-draft"},
        {"status": "needs-review"},
        {"status": "edited"},
        {"status": "fyi"},
        {"status": ""},
        {},                       # missing status — counts
        {"status": "sent"},       # terminal
        {"status": "dismissed"},  # terminal
    ]
    assert slack_mod._count_actionable(items) == 7

    # Degrades gracefully on junk inputs (never raises).
    assert slack_mod._count_actionable(None) == 0
    assert slack_mod._count_actionable("nope") == 0
    assert slack_mod._count_actionable([None, 1, "x", {"status": "edited"}]) == 1
