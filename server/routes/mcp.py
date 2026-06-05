"""CRUD /api/mcp — manage MCP server configurations."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from server import filestore


# MCP servers can be in multiple places; we read from the agent config
AGENT_MCP_PATH = Path.home() / ".claude" / "agents" / "meshclaw.mcp.json"
GLOBAL_STATE_PATH = Path.home() / ".claude.json"


def register(app: web.Application):
    app.router.add_get("/api/mcp", list_mcp)
    app.router.add_put("/api/mcp/{name}", update_mcp)
    app.router.add_post("/api/mcp", add_mcp)
    app.router.add_delete("/api/mcp/{name}", delete_mcp)


def _load_all_servers() -> tuple[dict, str | None]:
    """Load MCP servers from the agent config file."""
    data, etag = filestore.read_json(AGENT_MCP_PATH)
    servers = data.get("mcpServers", {})
    return servers, etag


async def list_mcp(request: web.Request) -> web.Response:
    servers, etag = _load_all_servers()
    result = []
    for name, config in servers.items():
        result.append({
            "name": name,
            "command": config.get("command", ""),
            "args": config.get("args", []),
            "type": config.get("type", "stdio"),
            "env": {k: "***" if any(s in k.upper() for s in ["KEY", "SECRET", "TOKEN", "PASSWORD"]) else v
                    for k, v in config.get("env", {}).items()},
        })
    return web.json_response({"servers": result, "etag": etag})


async def update_mcp(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json()
    expected_etag = body.get("etag")
    config = body.get("config", {})

    data, current_etag = filestore.read_json(AGENT_MCP_PATH)
    servers = data.get("mcpServers", {})
    if name not in servers:
        raise web.HTTPNotFound(reason=f"MCP server {name} not found")

    servers[name] = config
    data["mcpServers"] = servers

    try:
        new_etag = filestore.write_json(AGENT_MCP_PATH, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag})


async def add_mcp(request: web.Request) -> web.Response:
    body = await request.json()
    name = body.get("name", "").strip()
    config = body.get("config", {})
    expected_etag = body.get("etag")

    if not name:
        raise web.HTTPBadRequest(reason="name required")

    data, current_etag = filestore.read_json(AGENT_MCP_PATH)
    servers = data.get("mcpServers", {})
    if name in servers:
        raise web.HTTPConflict(reason=f"MCP server {name} already exists")

    servers[name] = config
    data["mcpServers"] = servers

    try:
        new_etag = filestore.write_json(AGENT_MCP_PATH, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag}, status=201)


async def delete_mcp(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json()
    expected_etag = body.get("etag")

    data, current_etag = filestore.read_json(AGENT_MCP_PATH)
    servers = data.get("mcpServers", {})
    if name not in servers:
        raise web.HTTPNotFound(reason=f"MCP server {name} not found")

    del servers[name]
    data["mcpServers"] = servers

    try:
        new_etag = filestore.write_json(AGENT_MCP_PATH, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_deleted", {"name": name})
    return web.json_response({"deleted": name, "etag": new_etag})
