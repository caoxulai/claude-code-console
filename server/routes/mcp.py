"""CRUD /api/mcp — manage MCP server configurations."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from server import filestore


# The standard location for a user's CLI-scoped MCP servers is the top-level
# `mcpServers` object in ~/.claude.json. A legacy MeshClaw agent config
# (~/.claude/agents/meshclaw.mcp.json) is used as a fallback for installs that
# predate the standard location — but the standard file always wins so the
# console reflects the servers the `claude` CLI actually uses.
GLOBAL_STATE_PATH = Path.home() / ".claude.json"
LEGACY_AGENT_MCP_PATH = Path.home() / ".claude" / "agents" / "meshclaw.mcp.json"


def register(app: web.Application):
    app.router.add_get("/api/mcp", list_mcp)
    app.router.add_put("/api/mcp/{name}", update_mcp)
    app.router.add_post("/api/mcp", add_mcp)
    app.router.add_delete("/api/mcp/{name}", delete_mcp)


def _mcp_config_path() -> Path:
    """Resolve the file backing MCP server config.

    Prefers the standard ~/.claude.json (where the CLI stores mcpServers).
    Falls back to the legacy MeshClaw agent file only when ~/.claude.json is
    absent but the legacy file exists, so existing installs keep working.
    """
    if GLOBAL_STATE_PATH.is_file():
        return GLOBAL_STATE_PATH
    if LEGACY_AGENT_MCP_PATH.is_file():
        return LEGACY_AGENT_MCP_PATH
    return GLOBAL_STATE_PATH


# env keys whose values are masked in GET responses (and therefore must be
# treated as "keep existing" on write — see _unmask_env).
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
_MASK = "***"


def _is_secret_key(key: str) -> bool:
    return any(hint in key.upper() for hint in _SECRET_HINTS)


def _load_all_servers() -> tuple[dict, str | None]:
    """Load MCP servers from the active config file."""
    data, etag = filestore.read_json(_mcp_config_path())
    servers = data.get("mcpServers", {})
    return servers, etag


def _unmask_env(new_config: dict, existing_config: dict) -> dict:
    """Restore masked secret env values from the existing on-disk config.

    GET masks secret env values to "***". The edit form round-trips those
    masked values, so a naive write would overwrite the real secret on disk
    with "***" and break the server. Whenever an incoming env value is exactly
    the mask, we substitute the value currently stored for that key. Done
    server-side so it holds regardless of which client issued the write.
    """
    new_env = new_config.get("env")
    if not isinstance(new_env, dict):
        return new_config
    existing_env = existing_config.get("env", {}) if isinstance(existing_config, dict) else {}
    merged = dict(new_env)
    for k, v in new_env.items():
        if v == _MASK and _is_secret_key(k) and k in existing_env:
            merged[k] = existing_env[k]
    result = dict(new_config)
    result["env"] = merged
    return result


async def list_mcp(request: web.Request) -> web.Response:
    servers, etag = _load_all_servers()
    result = []
    for name, config in servers.items():
        result.append({
            "name": name,
            "command": config.get("command", ""),
            "args": config.get("args", []),
            "type": config.get("type", "stdio"),
            "env": {k: _MASK if _is_secret_key(k) else v
                    for k, v in config.get("env", {}).items()},
        })
    return web.json_response({"servers": result, "etag": etag})


async def update_mcp(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json()
    expected_etag = body.get("etag")
    config = body.get("config", {})

    path = _mcp_config_path()
    data, current_etag = filestore.read_json(path)
    servers = data.get("mcpServers", {})
    if name not in servers:
        raise web.HTTPNotFound(reason=f"MCP server {name} not found")

    # Don't let a masked secret ("***") round-tripped from the edit form clobber
    # the real value stored on disk.
    config = _unmask_env(config, servers[name])
    servers[name] = config
    data["mcpServers"] = servers

    try:
        new_etag = filestore.write_json(path, data, expected_etag)
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

    path = _mcp_config_path()
    data, current_etag = filestore.read_json(path)
    servers = data.get("mcpServers", {})
    if name in servers:
        raise web.HTTPConflict(reason=f"MCP server {name} already exists")

    servers[name] = config
    data["mcpServers"] = servers

    try:
        new_etag = filestore.write_json(path, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag}, status=201)


async def delete_mcp(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json()
    expected_etag = body.get("etag")

    path = _mcp_config_path()
    data, current_etag = filestore.read_json(path)
    servers = data.get("mcpServers", {})
    if name not in servers:
        raise web.HTTPNotFound(reason=f"MCP server {name} not found")

    del servers[name]
    data["mcpServers"] = servers

    try:
        new_etag = filestore.write_json(path, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_deleted", {"name": name})
    return web.json_response({"deleted": name, "etag": new_etag})
