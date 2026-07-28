"""CRUD /api/mcp — manage MCP server configurations."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from server.routes import read_json_body

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


# Keys whose values are masked in GET responses (and therefore treated as
# "keep existing" on write — see _unmask_secrets). Applies to both `env`
# (stdio servers) and `headers` (http/sse servers, e.g. Authorization tokens).
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "AUTHORIZATION")
_MASK = "***"
# The sub-objects that can carry secrets, masked/unmasked uniformly.
_SECRET_SECTIONS = ("env", "headers")


def _is_secret_key(key: str) -> bool:
    return any(hint in key.upper() for hint in _SECRET_HINTS)


async def _load_all_servers() -> tuple[dict, str | None]:
    """Load MCP servers from the active config file."""
    data, etag = await filestore.async_read_json(_mcp_config_path())
    servers = data.get("mcpServers", {})
    return servers, etag


def _mask_section(section: dict) -> dict:
    """Mask secret-looking values in an env/headers dict for GET responses."""
    return {k: _MASK if _is_secret_key(k) else v for k, v in section.items()}


def _unmask_secrets(new_config: dict, existing_config: dict) -> dict:
    """Restore masked secret values (env + headers) from the on-disk config.

    GET masks secret values to "***". The edit form round-trips those masked
    values, so a naive write would overwrite the real secret on disk with "***"
    and break the server. Whenever an incoming value is exactly the mask, we
    substitute the value currently stored for that key. Done server-side so it
    holds regardless of which client issued the write. Covers both `env` (stdio)
    and `headers` (http/sse).
    """
    if not isinstance(new_config, dict):
        return new_config
    result = dict(new_config)
    for section in _SECRET_SECTIONS:
        new_section = new_config.get(section)
        if not isinstance(new_section, dict):
            continue
        existing_section = (existing_config.get(section, {})
                             if isinstance(existing_config, dict) else {})
        merged = dict(new_section)
        for k, v in new_section.items():
            if v == _MASK and _is_secret_key(k) and k in existing_section:
                merged[k] = existing_section[k]
        result[section] = merged
    return result


async def list_mcp(request: web.Request) -> web.Response:
    servers, etag = await _load_all_servers()
    result = []
    for name, config in servers.items():
        entry = {
            "name": name,
            "command": config.get("command", ""),
            "args": config.get("args", []),
            "type": config.get("type", "stdio"),
            "env": _mask_section(config.get("env", {}) if isinstance(config.get("env"), dict) else {}),
        }
        # Mask secrets in headers too (http/sse servers carry tokens there).
        headers = config.get("headers")
        if isinstance(headers, dict):
            entry["headers"] = _mask_section(headers)
        result.append(entry)
    return web.json_response({"servers": result, "etag": etag})


async def update_mcp(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")
    config = body.get("config", {})
    if not isinstance(config, dict):
        raise web.HTTPBadRequest(reason="config must be a JSON object")

    path = _mcp_config_path()
    data, current_etag = await filestore.async_read_json(path)
    servers = data.get("mcpServers", {})
    if name not in servers:
        raise web.HTTPNotFound(reason=f"MCP server {name} not found")

    # Don't let a masked secret ("***") round-tripped from the edit form clobber
    # the real value stored on disk (env + headers).
    config = _unmask_secrets(config, servers[name])
    servers[name] = config
    data["mcpServers"] = servers

    try:
        new_etag = await filestore.async_write_json(path, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag})


async def add_mcp(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    name = body.get("name", "").strip()
    config = body.get("config", {})
    expected_etag = body.get("etag")

    if not name:
        raise web.HTTPBadRequest(reason="name required")
    if not isinstance(config, dict):
        raise web.HTTPBadRequest(reason="config must be a JSON object")

    path = _mcp_config_path()
    data, current_etag = await filestore.async_read_json(path)
    servers = data.get("mcpServers", {})
    if name in servers:
        raise web.HTTPConflict(reason=f"MCP server {name} already exists")

    servers[name] = config
    data["mcpServers"] = servers

    try:
        new_etag = await filestore.async_write_json(path, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag}, status=201)


async def delete_mcp(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")

    path = _mcp_config_path()
    data, current_etag = await filestore.async_read_json(path)
    servers = data.get("mcpServers", {})
    if name not in servers:
        raise web.HTTPNotFound(reason=f"MCP server {name} not found")

    del servers[name]
    data["mcpServers"] = servers

    try:
        new_etag = await filestore.async_write_json(path, data, expected_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("mcp_deleted", {"name": name})
    return web.json_response({"deleted": name, "etag": new_etag})
