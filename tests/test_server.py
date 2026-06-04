"""Smoke + auth + SSE-shape tests for claude-web. claude is mocked."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent.parent))
import server  # noqa: E402


SECRET = "test-secret-do-not-use"


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    # Allow tmp paths so the test client can pass cwd values without hitting the
    # production allowlist ($HOME, $HOME/workspace).
    monkeypatch.setattr(server, "ALLOWED_CWD_ROOTS", [tmp_path, Path("/etc/__never__")])
    return server.make_app(SECRET, tmp_path)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture
async def authed_client(aiohttp_client, app):
    # Inject the device cookie directly. /auth sets Secure=True, which the test
    # client (plain HTTP) drops on the round trip, so we mint the cookie value
    # ourselves using the same HMAC the server expects.
    cookies = {server.COOKIE_NAME: server._mint_cookie(SECRET)}
    return await aiohttp_client(app, cookies=cookies)


async def test_healthz_is_public(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}


async def test_chat_requires_auth(client):
    resp = await client.post("/api/chat", json={"prompt": "hi"})
    assert resp.status == 401


async def test_auth_bad_secret(client):
    resp = await client.get("/auth?secret=wrong", allow_redirects=False)
    assert resp.status == 401


async def test_auth_good_secret_sets_cookie(client):
    resp = await client.get(f"/auth?secret={SECRET}", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/"
    assert server.COOKIE_NAME in resp.cookies


async def test_authed_chat_streams_sse(authed_client, tmp_path):
    fake_stream = (
        b'{"type":"system","session_id":"abc"}\n'
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        b'{"type":"result","is_error":false,"result":"hello"}\n'
    )

    class FakeProc:
        def __init__(self):
            self.stdout = self._mk_reader(fake_stream)
            self.stderr = self._mk_reader(b"")

        @staticmethod
        def _mk_reader(data: bytes):
            r = asyncio.StreamReader()
            r.feed_data(data)
            r.feed_eof()
            return r

        async def wait(self):
            return 0

        def terminate(self):
            pass

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        resp = await authed_client.post("/api/chat", json={"prompt": "hi", "cwd": str(tmp_path)})
    assert resp.status == 200
    body = await resp.text()
    # Should be a sequence of SSE frames terminated by [DONE].
    assert "data: [DONE]" in body
    assert '"type":"system"' in body
    assert '"type":"assistant"' in body


async def test_chat_rejects_cwd_outside_allowlist(authed_client):
    # /tmp is not under the allowlist (which is just tmp_path in this test setup,
    # but we pass / directly to verify the rejection path).
    resp = await authed_client.post("/api/chat", json={"prompt": "hi", "cwd": "/"})
    assert resp.status == 403


async def test_chat_rejects_empty_prompt(authed_client):
    resp = await authed_client.post("/api/chat", json={"prompt": "   "})
    assert resp.status == 400
