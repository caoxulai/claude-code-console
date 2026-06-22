"""GET /api/workers — registry of background asyncio workers and their live status."""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web


def register(app: web.Application):
    """Initialize the worker registry and add the listing route.

    Must be called BEFORE other modules' register() so that app['_worker_registry']
    exists when they call register_worker().
    """
    app["_worker_registry"] = []
    app.router.add_get("/api/workers", list_workers)


def register_worker(
    app: web.Application,
    name: str,
    task_key: str,
    interval_s: int | float,
    project: str | None = None,
    description: str = "",
) -> None:
    """Record a background worker in the registry.

    Called by other modules AFTER their asyncio.create_task() calls so the
    registry knows which app[task_key] to inspect for live status.
    """
    app["_worker_registry"].append({
        "name": name,
        "taskKey": task_key,
        "intervalS": interval_s,
        "project": project,
        "description": description,
        "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastRunAt": None,
        "lastResult": None,
    })


def mark_worker_run(app: web.Application, name: str, result: str | None = None) -> None:
    """Update lastRunAt (and optionally lastResult) for the named worker.

    Silently no-ops if the name is not found (defensive — a module may call this
    before its worker is registered during startup races).
    """
    for entry in app.get("_worker_registry", []):
        if entry["name"] == name:
            entry["lastRunAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if result is not None:
                entry["lastResult"] = result
            return


def _derive_status(app: web.Application, entry: dict) -> str:
    """Derive the live status of a worker from its CURRENT app[taskKey] reference.

    Uses the ACTUAL app[taskKey] (not a stale snapshot from registration time) so
    that a crashed-and-restarted task is correctly reported.
    """
    task = app.get(entry["taskKey"])
    if task is None:
        return "stopped"
    if task.done():
        if task.cancelled():
            return "stopped"
        try:
            exc = task.exception()
        except Exception:
            return "stopped"
        if exc is not None:
            return "errored"
        return "stopped"
    return "running"


async def list_workers(request: web.Request) -> web.Response:
    """Return the list of registered workers with their live status.

    Optional query param ?project=<name>: when present, returns the UNION of
    entries matching that project AND global workers (project is None). When
    absent, returns all workers.
    """
    app = request.app
    registry = app.get("_worker_registry", [])
    project_filter = request.rel_url.query.get("project")

    results = []
    for entry in registry:
        # Apply project filter (union of matching + global)
        if project_filter is not None:
            if entry["project"] is not None and entry["project"] != project_filter:
                continue

        status = _derive_status(app, entry)
        results.append({
            "name": entry["name"],
            "status": status,
            "project": entry["project"],
            "description": entry["description"],
            "intervalS": entry["intervalS"],
            "startedAt": entry["startedAt"],
            "lastRunAt": entry["lastRunAt"],
            "lastResult": entry["lastResult"],
        })

    return web.json_response(results)
