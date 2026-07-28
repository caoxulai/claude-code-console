"""Startup-wiring tests for server/app.py (D-078 clauses 1-3, roadmap items 20/23/27e).

Three behaviours that all live in create_app()/_spa_handler:

  1. permissionMode is WIRED into the live path. The README documents
     ~/.claude-web/config.json's "permissionMode" key and cli.resolve_permission_mode()
     has always implemented env > config-file > bypassPermissions — but nothing in
     the startup path ever called it, so the documented config key was dead. These
     tests observe the mode at the SPAWN seam (manager.get_or_create), not at the
     resolver's return value: a green resolver unit test is not proof the feature
     works.

  2. create_app() logs exactly ONE line naming the frontend dist directory it is
     actually serving (dev frontend/dist vs packaged server/static vs missing), so
     a stale/absent dist is visible instead of silently serving the wrong UI.

  3. Unknown /api/... paths return a JSON 404 instead of falling through the SPA
     catch-all and answering 200 + index.html. Client routes (/sessions, /) still
     get the SPA shell. The guard lives INSIDE _spa_handler, not as a separate
     /api/{path:.*} route, so POST /api/tasks/dismiss stays a 405 (see the pin in
     tests/test_server.py::test_tasks_dismissed_endpoints_removed).

Every test is hermetic: the config file is a tmp_path file (never the real
~/.claude-web/config.json), the frontend dist is a tmp_path directory, and the
suite-wide autouse hermeticity trap in tests/conftest.py applies unchanged.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
import server.app as app_mod  # noqa: E402
import server.cli as cli_mod  # noqa: E402


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


def _fake_dist(tmp_path: Path) -> Path:
    """A stand-in frontend dist dir (index.html + assets/), so the SPA routes
    register without depending on a real `npm run build` in this checkout."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>claude-web</title>", encoding="utf-8")
    return dist


def _point_config_at(tmp_path: Path, monkeypatch, payload: dict | None) -> Path:
    """Point cli.load_config() at a tmp config.json (or a missing path).

    cli.CONFIG_PATH is bound from cfg.config_path at import time, so both the
    cfg field and the module constant get patched (mirrors the helper used by
    the resolver's own tests in tests/test_server.py).
    """
    config_path = tmp_path / "config.json"
    if payload is None:
        config_path = tmp_path / "does-not-exist.json"
    else:
        config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("server.config.cfg.config_path", config_path)
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", config_path)
    return config_path


def _make_app(
    tmp_path: Path,
    monkeypatch,
    *,
    config: dict | None = None,
    env_mode: str = "bypassPermissions",
    dist: Path | None = None,
) -> web.Application:
    """Build a real app the way the production startup path does.

    `env_mode` is what cfg resolved from CLAUDE_WEB_PERMISSION_MODE (cfg is frozen
    at import, so the env var itself is patched onto the cfg field);
    'bypassPermissions' means "no env override set".
    """
    _point_config_at(tmp_path, monkeypatch, config)
    monkeypatch.setattr("server.config.cfg.permission_mode", env_mode)
    monkeypatch.setattr(app_mod, "FRONTEND_DIST", dist if dist else _fake_dist(tmp_path))
    monkeypatch.setattr(app_mod, "ALLOWED_CWD_ROOTS", [tmp_path])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = app_mod.create_app()
    application["allowed_cwd_roots"] = [tmp_path]
    application["default_cwd"] = tmp_path
    return application


# --------------------------------------------------------------------------- #
# Item 20 — permissionMode wired into startup (env > config.json > default)
# --------------------------------------------------------------------------- #
def test_startup_uses_config_permission_mode_when_no_env(tmp_path, monkeypatch):
    """No env override + config.json {"permissionMode": "plan"} -> app state 'plan'.

    This is the documented-but-unwired case: create_app used to read cfg.permission_mode
    directly (env only), so the config key was silently ignored.
    """
    application = _make_app(tmp_path, monkeypatch, config={"permissionMode": "plan"})
    assert application["permission_mode"] == "plan"


def test_startup_env_beats_config_permission_mode(tmp_path, monkeypatch):
    """Locked precedence: the env override wins over the config file."""
    application = _make_app(
        tmp_path, monkeypatch, config={"permissionMode": "plan"}, env_mode="acceptEdits"
    )
    assert application["permission_mode"] == "acceptEdits"


def test_startup_falls_back_to_default_permission_mode(tmp_path, monkeypatch):
    """Neither env nor config -> the safe default, bypassPermissions."""
    application = _make_app(tmp_path, monkeypatch, config=None)
    assert application["permission_mode"] == "bypassPermissions"


async def test_config_permission_mode_reaches_session_spawn(
    aiohttp_client, tmp_path, monkeypatch
):
    """END-TO-END at the SPAWN seam: with only config.json setting "plan", a POST
    /api/chat creates the session with permission_mode='plan'.

    Observing app['permission_mode'] alone would not prove the mode reaches the
    session manager, which is the behaviour the user actually asked for.
    """
    application = _make_app(tmp_path, monkeypatch, config={"permissionMode": "plan"})

    async def fake_send(prompt):
        yield {"type": "system", "session_id": "spawn-seam-1"}
        yield {"type": "result", "is_error": False, "result": "ok"}

    fake_session = AsyncMock()
    fake_session.session_id = "spawn-seam-1"
    fake_session.send = fake_send

    fake_manager = AsyncMock()
    fake_manager.get_or_create = AsyncMock(return_value=fake_session)
    fake_manager.register = lambda s: None
    application["session_manager"] = fake_manager

    client = await aiohttp_client(application)
    resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": str(tmp_path)})
    assert resp.status == 200
    await resp.text()  # drain the SSE stream so the handler runs to completion

    _, kwargs = fake_manager.get_or_create.await_args
    assert kwargs["permission_mode"] == "plan"


