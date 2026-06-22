"""Route-layer tests for the two latency wirings (T2):

  B1 — get_transcript reads + parses OFF the event loop via asyncio.to_thread
       (the synchronous open()+per-line json.loads was the literal "app hangs"
       on every ClarifyChat resume of a large transcript). We pin the small sync
       helper (parse + malformed-line tolerance) and the unchanged response shape.

  B2/B3 — POST /api/chat/prewarm pre-boots a `claude` subprocess so a fresh
       clarify (manager.prewarm) or a reaped-then-resumed chat
       (manager.get_or_create with the resume id) skips the cold start. It is
       purely additive and best-effort: a failed warm is NEVER user-visible
       (returns {warmed: false}, HTTP 200) and the cold /api/chat path still works.

These drive the real route registration via aiohttp_client(create_app()); the
session manager is replaced with an AsyncMock so no real `claude` is spawned.
A dedicated module (not the test_server.py monolith, not T3's
test_session_streaming.py) to avoid the concurrent-edit hazard on those files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from server.app import create_app  # noqa: E402
import server.routes.sessions as sessions_mod  # noqa: E402


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
    """Reset Slack/email lazily-created loop-bound asyncio primitives between tests.

    create_app() starts BOTH the slack and email workers; their Events/Locks are
    lazily bound to the first loop and otherwise leak across tests, raising
    "bound to a different event loop" at the NEXT test's teardown.
    """
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


# --------------------------------------------------------------------------- #
# B1 — get_transcript off the event loop
# --------------------------------------------------------------------------- #
def test_read_transcript_records_parses_and_tolerates_malformed(tmp_path):
    """The sync helper parses every valid JSONL line and SKIPS a malformed one,
    matching the previous inline `except json.JSONDecodeError: continue` exactly.
    """
    p = tmp_path / "sess.jsonl"
    p.write_text(
        json.dumps({"type": "system", "session_id": "s1"}) + "\n"
        + "{not valid json\n"  # malformed → skipped, never aborts the read
        + json.dumps({"type": "result", "result": "ok"}) + "\n"
    )

    records = sessions_mod._read_transcript_records(p)

    assert records == [
        {"type": "system", "session_id": "s1"},
        {"type": "result", "result": "ok"},
    ]


async def test_get_transcript_returns_unchanged_shape(client, tmp_path, monkeypatch):
    """The endpoint response shape is byte-identical after the to_thread move:
    {messages, offset, limit, total} with the tail slice intact.
    """
    proj = tmp_path / "proj-a"
    proj.mkdir()
    sid = "11111111-2222-3333-4444-555555555555"
    lines = [json.dumps({"type": "assistant", "i": i}) for i in range(5)]
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")

    # Point the endpoint's project-dir scan at our temp tree.
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", tmp_path)

    resp = await client.get(f"/api/sessions/{sid}/transcript?limit=2&offset=0&tail=true")
    assert resp.status == 200
    data = await resp.json()
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0
    # tail=true with limit=2 → the LAST two records.
    assert data["messages"] == [{"type": "assistant", "i": 3}, {"type": "assistant", "i": 4}]


async def test_get_transcript_404_when_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", tmp_path)
    resp = await client.get(
        "/api/sessions/00000000-0000-0000-0000-000000000000/transcript")
    assert resp.status == 404


# --------------------------------------------------------------------------- #
# B2/B3 — POST /api/chat/prewarm
# --------------------------------------------------------------------------- #
async def test_prewarm_fresh_clarify_calls_manager_prewarm(client, tmp_path):
    """No resume id → warm a spare for the fresh-clarify flow via manager.prewarm,
    keyed by the resolved cwd + permission mode. Returns {warmed: true}.
    """
    fake_manager = AsyncMock()
    fake_manager.prewarm = AsyncMock(return_value=None)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(client.app, "session_manager", fake_manager)
        resp = await client.post("/api/chat/prewarm", json={"cwd": str(tmp_path)})

    assert resp.status == 200
    assert await resp.json() == {"warmed": True}
    fake_manager.prewarm.assert_awaited_once()
    args, _ = fake_manager.prewarm.await_args
    assert args[0] == str(tmp_path)
    assert args[1] == "bypassPermissions"
    fake_manager.get_or_create.assert_not_awaited()


async def test_prewarm_resume_lands_live_resume_process(client, tmp_path):
    """A resume id → land the live --resume process in _sessions now (via
    get_or_create, idempotent for a live id) so the first real send reuses it.
    Resume is cwd-scoped, so it runs in the resolved (original) cwd.
    """
    fake_manager = AsyncMock()
    fake_manager.get_or_create = AsyncMock(return_value=AsyncMock())

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(client.app, "session_manager", fake_manager)
        resp = await client.post(
            "/api/chat/prewarm",
            json={"resume": "sess-abc", "cwd": str(tmp_path)},
        )

    assert resp.status == 200
    assert await resp.json() == {"warmed": True}
    fake_manager.get_or_create.assert_awaited_once()
    _, kwargs = fake_manager.get_or_create.await_args
    assert kwargs["session_id"] == "sess-abc"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["permission_mode"] == "bypassPermissions"
    fake_manager.prewarm.assert_not_awaited()


async def test_prewarm_resume_rejects_path_traversal(client, tmp_path):
    """A resume id with a path-traversal token is rejected before it can reach
    `--resume <id>` (mirrors chat_handler's guard).
    """
    fake_manager = AsyncMock()
    for bad in ("../etc", "a/b", "a\\b", ".."):
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(client.app, "session_manager", fake_manager)
            resp = await client.post(
                "/api/chat/prewarm",
                json={"resume": bad, "cwd": str(tmp_path)},
            )
        assert resp.status == 400, f"expected 400 for resume id {bad!r}"
    fake_manager.get_or_create.assert_not_awaited()


async def test_prewarm_failure_is_never_user_visible(client, tmp_path):
    """Best-effort: a warm that RAISES must not 500 — the handler swallows it and
    returns {warmed: false} (HTTP 200) so the cold /api/chat path still works.
    """
    fake_manager = AsyncMock()
    fake_manager.prewarm = AsyncMock(side_effect=RuntimeError("boot failed"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(client.app, "session_manager", fake_manager)
        resp = await client.post("/api/chat/prewarm", json={"cwd": str(tmp_path)})

    assert resp.status == 200
    assert await resp.json() == {"warmed": False}


async def test_prewarm_rejects_cwd_outside_allowlist(client):
    """cwd resolution reuses _resolve_cwd, so an out-of-allowlist cwd is 403 (the
    guard fires before any warm)."""
    resp = await client.post("/api/chat/prewarm", json={"cwd": "/"})
    assert resp.status == 403
