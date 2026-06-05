"""GET /api/tasks — read-only view of Claude Code task tracking."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web


TASKS_DIR = Path.home() / ".claude" / "tasks"


def register(app: web.Application):
    app.router.add_get("/api/tasks", list_tasks)


async def list_tasks(request: web.Request) -> web.Response:
    all_tasks = []
    if not TASKS_DIR.is_dir():
        return web.json_response([])

    for session_dir in TASKS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        for task_file in session_dir.glob("*.json"):
            try:
                data = json.loads(task_file.read_text())
                data["_sessionId"] = session_dir.name
                all_tasks.append(data)
            except (OSError, json.JSONDecodeError):
                continue

    # Sort: in_progress first, then by id
    all_tasks.sort(key=lambda t: (
        0 if t.get("status") == "in_progress" else 1,
        t.get("id", ""),
    ))
    return web.json_response(all_tasks)
