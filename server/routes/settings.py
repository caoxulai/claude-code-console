"""GET/PUT /api/settings — read/write ~/.claude/settings.json with etag concurrency."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from server.routes import read_json_body

from server import filestore


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
SETTINGS_LOCAL_PATH = Path.home() / ".claude" / "settings.local.json"


def register(app: web.Application):
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", put_settings)
    app.router.add_get("/api/settings/local", get_settings_local)


async def get_settings(request: web.Request) -> web.Response:
    data, etag = await filestore.async_read_json(SETTINGS_PATH)
    return web.json_response({"data": data, "etag": etag})


async def put_settings(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    data = body.get("data")
    expected_etag = body.get("etag")

    if data is None:
        raise web.HTTPBadRequest(reason="data field required")

    try:
        new_etag = await filestore.async_write_json(SETTINGS_PATH, data, expected_etag)
    except filestore.ConflictError as e:
        current_data, current_etag = await filestore.async_read_json(SETTINGS_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current_data, "etag": current_etag},
            status=409,
        )

    # Broadcast change to other connected clients
    ws = request.app["ws_manager"]
    await ws.broadcast("settings_changed", {"etag": new_etag})

    return web.json_response({"data": data, "etag": new_etag})


async def get_settings_local(request: web.Request) -> web.Response:
    data, etag = await filestore.async_read_json(SETTINGS_LOCAL_PATH)
    return web.json_response({"data": data, "etag": etag})
