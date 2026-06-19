"""POST /api/chat — persistent session mode with streaming SSE."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web

from server.cli import load_config
from server.routes import read_json_body

from server.session_manager import SessionManager


PROJECTS_BASE = Path.home() / ".claude" / "projects"

# Valid --permission-mode tokens accepted by the installed claude CLI. Anything
# outside this set must NOT be passed through (the CLI rejects it), so we fall
# back to a safe default instead.
_VALID_PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)
# Web sessions run unattended (no terminal to approve prompts), so default to
# bypassPermissions rather than the CLI's interactive "default".
_DEFAULT_PERMISSION_MODE = "bypassPermissions"


def _resolve_permission_mode(app: web.Application) -> str:
    """Resolve the configured permission mode, validated against the CLI tokens.

    Prefers a value already resolved onto the app (populated at startup);
    otherwise reads CLAUDE_WEB_PERMISSION_MODE / config.json's permissionMode.
    Invalid or missing values fall back to bypassPermissions so we never hand
    the CLI a token it would reject. Kept overridable via app state and env so
    tests can pin a mode without touching the process environment.
    """
    mode = app.get("permission_mode")
    if mode is None:
        mode = os.environ.get("CLAUDE_WEB_PERMISSION_MODE") or load_config().get("permissionMode")
    if mode in _VALID_PERMISSION_MODES:
        return mode
    return _DEFAULT_PERMISSION_MODE


def register(app: web.Application):
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_post("/api/chat/stop", stop_session_handler)
    app.router.add_post("/api/chat/restart", restart_session_handler)

    # Create session manager and attach to app
    if "session_manager" not in app:
        app["session_manager"] = SessionManager()

    # Cleanup on shutdown
    app.on_shutdown.append(_on_shutdown)


async def _on_shutdown(app: web.Application):
    if "session_manager" in app:
        await app["session_manager"].stop_all()


def _resolve_cwd(raw: str | None, app: web.Application) -> str:
    default = app["default_cwd"]
    if not raw:
        return str(default)
    target = Path(raw).expanduser().resolve()
    if not target.is_dir():
        raise web.HTTPBadRequest(reason=f"cwd not a directory: {target}")
    for root in app["allowed_cwd_roots"]:
        try:
            target.relative_to(root.resolve())
            return str(target)
        except ValueError:
            continue
    raise web.HTTPForbidden(reason=f"cwd not under an allowlisted root: {target}")


async def chat_handler(request: web.Request) -> web.StreamResponse:
    body = await read_json_body(request)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise web.HTTPBadRequest(reason="prompt required")

    cwd = _resolve_cwd(body.get("cwd"), request.app)
    resume = body.get("resume")
    # Reject path-traversal in a client-supplied resume id before it reaches
    # `--resume <id>` (mirrors sessions._validate_session_id; defense in depth).
    if resume and ("/" in resume or "\\" in resume or ".." in resume):
        raise web.HTTPBadRequest(reason="invalid session id")
    manager: SessionManager = request.app["session_manager"]

    # Get or create a persistent session
    # For new sessions, derive a name from the prompt so it shows in `claude --resume`
    session_name = None
    if not resume:
        session_name = prompt[:50].split('\n')[0]

    session = await manager.get_or_create(
        session_id=resume,
        cwd=cwd,
        permission_mode=_resolve_permission_mode(request.app),
        name=session_name,
    )

    # Start SSE response
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    # Hold a reference to the async generator so we can explicitly close it.
    # session.send() holds the session lock across its yield loop; if the client
    # disconnects mid-stream the loop is abandoned and the suspended generator
    # would only release the lock on lazy GC finalization. A fast-following
    # request that reuses the session could then block forever on the still-held
    # lock (asyncio.Lock has no acquire timeout). aclose() in finally releases it
    # deterministically the moment the client goes away.
    gen = session.send(prompt)
    try:
        async for evt in gen:
            line = json.dumps(evt)
            await resp.write(f"data: {line}\n\n".encode("utf-8"))

            # Register session once we know its ID
            if evt.get("type") == "system" and evt.get("session_id"):
                manager.register(session)

        await resp.write(b"data: [DONE]\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except RuntimeError as e:
        await resp.write(f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
    finally:
        await gen.aclose()

    # For new sessions: mark as interactive so it appears in both CLI and GUI session lists
    if not resume and session.session_id:
        _mark_session_interactive(session.session_id, prompt[:50].split('\n')[0])

    return resp


async def stop_session_handler(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    session_id = body.get("session_id")
    if not session_id:
        raise web.HTTPBadRequest(reason="session_id required")
    manager: SessionManager = request.app["session_manager"]
    await manager.stop(session_id)
    return web.json_response({"stopped": session_id})


async def restart_session_handler(request: web.Request) -> web.Response:
    """Respawn a session's claude subprocess to pick up refreshed credentials.

    This is the server-side half of auth-error recovery: after the user
    re-runs mwinit, the long-lived process still holds stale creds, so we tear
    it down and start a fresh one (resuming the same session). The next /api/chat
    call then runs against the new process. Scoped to one session.
    """
    body = await read_json_body(request)
    session_id = body.get("session_id")
    if not session_id:
        raise web.HTTPBadRequest(reason="session_id required")
    cwd = _resolve_cwd(body.get("cwd"), request.app)
    manager: SessionManager = request.app["session_manager"]
    # Use the same resolved mode as the initial spawn so a credential-recovered
    # session keeps its write ability instead of reverting to "default".
    await manager.respawn(
        session_id,
        cwd=cwd,
        permission_mode=_resolve_permission_mode(request.app),
    )
    return web.json_response({"restarted": session_id})


def _mark_session_interactive(session_id: str, title: str) -> None:
    """Prepend a mode entry to make the session visible in claude --resume, then add a title."""
    for d in PROJECTS_BASE.iterdir():
        if not d.is_dir():
            continue
        path = d / f"{session_id}.jsonl"
        if path.exists():
            # Read existing content
            content = path.read_text()
            # Prepend mode entry (makes it pass the interactive check)
            mode_entry = json.dumps({"type": "mode", "mode": "normal"}) + "\n"
            title_entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id}) + "\n"
            path.write_text(mode_entry + title_entry + content)
            return
