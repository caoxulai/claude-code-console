"""CRUD /api/hooks — manage hooks in settings.json."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web

from server import filestore


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def register(app: web.Application):
    app.router.add_get("/api/hooks", get_hooks)
    app.router.add_put("/api/hooks", put_hooks)
    app.router.add_post("/api/hooks/test", test_hook)


async def get_hooks(request: web.Request) -> web.Response:
    data, etag = filestore.read_json(SETTINGS_PATH)
    hooks = data.get("hooks", {})
    return web.json_response({"hooks": hooks, "etag": etag})


async def put_hooks(request: web.Request) -> web.Response:
    """Replace the entire hooks section in settings.json."""
    body = await request.json()
    new_hooks = body.get("hooks", {})
    expected_etag = body.get("etag")

    data, current_etag = filestore.read_json(SETTINGS_PATH)

    if expected_etag and current_etag and expected_etag != current_etag:
        return web.json_response(
            {"error": "conflict", "hooks": data.get("hooks", {}), "etag": current_etag},
            status=409,
        )

    data["hooks"] = new_hooks
    new_etag = filestore.write_json(SETTINGS_PATH, data, current_etag)

    ws = request.app["ws_manager"]
    await ws.broadcast("hooks_changed", {"etag": new_etag})
    return web.json_response({"hooks": new_hooks, "etag": new_etag})


async def test_hook(request: web.Request) -> web.Response:
    """Execute a hook command with a mock payload to test it."""
    body = await request.json()
    command = body.get("command", "")
    mock_input = body.get("input", "{}")

    if not command:
        raise web.HTTPBadRequest(reason="command required")

    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=mock_input.encode()),
        timeout=10,
    )
    return web.json_response({
        "exit_code": proc.returncode,
        "stdout": stdout.decode(errors="replace")[:5000],
        "stderr": stderr.decode(errors="replace")[:5000],
    })
