"""aiohttp application factory for Claude Code Console."""
from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from server.cli import resolve_permission_mode
from server.ws import WebSocketManager
from server.routes import chat, sessions, settings, memory, skills, hooks, mcp, crons, oscron, tasks, plugins, usage, agents, slack


def _resolve_frontend_dist() -> Path:
    """Find the frontend dist directory (works both in dev and installed mode)."""
    # Dev mode: frontend/dist lives next to the server/ package
    dev_path = Path(__file__).parent.parent / "frontend" / "dist"
    if dev_path.is_dir():
        return dev_path
    # Installed mode: static/ is bundled inside the server package
    installed_path = Path(__file__).parent / "static"
    if installed_path.is_dir():
        return installed_path
    return dev_path  # fallback (will just fail gracefully in create_app)


FRONTEND_DIST = _resolve_frontend_dist()

ALLOWED_CWD_ROOTS = [
    Path.home(),
]


def create_app() -> web.Application:
    app = web.Application()

    # Shared state
    app["ws_manager"] = WebSocketManager()
    app["default_cwd"] = Path(
        os.environ.get("CLAUDE_WEB_CWD", str(Path.home()))
    ).expanduser().resolve()
    app["allowed_cwd_roots"] = ALLOWED_CWD_ROOTS
    # Resolve the permission mode once at startup so chat.py can read it
    # per-request without re-parsing config (env > config.json > bypassPermissions).
    app["permission_mode"] = resolve_permission_mode()

    # WebSocket
    app.router.add_get("/ws", app["ws_manager"].handle)

    # API routes
    chat.register(app)
    sessions.register(app)
    settings.register(app)
    memory.register(app)
    skills.register(app)
    hooks.register(app)
    mcp.register(app)
    crons.register(app)
    oscron.register(app)
    tasks.register(app)
    plugins.register(app)
    usage.register(app)
    agents.register(app)
    slack.register(app)

    # Health check
    app.router.add_get("/healthz", _healthz)

    # SPA fallback: serve frontend dist, fall back to index.html for client routes
    if FRONTEND_DIST.is_dir():
        app.router.add_static("/assets", FRONTEND_DIST / "assets", show_index=False)
        app.router.add_get("/{path:.*}", _spa_handler)

    return app


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _spa_handler(_request: web.Request) -> web.Response:
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return web.FileResponse(index)
    return web.Response(status=404, text="frontend not built")