async def test_env_permission_mode_reaches_session_spawn(
    aiohttp_client, tmp_path, monkeypatch
):
    """Same seam, precedence half: env acceptEdits + config plan -> acceptEdits
    is what the session is spawned with."""
    application = _make_app(
        tmp_path, monkeypatch, config={"permissionMode": "plan"}, env_mode="acceptEdits"
    )

    async def fake_send(prompt):
        yield {"type": "system", "session_id": "spawn-seam-2"}
        yield {"type": "result", "is_error": False, "result": "ok"}

    fake_session = AsyncMock()
    fake_session.session_id = "spawn-seam-2"
    fake_session.send = fake_send

    fake_manager = AsyncMock()
    fake_manager.get_or_create = AsyncMock(return_value=fake_session)
    fake_manager.register = lambda s: None
    application["session_manager"] = fake_manager

    client = await aiohttp_client(application)
    resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": str(tmp_path)})
    assert resp.status == 200
    await resp.text()

    _, kwargs = fake_manager.get_or_create.await_args
    assert kwargs["permission_mode"] == "acceptEdits"


def test_startup_rejects_garbage_config_permission_mode(tmp_path, monkeypatch):
    """A garbage config value must never reach `claude --permission-mode`; the
    resolver's validation is inherited unchanged by the startup wiring."""
    application = _make_app(tmp_path, monkeypatch, config={"permissionMode": "yolo-mode"})
    assert application["permission_mode"] == "bypassPermissions"


# --------------------------------------------------------------------------- #
# Item 23 (log half) — one startup line naming the dist dir actually served
# --------------------------------------------------------------------------- #
def test_create_app_logs_the_frontend_dist_it_serves(tmp_path, monkeypatch, caplog):
    """Exactly one server.app INFO line, naming the resolved dist directory, so a
    stale/absent dist is visible in the log instead of silently mis-serving."""
    dist = _fake_dist(tmp_path)
    with caplog.at_level(logging.INFO, logger="server.app"):
        _make_app(tmp_path, monkeypatch, config=None, dist=dist)

    lines = [r.getMessage() for r in caplog.records if r.name == "server.app"]
    assert len(lines) == 1, lines
    assert str(dist) in lines[0]


def test_create_app_logs_missing_frontend_dist(tmp_path, monkeypatch, caplog):
    """With no dist dir at all the single line says so (the packaged-without-UI
    failure mode is otherwise silent)."""
    missing = tmp_path / "nope-dist"
    with caplog.at_level(logging.INFO, logger="server.app"):
        _make_app(tmp_path, monkeypatch, config=None, dist=missing)

    lines = [r.getMessage() for r in caplog.records if r.name == "server.app"]
    assert len(lines) == 1, lines
    assert str(missing) in lines[0]
    assert "missing" in lines[0].lower()


# --------------------------------------------------------------------------- #
# Item 27e — unknown /api/ paths are a JSON 404, client routes keep the shell
# --------------------------------------------------------------------------- #
async def test_unknown_api_path_is_json_404(aiohttp_client, tmp_path, monkeypatch):
    application = _make_app(tmp_path, monkeypatch, config=None)
    client = await aiohttp_client(application)

    resp = await client.get("/api/definitely-not-a-route")
    assert resp.status == 404
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["error"] == "not found"
    assert body["path"] == "/api/definitely-not-a-route"


async def test_unknown_nested_api_path_is_json_404(aiohttp_client, tmp_path, monkeypatch):
    """The guard covers nested unknown API paths too (not just first-segment ones)."""
    application = _make_app(tmp_path, monkeypatch, config=None)
    client = await aiohttp_client(application)

    resp = await client.get("/api/sessions/nope/not-a-subresource")
    assert resp.status == 404
    assert resp.content_type == "application/json"


async def test_client_routes_still_serve_the_spa_shell(aiohttp_client, tmp_path, monkeypatch):
    """/sessions and / are React-router client routes: they must keep returning
    the SPA shell, or the JSON-404 guard has been made too broad."""
    dist = _fake_dist(tmp_path)
    application = _make_app(tmp_path, monkeypatch, config=None, dist=dist)
    client = await aiohttp_client(application)

    for path in ("/", "/sessions", "/projects/alpha"):
        resp = await client.get(path)
        assert resp.status == 200, path
        assert "claude-web" in (await resp.text()), path


async def test_unknown_api_post_stays_405(aiohttp_client, tmp_path, monkeypatch):
    """The guard lives inside _spa_handler, NOT as a /api/{path:.*} route — so a
    POST to a removed endpoint still surfaces 405 (the SPA catch-all only handles
    GET). Pins the same contract as test_tasks_dismissed_endpoints_removed.
    """
    application = _make_app(tmp_path, monkeypatch, config=None)
    client = await aiohttp_client(application)

    resp = await client.post("/api/tasks/dismiss", json={"taskId": "1"})
    assert resp.status == 405
