"""CRUD /api/crons — manage scheduled_tasks.json.

Source-of-truth note: the Claude Code harness scheduler (the CronCreate tool)
persists durable tasks to ``.claude/scheduled_tasks.json`` resolved **relative to
the session's working directory**, NOT to ``~/.claude/``. Empirically confirmed:
a harness-created durable job landed in ``<cwd>/.claude/scheduled_tasks.json``
while ``~/.claude/scheduled_tasks.json`` stayed empty. The claude-web server is
launched from the same working directory as the session, so we resolve the same
cwd-relative path here — that way the Cron Jobs tab is a faithful view of exactly
the tasks the scheduler fires, instead of a second, divergent list.

If the server is ever started from a different directory than the scheduler, set
``CLAUDE_WEB_TASKS_PATH`` to the scheduler's file to keep them aligned.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

from server.routes import read_json_body

from server import filestore


def _resolve_tasks_path() -> Path:
    """Resolve the scheduled_tasks.json the harness scheduler actually fires from.

    Precedence:
      1. ``CLAUDE_WEB_TASKS_PATH`` env override (explicit alignment escape hatch).
      2. ``<cwd>/.claude/scheduled_tasks.json`` — where the harness writes durable
         tasks (cwd-relative); the server shares the session's cwd.
    """
    env = os.environ.get("CLAUDE_WEB_TASKS_PATH")
    if env:
        return Path(env)
    return Path.cwd() / ".claude" / "scheduled_tasks.json"


# Resolved once at import (the server's cwd is fixed for its lifetime). Kept as a
# module attribute so it stays inspectable and overridable (tests monkeypatch it).
TASKS_PATH = _resolve_tasks_path()


def _resolve_runs_path() -> Path:
    """Resolve the cron run-history sidecar file.

    Derived from the SAME logic as TASKS_PATH so the two files stay aligned:
    ``cron_runs.json`` lives in the same dir as the harness scheduled_tasks.json.
    A ``CLAUDE_WEB_CRON_RUNS_PATH`` env override mirrors ``CLAUDE_WEB_TASKS_PATH``.
    """
    env = os.environ.get("CLAUDE_WEB_CRON_RUNS_PATH")
    if env:
        return Path(env)
    return TASKS_PATH.parent / "cron_runs.json"


# Run-history is a SEPARATE store, never written into scheduled_tasks.json. WHY:
# the Claude Code harness fires durable jobs externally and records NO run signal
# (real tasks have no lastFiredAt; scheduled_tasks.lock is a singleton lock, not a
# log), and claude-web has no code that fires crons. So this sidecar captures ONLY
# runs that flow through claude-web's POST endpoint and shows an honest empty state
# otherwise — we NEVER fabricate runs nor infer them from the harness files.
RUNS_PATH = _resolve_runs_path()

# Outcomes we accept; anything else is normalized to "unknown".
_VALID_OUTCOMES = {"success", "failure", "unknown"}
_RESULT_CAP = 4000


def register(app: web.Application):
    app.router.add_get("/api/crons", list_crons)
    app.router.add_post("/api/crons", create_cron)
    app.router.add_put("/api/crons/{job_id}", update_cron)
    app.router.add_delete("/api/crons/{job_id}", delete_cron)
    app.router.add_get("/api/crons/{job_id}/runs", list_runs)
    app.router.add_post("/api/crons/{job_id}/runs", append_run)


def _load() -> tuple[dict, str | None]:
    data, etag = filestore.read_json(TASKS_PATH)
    if not data:
        data = {"tasks": []}
    if "tasks" not in data:
        data["tasks"] = []
    return data, etag


def _load_runs() -> tuple[dict, str | None]:
    data, etag = filestore.read_json(RUNS_PATH)
    if not data:
        data = {"runs": {}}
    if "runs" not in data:
        data["runs"] = {}
    return data, etag


async def list_crons(request: web.Request) -> web.Response:
    data, etag = _load()
    return web.json_response({"jobs": data["tasks"], "etag": etag})


async def create_cron(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    cron_expr = body.get("cron", "").strip()
    prompt = body.get("prompt", "").strip()
    recurring = body.get("recurring", True)

    if not cron_expr or not prompt:
        raise web.HTTPBadRequest(reason="cron and prompt required")

    data, etag = _load()
    job = {
        "id": secrets.token_hex(4),
        "cron": cron_expr,
        "prompt": prompt,
        "recurring": recurring,
        "createdAt": int(time.time() * 1000),
        "lastFiredAt": None,
    }
    data["tasks"].append(job)

    try:
        new_etag = filestore.write_json(TASKS_PATH, data, etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_changed", {"id": job["id"]})
    return web.json_response({"job": job, "etag": new_etag}, status=201)


async def update_cron(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")

    data, current_etag = _load()
    job = next((j for j in data["tasks"] if j.get("id") == job_id), None)
    if not job:
        raise web.HTTPNotFound(reason=f"job {job_id} not found")

    if "cron" in body:
        job["cron"] = body["cron"]
    if "prompt" in body:
        job["prompt"] = body["prompt"]
    if "recurring" in body:
        job["recurring"] = body["recurring"]

    try:
        new_etag = filestore.write_json(TASKS_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_changed", {"id": job_id})
    return web.json_response({"job": job, "etag": new_etag})


async def delete_cron(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    # Read the body whether or not Content-Length is set (chunked bodies report
    # content_length=None) so the optimistic-concurrency etag is always honored.
    # An empty body is fine — it just means no etag was supplied.
    if request.can_read_body:
        body = await read_json_body(request)
    else:
        body = {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
    original_len = len(data["tasks"])
    data["tasks"] = [j for j in data["tasks"] if j.get("id") != job_id]

    if len(data["tasks"]) == original_len:
        raise web.HTTPNotFound(reason=f"job {job_id} not found")

    try:
        new_etag = filestore.write_json(TASKS_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_deleted", {"id": job_id})
    return web.json_response({"deleted": job_id, "etag": new_etag})


async def list_runs(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    data, etag = _load_runs()
    runs = data["runs"].get(job_id, [])
    # Newest-first. Defensive sort in case the file was hand-edited out of order.
    runs = sorted(runs, key=lambda r: r.get("ts", 0), reverse=True)
    return web.json_response({"runs": runs, "etag": etag})


async def append_run(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")

    outcome = body.get("outcome", "unknown")
    if outcome not in _VALID_OUTCOMES:
        outcome = "unknown"
    result = str(body.get("result", ""))[:_RESULT_CAP]

    run = {
        "id": secrets.token_hex(4),
        "ts": body.get("ts") or int(time.time() * 1000),
        "outcome": outcome,
        "result": result,
    }
    if body.get("sessionId"):
        run["sessionId"] = body["sessionId"]

    data, current_etag = _load_runs()
    # Prepend so the on-disk order is newest-first too.
    data["runs"].setdefault(job_id, []).insert(0, run)

    try:
        new_etag = filestore.write_json(RUNS_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_run_recorded", {"id": job_id})
    return web.json_response({"run": run, "etag": new_etag}, status=201)
