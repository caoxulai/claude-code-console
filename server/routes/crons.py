"""CRUD /api/crons — manage scheduled_tasks.json."""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from aiohttp import web

from server import filestore


TASKS_PATH = Path.home() / ".claude" / "scheduled_tasks.json"


def register(app: web.Application):
    app.router.add_get("/api/crons", list_crons)
    app.router.add_post("/api/crons", create_cron)
    app.router.add_put("/api/crons/{job_id}", update_cron)
    app.router.add_delete("/api/crons/{job_id}", delete_cron)


def _load() -> tuple[dict, str | None]:
    data, etag = filestore.read_json(TASKS_PATH)
    if not data:
        data = {"tasks": []}
    if "tasks" not in data:
        data["tasks"] = []
    return data, etag


async def list_crons(request: web.Request) -> web.Response:
    data, etag = _load()
    return web.json_response({"jobs": data["tasks"], "etag": etag})


async def create_cron(request: web.Request) -> web.Response:
    body = await request.json()
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
    body = await request.json()
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
    body = await request.json() if request.content_length else {}
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
