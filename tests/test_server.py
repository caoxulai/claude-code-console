"""Smoke + API-shape tests for claude-web. claude is mocked."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from server.app import create_app, ALLOWED_CWD_ROOTS  # noqa: E402


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    # Patch ALLOWED_CWD_ROOTS in server.app so the test client can pass cwd values
    # without hitting the production allowlist.
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path, Path("/etc/__never__")])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    # Also patch the runtime copy stored in app state
    application["allowed_cwd_roots"] = [tmp_path, Path("/etc/__never__")]
    application["default_cwd"] = tmp_path
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_healthz_is_public(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}


async def test_chat_rejects_empty_prompt(client):
    resp = await client.post("/api/chat", json={"prompt": "   "})
    assert resp.status == 400


async def test_chat_rejects_cwd_outside_allowlist(client):
    # "/" is not under the allowlist (which is just tmp_path in this test setup).
    resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": "/"})
    assert resp.status == 403


async def test_chat_streams_sse(client, tmp_path):
    fake_events = [
        {"type": "system", "session_id": "abc"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "result", "is_error": False, "result": "hello"},
    ]

    async def fake_send(prompt):
        for evt in fake_events:
            yield evt

    # Mock the session manager to return a fake session
    fake_session = AsyncMock()
    fake_session.session_id = "abc"
    fake_session.send = fake_send

    fake_manager = AsyncMock()
    fake_manager.get_or_create = AsyncMock(return_value=fake_session)
    fake_manager.register = lambda s: None

    with patch.dict(client.app, {"session_manager": fake_manager}):
        resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": str(tmp_path)})
    assert resp.status == 200
    body = await resp.text()
    # Should be a sequence of SSE frames terminated by [DONE].
    assert "data: [DONE]" in body
    assert '"type":"system"' in body or '"type": "system"' in body
    assert '"type":"assistant"' in body or '"type": "assistant"' in body
