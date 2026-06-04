"""Single-user web UI for Claude Code.

See README.md and ~/.claude/plans/clever-waddling-mountain.md for the design.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
STATIC_DIR = Path(__file__).parent / "frontend" / "dist"
COOKIE_NAME = "claude_web_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Per the plan: only allow cwds under these roots.
ALLOWED_CWD_ROOTS = [
    Path.home(),
    Path("$HOME/workspace"),
]


def _ensure_env() -> None:
    """Generate .env with a random CLAUDE_WEB_SECRET on first run."""
    if ENV_PATH.exists():
        return
    secret = secrets.token_urlsafe(32)
    ENV_PATH.write_text(
        f"CLAUDE_WEB_SECRET={secret}\n"
        f"CLAUDE_WEB_PORT=7780\n"
        f"CLAUDE_WEB_CWD={Path.home()}\n",
        encoding="utf-8",
    )
    ENV_PATH.chmod(0o600)
    print(f"[claude-web] generated {ENV_PATH} with a fresh secret. chmod 600.")


def _hmac(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _mint_cookie(secret: str) -> str:
    # Token has no payload — possession of the HMAC of the literal "device" string is enough.
    # Rotating CLAUDE_WEB_SECRET invalidates all devices, which is the intended escape hatch.
    return _hmac(secret, "device")


def _cookie_valid(secret: str, cookie: str | None) -> bool:
    if not cookie:
        return False
    return hmac.compare_digest(cookie, _mint_cookie(secret))


def _resolve_cwd(raw: str | None, default: Path) -> Path:
    target = Path(raw).expanduser().resolve() if raw else default
    if not target.is_dir():
        raise web.HTTPBadRequest(reason=f"cwd not a directory: {target}")
    for root in ALLOWED_CWD_ROOTS:
        try:
            target.relative_to(root.resolve())
            return target
        except ValueError:
            continue
    raise web.HTTPForbidden(reason=f"cwd not under an allowlisted root: {target}")


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    # Auth disabled for now — focus on building the console first.
    return await handler(request)


async def auth_handler(request: web.Request) -> web.StreamResponse:
    provided = request.query.get("secret", "")
    secret = request.app["secret"]
    if not provided or not hmac.compare_digest(provided, secret):
        return web.Response(status=401, text="bad secret")
    resp = web.Response(status=302, headers={"Location": "/"})
    resp.set_cookie(
        COOKIE_NAME,
        _mint_cookie(secret),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


async def healthz(_request: web.Request) -> web.StreamResponse:
    return web.json_response({"ok": True})


async def chat_handler(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise web.HTTPBadRequest(reason="prompt required")
    cwd = _resolve_cwd(body.get("cwd"), request.app["default_cwd"])
    resume = body.get("resume")

    claude_bin = shutil.which("claude") or "claude"
    args = [
        claude_bin, "--print",
        "--output-format", "stream-json",
        "--verbose",  # required by Claude CLI when using stream-json output
        "--permission-mode", "default",
    ]
    if isinstance(resume, str) and resume:
        args += ["--resume", resume]
    args += ["-p", prompt]

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    assert proc.stdout is not None and proc.stderr is not None

    async def pump_stderr() -> None:
        # Stream stderr to console (for debugging), don't proxy to the client.
        async for line in proc.stderr:  # type: ignore[union-attr]
            print(f"[claude stderr] {line.decode(errors='replace').rstrip()}")

    stderr_task = asyncio.create_task(pump_stderr())

    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            if not line:
                continue
            # Pass through the stream-json line verbatim as one SSE frame.
            await resp.write(f"data: {line}\n\n".encode("utf-8"))
        await resp.write(b"data: [DONE]\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        proc.terminate()
        raise
    finally:
        await proc.wait()
        await stderr_task
    return resp


async def index_handler(_request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


# --- Sessions API ---

SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-local-home-xulaicao"


async def sessions_handler(_request: web.Request) -> web.StreamResponse:
    sessions = []
    if SESSIONS_DIR.is_dir():
        for f in sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            title = None
            try:
                with open(f) as fh:
                    for raw_line in fh:
                        try:
                            rec = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") == "custom-title":
                            title = rec.get("customTitle")
                            break
            except OSError:
                pass
            stat = f.stat()
            size_kb = stat.st_size // 1024
            from datetime import datetime
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%b %d %H:%M")
            sessions.append({
                "id": f.stem,
                "title": title or f.stem[:8],
                "date": mtime,
                "size": f"{size_kb}KB" if size_kb < 1024 else f"{size_kb // 1024}MB",
            })
    return web.json_response(sessions)


# --- Cron Jobs API ---

SCHEDULED_TASKS_PATH = Path.home() / ".claude" / "projects" / "-local-home-xulaicao" / ".." / ".." / "scheduled_tasks.json"


def _load_crons() -> list[dict]:
    # Try multiple known paths
    paths = [
        Path.home() / ".claude" / "scheduled_tasks.json",
        Path("$HOME/.claude/scheduled_tasks.json"),
    ]
    for p in paths:
        if p.exists():
            try:
                data = json.loads(p.read_text())
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("tasks", data.get("jobs", []))
            except (json.JSONDecodeError, OSError):
                pass
    return []


async def crons_handler(_request: web.Request) -> web.StreamResponse:
    jobs = _load_crons()
    result = []
    for j in jobs:
        result.append({
            "id": j.get("id", ""),
            "name": j.get("name", j.get("description", "")),
            "schedule": j.get("schedule", j.get("cron", "")),
            "prompt": j.get("prompt", j.get("command", ""))[:200],
        })
    return web.json_response(result)


async def cron_delete_handler(request: web.Request) -> web.StreamResponse:
    job_id = request.match_info["id"]
    # Use claude CLI to delete via CronDelete tool
    claude_bin = shutil.which("claude") or "claude"
    proc = await asyncio.create_subprocess_exec(
        claude_bin, "--print", "--permission-mode", "default",
        "-p", f"Delete the cron job with ID {job_id}. Use the CronDelete tool.",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    return web.json_response({"ok": True, "deleted": job_id})


def make_app(secret: str, default_cwd: Path) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["secret"] = secret
    app["default_cwd"] = default_cwd
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/auth", auth_handler)
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_get("/api/sessions", sessions_handler)
    app.router.add_get("/api/crons", crons_handler)
    app.router.add_delete("/api/crons/{id}", cron_delete_handler)
    app.router.add_static("/assets", STATIC_DIR / "assets", show_index=False)
    app.router.add_get("/{path:.*}", index_handler)
    return app


def main() -> None:
    _ensure_env()
    load_dotenv(ENV_PATH)
    secret = os.environ.get("CLAUDE_WEB_SECRET", "")
    if not secret:
        raise SystemExit("CLAUDE_WEB_SECRET not set in .env")
    port = int(os.environ.get("CLAUDE_WEB_PORT", "7780"))
    default_cwd = Path(os.environ.get("CLAUDE_WEB_CWD", str(Path.home()))).expanduser().resolve()
    app = make_app(secret, default_cwd)
    print(f"[claude-web] listening on 127.0.0.1:{port}, default cwd={default_cwd}")
    print(f"[claude-web] pair a device: GET /auth?secret={secret}")
    web.run_app(app, host="127.0.0.1", port=port, print=lambda *_: None)


if __name__ == "__main__":
    main()
