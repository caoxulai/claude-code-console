"""GET /api/tasks — read-only view of Claude Code task tracking.

Claude Code writes one JSON file per task under
~/.claude/tasks/<sessionId>/<taskId>.json. This endpoint aggregates them and
enriches each task with:

  - project: a human project name, derived by mapping the task's session id to
    the project whose transcript carries that session (Claude encodes the cwd as
    the project dir name under ~/.claude/projects). The mapping requires scanning
    the projects tree, so it's cached with a short TTL (see _session_project_map).
  - mtime: the task file's modification time, used for the time-range filter.

Dismissal is a claude-web-side view-state concern only: the user can hide
completed tasks, persisted in ~/.claude-web/dismissed_tasks.json. We NEVER
modify or delete the underlying ~/.claude/tasks files — they are Claude Code's
own state.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

from server.routes import read_json_body

from server import filestore
from server.routes.sessions import _project_label, WORKSPACE_DIR


TASKS_DIR = Path.home() / ".claude" / "tasks"
CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"

# The dismissed-task list and the user-created task store live alongside the
# claude-web config file (default ~/.claude-web/config.json), so they follow
# CLAUDE_WEB_CONFIG if overridden.
_CONFIG_PATH = Path(os.environ.get(
    "CLAUDE_WEB_CONFIG",
    Path.home() / ".claude-web" / "config.json",
))
DISMISSED_PATH = _CONFIG_PATH.parent / "dismissed_tasks.json"
# Console-created tasks live here — NEVER in ~/.claude/tasks (that is Claude
# Code's own state, which we keep read-only except for the explicit
# complete/delete actions on its files).
USER_TASKS_PATH = _CONFIG_PATH.parent / "tasks.json"

# Synthetic _sessionId used for all console-created tasks, so they flow through
# the same list/complete/delete/dismiss/trigger-goal plumbing as Claude tasks
# without special-casing every call site. It is NOT a real session id; the
# user-task endpoints route on the "user:" id prefix instead of the filesystem.
USER_SESSION_ID = "claude-web-user"

# Dirs under ~/.claude/projects that are not real projects (mirrors
# sessions.EXCLUDED_DIRS / the wf_ + "--" filtering there).
_EXCLUDED_DIRS = {"subagents", "transcripts", "memory"}

_MAP_TTL = 60  # seconds — how long the sessionId→project map stays warm


def register(app: web.Application):
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_get("/api/tasks/dismissed", get_dismissed)
    app.router.add_post("/api/tasks/dismiss", dismiss_task)
    app.router.add_post("/api/tasks/undismiss", undismiss_task)
    app.router.add_post("/api/tasks/complete", complete_task)
    app.router.add_post("/api/tasks/delete", delete_task)
    # Console-created (user) tasks — stored in ~/.claude-web/tasks.json.
    app.router.add_post("/api/tasks/user", create_user_task)
    app.router.add_put("/api/tasks/user", update_user_task)


def _validate_id(value: str, label: str) -> None:
    """Reject a session/task id with path-traversal characters.

    sessionId and taskId are joined into a filesystem path
    (TASKS_DIR/<sessionId>/<taskId>.json), so guard against escaping that dir.
    Mirrors sessions._validate_session_id.
    """
    if not value or ".." in value or "/" in value or "\\" in value:
        raise web.HTTPBadRequest(reason=f"invalid {label}")


def _task_file(session_id: str, task_id: str) -> Path:
    """Resolve the on-disk path for a task, after traversal validation.

    Returns the path even if it doesn't exist (callers 404 on absence). The
    resolved path is asserted to stay within its session dir as defense in depth.
    """
    _validate_id(session_id, "sessionId")
    _validate_id(str(task_id), "taskId")
    session_dir = TASKS_DIR / session_id
    path = session_dir / f"{task_id}.json"
    # Defense in depth: the resolved file must live directly under the session
    # dir. (The char checks above already prevent traversal; this catches any
    # symlink shenanigans.)
    if path.parent.resolve() != session_dir.resolve():
        raise web.HTTPBadRequest(reason="invalid task path")
    return path


# ---------------------------------------------------------------------------
# sessionId → project mapping (cached)
# ---------------------------------------------------------------------------
#
# Claude encodes a cwd as the project dir name by replacing "/" with "-" and
# prepending "-". The session's transcript lives at
# ~/.claude/projects/<slug>/<sessionId>.jsonl. To label a task we find which
# project dir holds <sessionId>.jsonl and turn the slug back into a readable
# project name. Scanning the whole projects tree per request is expensive, so
# the index is cached with a short TTL.

_map_cache: dict[str, dict] = {}
_map_updated_at: float = 0.0
_map_lock = asyncio.Lock()


def _slug_to_project(slug: str) -> dict:
    """Turn a project dir slug into {name, path}.

    Reuses sessions._project_label, which reconstructs the real filesystem path
    from the slug (greedily matching path segments against the real tree, so
    hyphenated directory names are handled correctly). The project name is that
    path's final segment; for a home-cwd slug that resolves to e.g.
    /local/home/<user>, the basename (the username) is a sensible group label.
    The path is surfaced so the UI can build a session slug for trigger-goal.
    """
    path = _project_label(slug)
    name = os.path.basename(path.rstrip("/")) or slug
    return {"name": name, "path": path}


def _build_session_project_map() -> dict[str, dict]:
    """Scan ~/.claude/projects once and map each sessionId → {name, path}.

    A session id can appear in more than one project dir (e.g. a transcripts/
    export). Real project dirs (immediate children with .jsonl files, excluding
    internal dirs) win over the internal subtrees, so we record those last and
    let them overwrite. Internal dirs (subagents/transcripts/memory, wf_*, and
    worktree "--" dirs) are skipped entirely.
    """
    mapping: dict[str, dict] = {}
    if not CLAUDE_PROJECTS_BASE.is_dir():
        return mapping

    for d in CLAUDE_PROJECTS_BASE.iterdir():
        if not d.is_dir():
            continue
        if d.name in _EXCLUDED_DIRS or d.name.startswith("wf_") or "--" in d.name:
            continue
        project = _slug_to_project(d.name)
        for jsonl in d.glob("*.jsonl"):
            mapping[jsonl.stem] = project

    return mapping


async def _session_project_map() -> dict[str, dict]:
    """Return the cached sessionId→{name,path} map, refreshing if stale."""
    global _map_cache, _map_updated_at
    now = time.monotonic()
    if _map_cache and now - _map_updated_at < _MAP_TTL:
        return _map_cache

    async with _map_lock:
        now = time.monotonic()
        if _map_cache and now - _map_updated_at < _MAP_TTL:
            return _map_cache
        mapping = await asyncio.to_thread(_build_session_project_map)
        _map_cache = mapping
        _map_updated_at = time.monotonic()
        return mapping


# ---------------------------------------------------------------------------
# Dismissed-task view state (claude-web side, never touches ~/.claude/tasks)
# ---------------------------------------------------------------------------

def _task_key(session_id: str, task_id: str) -> str:
    return f"{session_id}/{task_id}"


def _load_dismissed() -> set[str]:
    data, _etag = filestore.read_json(DISMISSED_PATH)
    keys = data.get("dismissed") if isinstance(data, dict) else None
    return set(keys) if isinstance(keys, list) else set()


def _save_dismissed(keys: set[str]) -> None:
    filestore.write_json(DISMISSED_PATH, {"dismissed": sorted(keys)})


# ---------------------------------------------------------------------------
# User-created task store (~/.claude-web/tasks.json) — claude-web's own data
# ---------------------------------------------------------------------------
#
# These are ideas/TODOs captured from the console. They are NOT written into
# ~/.claude/tasks. Each carries an explicit project name + path (so the list,
# filters, and trigger-goal work just like Claude tasks) and a stable id of the
# form "user:<token>". They are surfaced with _sessionId = USER_SESSION_ID and
# source = "user" so the UI can badge them and the existing actions can route.

def _load_user_tasks() -> list[dict]:
    data, _etag = filestore.read_json(USER_TASKS_PATH)
    items = data.get("tasks") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _save_user_tasks(tasks: list[dict]) -> None:
    filestore.write_json(USER_TASKS_PATH, {"tasks": tasks})


def _known_projects() -> dict[str, str]:
    """Map of project name -> real path for projects under the workspace dir.

    Used to validate the project a user assigns to a task and to resolve its
    path (for trigger-goal). Mirrors how list_projects enumerates WORKSPACE_DIR.
    """
    projects: dict[str, str] = {}
    if WORKSPACE_DIR.is_dir():
        for d in WORKSPACE_DIR.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                projects[d.name] = str(d)
    return projects


def _user_task_view(t: dict) -> dict:
    """Shape a stored user task into the same record shape list_tasks emits."""
    return {
        **t,
        "_sessionId": USER_SESSION_ID,
        "_mtime": t.get("updatedAt") or t.get("createdAt") or 0,
        "source": "user",
        # A user task with no project is a global task (not "unknown" — that
        # label is reserved for Claude tasks whose session can't be mapped).
        "project": t.get("project") or "(global)",
        "projectPath": t.get("projectPath"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _scan_tasks() -> list[dict]:
    """Read every task file, tagging session id and file mtime."""
    tasks: list[dict] = []
    if not TASKS_DIR.is_dir():
        return tasks

    for session_dir in TASKS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        for task_file in session_dir.glob("*.json"):
            data, _etag = filestore.read_json(task_file)
            if not isinstance(data, dict) or not data:
                continue
            try:
                mtime = task_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            data["_sessionId"] = session_dir.name
            data["_mtime"] = mtime
            tasks.append(data)
    return tasks


def project_task_counts() -> dict[str, dict]:
    """Per-project task counts, keyed by project name. Synchronous helper for
    other routes (e.g. /api/projects) to enrich their payload.

    Returns {projectName: {"total": n, "open": n}} where "open" excludes
    completed tasks. Uses the cached session→project map if warm, else builds it
    once. Dismissed tasks still count (the count reflects real task state, not
    the user's view filter). Safe to call from a thread.
    """
    mapping = _map_cache if _map_cache else _build_session_project_map()
    counts: dict[str, dict] = {}
    for t in _scan_tasks():
        proj = mapping.get(t["_sessionId"])
        name = proj["name"] if proj else "(unknown)"
        c = counts.setdefault(name, {"total": 0, "open": 0})
        c["total"] += 1
        if t.get("status") != "completed":
            c["open"] += 1
    return counts


async def list_tasks(request: web.Request) -> web.Response:
    """List tasks with project attribution and optional filters.

    Query params (all optional):
      - project=<name>   only tasks whose mapped project equals <name>
      - sinceDays=<n>    only tasks whose file mtime is within the last n days
      - includeDismissed=1  include tasks the user has hidden (default: exclude)

    The response also returns the set of distinct project names (so the UI can
    populate a filter) and the dismissed count, computed before filtering.
    """
    project_filter = request.query.get("project")
    since_days = request.query.get("sinceDays")
    include_dismissed = request.query.get("includeDismissed") == "1"

    claude_tasks, mapping, dismissed, user_tasks = await asyncio.gather(
        asyncio.to_thread(_scan_tasks),
        _session_project_map(),
        asyncio.to_thread(_load_dismissed),
        asyncio.to_thread(_load_user_tasks),
    )

    # Enrich Claude Code tasks with their project (name + path) from the map.
    for t in claude_tasks:
        proj = mapping.get(t["_sessionId"])
        t["project"] = proj["name"] if proj else "(unknown)"
        t["projectPath"] = proj["path"] if proj else None
        t["source"] = "claude"

    # User tasks already carry their own project; shape them to match.
    tasks = claude_tasks + [_user_task_view(u) for u in user_tasks]

    # Dismissed flag applies uniformly (key = "<sessionId>/<id>").
    for t in tasks:
        t["dismissed"] = _task_key(t["_sessionId"], str(t.get("id", ""))) in dismissed

    # Distinct projects (before per-request filtering) for the filter dropdown.
    projects = sorted({t["project"] for t in tasks})
    dismissed_count = sum(1 for t in tasks if t["dismissed"])

    # Apply filters.
    cutoff = None
    if since_days:
        try:
            n = float(since_days)
            if n > 0:
                cutoff = time.time() - n * 86400
        except ValueError:
            cutoff = None

    def keep(t: dict) -> bool:
        if not include_dismissed and t["dismissed"]:
            return False
        if project_filter and t["project"] != project_filter:
            return False
        if cutoff is not None and t["_mtime"] < cutoff:
            return False
        return True

    filtered = [t for t in tasks if keep(t)]

    # Sort: in_progress first, then most recently modified, then id.
    filtered.sort(key=lambda t: (
        0 if t.get("status") == "in_progress" else 1,
        -t["_mtime"],
        t.get("id", ""),
    ))

    return web.json_response({
        "tasks": filtered,
        "projects": projects,
        "dismissedCount": dismissed_count,
    })


async def get_dismissed(request: web.Request) -> web.Response:
    dismissed = await asyncio.to_thread(_load_dismissed)
    return web.json_response({"dismissed": sorted(dismissed)})


async def dismiss_task(request: web.Request) -> web.Response:
    """Hide a task from the default view. Body: {sessionId, taskId}.

    View-state only — the underlying ~/.claude/tasks file is untouched.
    """
    body = await read_json_body(request)
    session_id = body.get("sessionId")
    task_id = body.get("taskId")
    if not session_id or task_id is None:
        raise web.HTTPBadRequest(reason="sessionId and taskId required")

    def _do() -> set[str]:
        dismissed = _load_dismissed()
        dismissed.add(_task_key(str(session_id), str(task_id)))
        _save_dismissed(dismissed)
        return dismissed

    dismissed = await asyncio.to_thread(_do)
    return web.json_response({"dismissed": sorted(dismissed)})


async def undismiss_task(request: web.Request) -> web.Response:
    """Un-hide a previously dismissed task. Body: {sessionId, taskId}."""
    body = await read_json_body(request)
    session_id = body.get("sessionId")
    task_id = body.get("taskId")
    if not session_id or task_id is None:
        raise web.HTTPBadRequest(reason="sessionId and taskId required")

    def _do() -> set[str]:
        dismissed = _load_dismissed()
        dismissed.discard(_task_key(str(session_id), str(task_id)))
        _save_dismissed(dismissed)
        return dismissed

    dismissed = await asyncio.to_thread(_do)
    return web.json_response({"dismissed": sorted(dismissed)})


# ---------------------------------------------------------------------------
# Mutating endpoints — REAL writes to Claude Code's task files
# ---------------------------------------------------------------------------
#
# Unlike dismiss/undismiss (claude-web view-state), these change the source
# ~/.claude/tasks/<sessionId>/<taskId>.json. They are deliberately narrow: only
# the named task file is touched, and only after path-traversal validation.

async def complete_task(request: web.Request) -> web.Response:
    """Mark a task complete.

    Body: {sessionId, taskId}. For Claude Code tasks, writes status:"completed"
    to the source ~/.claude/tasks file. For console-created tasks (sessionId ==
    USER_SESSION_ID), updates the entry in ~/.claude-web/tasks.json. 404 if absent.
    """
    body = await read_json_body(request)
    session_id = body.get("sessionId")
    task_id = body.get("taskId")
    if not session_id or task_id is None:
        raise web.HTTPBadRequest(reason="sessionId and taskId required")

    # Console-created task → mutate the user store, not the filesystem.
    if str(session_id) == USER_SESSION_ID:
        def _do_user() -> dict | None:
            tasks = _load_user_tasks()
            for t in tasks:
                if str(t.get("id")) == str(task_id):
                    t["status"] = "completed"
                    t["updatedAt"] = int(time.time() * 1000)
                    _save_user_tasks(tasks)
                    return t
            return None
        updated = await asyncio.to_thread(_do_user)
        if not updated:
            raise web.HTTPNotFound(reason="task not found")
        return web.json_response({"ok": True, "task": _user_task_view(updated)})

    path = _task_file(str(session_id), str(task_id))

    def _do() -> dict:
        data, etag = filestore.read_json(path)
        if not isinstance(data, dict) or not data:
            return {}
        data["status"] = "completed"
        # Only update status (other fields preserved). The etag read just above
        # guards against a concurrent CLI write between read and write.
        filestore.write_json(path, data, etag)
        return data

    try:
        updated = await asyncio.to_thread(_do)
    except filestore.ConflictError as e:
        # A concurrent writer (e.g. the CLI) changed the file between our read
        # and write — surface 409 like the other write endpoints, not 500.
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)
    if not updated:
        raise web.HTTPNotFound(reason="task not found")
    return web.json_response({"ok": True, "task": updated})


async def delete_task(request: web.Request) -> web.Response:
    """Delete a single task. Body: {sessionId, taskId}.

    For Claude Code tasks, removes only that one ~/.claude/tasks JSON file (never
    the session directory). For console-created tasks (sessionId ==
    USER_SESSION_ID), removes the entry from ~/.claude-web/tasks.json. Also drops
    any dismissed-list entry so stale view-state doesn't linger. 404 if absent.
    """
    body = await read_json_body(request)
    session_id = body.get("sessionId")
    task_id = body.get("taskId")
    if not session_id or task_id is None:
        raise web.HTTPBadRequest(reason="sessionId and taskId required")

    # Console-created task → remove from the user store, not the filesystem.
    if str(session_id) == USER_SESSION_ID:
        def _do_user() -> bool:
            tasks = _load_user_tasks()
            kept = [t for t in tasks if str(t.get("id")) != str(task_id)]
            if len(kept) == len(tasks):
                return False
            _save_user_tasks(kept)
            dismissed = _load_dismissed()
            key = _task_key(USER_SESSION_ID, str(task_id))
            if key in dismissed:
                dismissed.discard(key)
                _save_dismissed(dismissed)
            return True
        deleted = await asyncio.to_thread(_do_user)
        if not deleted:
            raise web.HTTPNotFound(reason="task not found")
        return web.json_response({"ok": True, "deleted": _task_key(USER_SESSION_ID, str(task_id))})

    path = _task_file(str(session_id), str(task_id))

    def _do() -> bool:
        if not path.is_file():
            return False
        filestore.delete_file(path)
        # Clean up any dismissed-list entry for the now-gone task.
        dismissed = _load_dismissed()
        key = _task_key(str(session_id), str(task_id))
        if key in dismissed:
            dismissed.discard(key)
            _save_dismissed(dismissed)
        return True

    deleted = await asyncio.to_thread(_do)
    if not deleted:
        raise web.HTTPNotFound(reason="task not found")
    return web.json_response({"ok": True, "deleted": _task_key(str(session_id), str(task_id))})


# ---------------------------------------------------------------------------
# Console-created task CRUD (~/.claude-web/tasks.json)
# ---------------------------------------------------------------------------

def _validate_user_task_body(body: dict) -> tuple[str, str, str | None, str | None]:
    """Validate + normalize a create/update body.

    Returns (subject, description, project_name, project_path). project_name/path
    are None for a global (no-project) task. Raises HTTPBadRequest on a missing
    subject or a non-empty project that isn't a known workspace project.
    """
    subject = (body.get("subject") or "").strip()
    if not subject:
        raise web.HTTPBadRequest(reason="subject required")
    description = (body.get("description") or "").strip()

    # Project is OPTIONAL — an empty value means a global task (no project).
    project = (body.get("project") or "").strip()
    if not project:
        return subject, description, None, None
    # A non-empty project must be a known workspace project — this both validates
    # input and resolves the real path (rejects traversal / unknown names).
    known = _known_projects()
    if project not in known:
        raise web.HTTPBadRequest(reason="unknown project")
    return subject, description, project, known[project]


async def create_user_task(request: web.Request) -> web.Response:
    """Create a console task. Body: {subject, description?, project}.

    Stored in ~/.claude-web/tasks.json with a "user:<token>" id and status
    "pending". Returns the created task in the standard list record shape.
    """
    body = await read_json_body(request)
    subject, description, project, project_path = _validate_user_task_body(body)

    def _do() -> dict:
        tasks = _load_user_tasks()
        now = int(time.time() * 1000)
        task = {
            "id": f"user:{secrets.token_hex(4)}",
            "subject": subject,
            "description": description,
            "status": "pending",
            "project": project,
            "projectPath": project_path,
            "createdAt": now,
            "updatedAt": now,
        }
        tasks.append(task)
        _save_user_tasks(tasks)
        return task

    task = await asyncio.to_thread(_do)
    # Notify open clients so the Tasks view refetches (C4). Outside the thread
    # because broadcast is async; the callback carries no fields — useLiveUpdates
    # just refetches — so an empty payload is intentional.
    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "task": _user_task_view(task)}, status=201)


async def update_user_task(request: web.Request) -> web.Response:
    """Edit a console task. Body: {id, subject?, description?, project?}.

    Only console-created tasks can be edited. 404 if the id isn't in the store.
    """
    body = await read_json_body(request)
    task_id = body.get("id")
    if not task_id:
        raise web.HTTPBadRequest(reason="id required")

    # Re-validate any provided fields. subject/project are validated only when
    # present (partial edit); but subject can never be blanked to empty.
    subject = body.get("subject")
    if subject is not None and not str(subject).strip():
        raise web.HTTPBadRequest(reason="subject cannot be empty")
    # project provided: "" clears to global; a name must be a known project.
    project_provided = body.get("project") is not None
    project_name = None
    project_path = None
    if project_provided:
        project_name = str(body["project"]).strip()
        if project_name:
            known = _known_projects()
            if project_name not in known:
                raise web.HTTPBadRequest(reason="unknown project")
            project_path = known[project_name]
        else:
            project_name = None  # empty → global

    def _do() -> dict | None:
        tasks = _load_user_tasks()
        for t in tasks:
            if str(t.get("id")) == str(task_id):
                if subject is not None:
                    t["subject"] = str(subject).strip()
                if body.get("description") is not None:
                    t["description"] = str(body["description"]).strip()
                if project_provided:
                    t["project"] = project_name
                    t["projectPath"] = project_path
                t["updatedAt"] = int(time.time() * 1000)
                _save_user_tasks(tasks)
                return t
        return None

    updated = await asyncio.to_thread(_do)
    if not updated:
        raise web.HTTPNotFound(reason="task not found")
    # Only broadcast on a real change (never on a 404). Empty payload: the
    # frontend callback takes no args and just refetches (C4).
    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "task": _user_task_view(updated)})
