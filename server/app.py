"""aiohttp application factory for Claude Code Console."""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from server.config import cfg
from server.ws import WebSocketManager
from server.routes import chat, sessions, settings, memory, skills, hooks, mcp, crons, oscron, tasks, plugins, usage, agents, slack, email, workers

logger = logging.getLogger(__name__)

# Dev mode: frontend/dist lives next to the server/ package.
_DEV_DIST = Path(__file__).parent.parent / "frontend" / "dist"
# Installed mode: static/ is bundled inside the server package as package data.
_PACKAGED_DIST = Path(__file__).parent / "static"


def _resolve_frontend_dist() -> Path:
    """Find the frontend dist directory (works both in dev and installed mode)."""
    if _DEV_DIST.is_dir():
        return _DEV_DIST
    if _PACKAGED_DIST.is_dir():
        return _PACKAGED_DIST
    return _DEV_DIST  # fallback (will just fail gracefully in create_app)


FRONTEND_DIST = _resolve_frontend_dist()


def _frontend_dist_label(dist: Path) -> str:
    """Name the branch that produced `dist`, for the startup log line."""
    if dist == _DEV_DIST:
        return "dev frontend/dist"
    if dist == _PACKAGED_DIST:
        return "packaged server/static"
    return "custom path"


ALLOWED_CWD_ROOTS = [
    Path.home(),
]


def create_app() -> web.Application:
    app = web.Application(middlewares=[_immutable_assets])

    # Shared state
    app["ws_manager"] = WebSocketManager()
    app["default_cwd"] = cfg.default_cwd
    app["allowed_cwd_roots"] = ALLOWED_CWD_ROOTS
    # Resolve the permission mode once at startup so chat.py can read it
    # per-request without re-parsing config (env > config.json 'permissionMode' >
    # bypassPermissions). Imported function-locally: cli.py lazily imports
    # create_app inside cmd_start, so a module-level import here would cycle.
    from server.cli import resolve_permission_mode

    app["permission_mode"] = resolve_permission_mode()

    # WebSocket
    app.router.add_get("/ws", app["ws_manager"].handle)

    # API routes — workers registry FIRST so app['_worker_registry'] exists
    # before other modules call register_worker() during their register().
    workers.register(app)
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
    email.register(app)

    # Health check
    app.router.add_get("/healthz", _healthz)

    # SPA fallback: serve frontend dist, fall back to index.html for client routes.
    # One line naming the dist dir that actually won, so a stale or absent build is
    # visible in the log instead of silently serving the wrong (or no) UI.
    serving = FRONTEND_DIST.is_dir()
    logger.info(
        "Frontend dist: %s (%s)",
        FRONTEND_DIST,
        _frontend_dist_label(FRONTEND_DIST) if serving else "missing — UI not served",
    )
    if serving:
        app.router.add_static("/assets", FRONTEND_DIST / "assets", show_index=False)
        app.router.add_get("/{path:.*}", _spa_handler)

    return app


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _spa_handler(request: web.Request) -> web.Response:
    # An unmatched /api/ path is a backend miss, never a client route: answering it
    # with the SPA shell (HTTP 200 + index.html) makes a typo or a removed endpoint
    # look like a successful request to every fetch() caller. The guard lives here
    # rather than as an /api/{path:.*} route on purpose — a route-level catch-all
    # would turn POSTs to removed endpoints from 405 into 404.
    if request.path.startswith("/api/"):
        return web.json_response(
            {"error": "not found", "path": request.path}, status=404
        )
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return web.FileResponse(index)
    return web.Response(status=404, text="frontend not built")


@web.middleware
async def _immutable_assets(request: web.Request, handler):
    # Vite emits content-hashed asset filenames (e.g. index-C1GLvghA.js) under
    # /assets/, which are safe to cache forever. The /assets/ prefix guard is the
    # entire safety mechanism: it keeps the un-hashed SPA shell (index.html, served
    # by _spa_handler from / and arbitrary client-route paths) revalidated, so a new
    # deploy isn't stranded on a stale shell pointing at deleted bundles (ADR D-034).
    resp = await handler(request)
    if request.path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp
