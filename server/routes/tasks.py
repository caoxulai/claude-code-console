"""GET /api/tasks — user-created TODO backlog + CLI task read-only mirror.

User tasks live in ~/.claude-web/tasks.json with lifecycle stages
(draft|clarifying|planned|executing|done|archived). CLI tasks (written by
Claude Code under ~/.claude/tasks/<sessionId>/<taskId>.json) are served via a
separate GET /api/tasks/agent endpoint — we NEVER modify those files except for
the explicit complete/delete actions on their own path.
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

# The user-created task store lives alongside the claude-web config file
# (default ~/.claude-web/config.json), so it follows CLAUDE_WEB_CONFIG if
# overridden.
_CONFIG_PATH = Path(os.environ.get(
    "CLAUDE_WEB_CONFIG",
    Path.home() / ".claude-web" / "config.json",
))
# Console-created tasks live here — NEVER in ~/.claude/tasks (that is Claude
# Code's own state, which we keep read-only except for the explicit
# complete/delete actions on its files).
USER_TASKS_PATH = _CONFIG_PATH.parent / "tasks.json"

# Synthetic _sessionId used for all console-created tasks, so they flow through
# the same list/complete/delete/trigger-goal plumbing as Claude tasks without
# special-casing every call site. It is NOT a real session id; the user-task
# endpoints route on the "user:" id prefix instead of the filesystem.
USER_SESSION_ID = "claude-web-user"

# Dirs under ~/.claude/projects that are not real projects (mirrors
# sessions.EXCLUDED_DIRS / the wf_ + "--" filtering there).
_EXCLUDED_DIRS = {"subagents", "transcripts", "memory"}

_MAP_TTL = 60  # seconds — how long the sessionId→project map stays warm

# Valid stages and priorities for user tasks.
VALID_STAGES = {"draft", "clarifying", "planned", "executing", "done", "archived"}
VALID_PRIORITIES = {"p1", "p2", "p3"}

# Forward-only stage transitions. Key = current stage, value = set of valid
# target stages.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"clarifying", "planned", "done", "archived"},
    "clarifying": {"planned", "done", "archived"},
    "planned": {"executing", "done", "archived"},
    "executing": {"done", "archived"},
    "done": {"archived"},
}


def register(app: web.Application):
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_get("/api/tasks/agent", list_agent_tasks)
    app.router.add_post("/api/tasks/complete", complete_task)
    app.router.add_post("/api/tasks/delete", delete_task)
    # Console-created (user) tasks — stored in ~/.claude-web/tasks.json.
    app.router.add_post("/api/tasks/user", create_user_task)
    app.router.add_put("/api/tasks/user", update_user_task)
    app.router.add_post("/api/tasks/user/{task_id}/advance", advance_task)


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
# User-created task store (~/.claude-web/tasks.json) — claude-web's own data
# ---------------------------------------------------------------------------
#
# These are ideas/TODOs captured from the console. They are NOT written into
# ~/.claude/tasks. Each carries an explicit project name + path (so the list,
# filters, and trigger-goal work just like Claude tasks) and a stable id of the
# form "user:<token>". They are surfaced with _sessionId = USER_SESSION_ID and
# source = "user" so the UI can badge them and the existing actions can route.

def _load_user_tasks() -> list[dict]:
    """Load user tasks with backward-compat migration.

    Tasks missing the new 'stage' field get defaults applied. The old 'status'
    field is removed on load (not persisted back until next write).
    """
    data, _etag = filestore.read_json(USER_TASKS_PATH)
    items = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    for t in items:
        # Migrate: status → stage
        if "stage" not in t:
            old_status = t.get("status")
            if old_status == "completed":
                t["stage"] = "done"
            else:
                t["stage"] = "draft"
        # Ensure new fields exist with defaults
        if "priority" not in t:
            t["priority"] = "p2"
        if "tags" not in t:
            t["tags"] = []
        if "clarification" not in t:
            t["clarification"] = None
        if "plan" not in t:
            t["plan"] = None
        if "execution" not in t:
            t["execution"] = None
        # Remove the old status field — stage replaces it entirely
        t.pop("status", None)

    return items


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
            try:
                data, _etag = filestore.read_json(task_file)
                if not isinstance(data, dict) or not data:
                    continue
                mtime = task_file.stat().st_mtime
            except (OSError, UnicodeDecodeError):
                continue
            data["_sessionId"] = session_dir.name
            data["_mtime"] = mtime
            tasks.append(data)
    return tasks


def project_task_counts() -> dict[str, dict]:
    """Per-project task counts, keyed by project name. Synchronous helper for
    other routes (e.g. /api/projects) to enrich their payload.

    Returns {projectName: {"total": n, "open": n}} where "open" excludes
    completed/done tasks. Uses the cached session→project map if warm, else
    builds it once. Safe to call from a thread.
    """
    mapping = _map_cache if _map_cache else _build_session_project_map()
    counts: dict[str, dict] = {}

    # Count CLI tasks (these still use the 'status' field).
    for t in _scan_tasks():
        proj = mapping.get(t["_sessionId"])
        name = proj["name"] if proj else "(unknown)"
        c = counts.setdefault(name, {"total": 0, "open": 0})
        c["total"] += 1
        if t.get("status") != "completed":
            c["open"] += 1

    # Also count user tasks (use 'stage' field).
    for t in _load_user_tasks():
        name = t.get("project") or "(global)"
        c = counts.setdefault(name, {"total": 0, "open": 0})
        c["total"] += 1
        if t.get("stage") not in ("done", "archived"):
            c["open"] += 1

    return counts


async def list_tasks(request: web.Request) -> web.Response:
    """List user-created tasks with optional filters.

    Query params (all optional):
      - project=<name>   only tasks whose project equals <name>
      - stage=<s>        only tasks with matching stage (comma-separated multi)

    Returns {tasks, projects} where projects is the distinct set for a filter
    dropdown.
    """
    project_filter = request.query.get("project")
    stage_filter_raw = request.query.get("stage")
    stage_filter: set[str] | None = None
    if stage_filter_raw:
        stage_filter = {s.strip() for s in stage_filter_raw.split(",") if s.strip()}

    user_tasks = await asyncio.to_thread(_load_user_tasks)

    # Shape user tasks for response.
    tasks = [_user_task_view(u) for u in user_tasks]

    # Distinct projects (before per-request filtering) for the filter dropdown.
    projects = sorted({t["project"] for t in tasks})

    # Apply filters.
    def keep(t: dict) -> bool:
        if project_filter and t["project"] != project_filter:
            return False
        if stage_filter and t.get("stage") not in stage_filter:
            return False
        return True

    filtered = [t for t in tasks if keep(t)]

    # Sort: active stages first (draft, clarifying, planned, executing), then
    # done/archived, then most recently modified.
    _stage_order = {
        "draft": 0, "clarifying": 1, "planned": 2, "executing": 3,
        "done": 4, "archived": 5,
    }
    filtered.sort(key=lambda t: (
        _stage_order.get(t.get("stage", "draft"), 9),
        -t["_mtime"],
        t.get("id", ""),
    ))

    return web.json_response({
        "tasks": filtered,
        "projects": projects,
    })


async def list_agent_tasks(request: web.Request) -> web.Response:
    """List CLI tasks (from Claude Code sessions) with project attribution.

    Query params (optional):
      - project=<name>   only tasks for the named project

    Returns {tasks: [...]}. Read-only, no mutation.
    """
    project_filter = request.query.get("project")

    cli_tasks, mapping = await asyncio.gather(
        asyncio.to_thread(_scan_tasks),
        _session_project_map(),
    )

    # Enrich CLI tasks with their project (name + path) from the map.
    for t in cli_tasks:
        proj = mapping.get(t["_sessionId"])
        t["project"] = proj["name"] if proj else "(unknown)"
        t["projectPath"] = proj["path"] if proj else None
        t["source"] = "claude"

    # Apply project filter.
    if project_filter:
        cli_tasks = [t for t in cli_tasks if t["project"] == project_filter]

    # Sort: in_progress first, then most recently modified, then id.
    cli_tasks.sort(key=lambda t: (
        0 if t.get("status") == "in_progress" else 1,
        -t["_mtime"],
        t.get("id", ""),
    ))

    return web.json_response({"tasks": cli_tasks})


async def advance_task(request: web.Request) -> web.Response:
    """Advance a user task to a new stage.

    POST /api/tasks/user/{task_id}/advance
    Body: {stage: "<target>"}

    Validates forward-only transitions per VALID_TRANSITIONS. Returns 400 on
    invalid transition, 404 if task not found.
    """
    task_id = request.match_info["task_id"]
    _validate_id(task_id, "task_id")

    body = await read_json_body(request)
    target_stage = body.get("stage")
    if not target_stage or target_stage not in VALID_STAGES:
        raise web.HTTPBadRequest(
            reason=f"stage must be one of: {', '.join(sorted(VALID_STAGES))}"
        )

    def _do() -> dict | None:
        tasks = _load_user_tasks()
        for t in tasks:
            if str(t.get("id")) == str(task_id):
                current_stage = t.get("stage", "draft")
                allowed = VALID_TRANSITIONS.get(current_stage, set())
                if target_stage not in allowed:
                    return {"error": "invalid_transition", "current": current_stage, "target": target_stage}
                t["stage"] = target_stage
                t["updatedAt"] = int(time.time() * 1000)
                _save_user_tasks(tasks)
                return {"task": t}
        return None

    result = await asyncio.to_thread(_do)
    if result is None:
        raise web.HTTPNotFound(reason="task not found")
    if "error" in result:
        return web.json_response(
            {"error": result["error"], "message": f"Cannot transition from '{result['current']}' to '{result['target']}'"},
            status=400,
        )

    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "task": _user_task_view(result["task"])})


# ---------------------------------------------------------------------------
# Mutating endpoints — REAL writes to Claude Code's task files
# ---------------------------------------------------------------------------
#
# These change the source ~/.claude/tasks/<sessionId>/<taskId>.json. They are
# deliberately narrow: only the named task file is touched, and only after
# path-traversal validation.

async def complete_task(request: web.Request) -> web.Response:
    """Mark a task complete.

    Body: {sessionId, taskId}. For Claude Code tasks, writes status:"completed"
    to the source ~/.claude/tasks file (their own schema). For console-created
    tasks (sessionId == USER_SESSION_ID), sets stage='done'. 404 if absent.
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
                    t["stage"] = "done"
                    t["updatedAt"] = int(time.time() * 1000)
                    _save_user_tasks(tasks)
                    return t
            return None
        updated = await asyncio.to_thread(_do_user)
        if not updated:
            raise web.HTTPNotFound(reason="task not found")
        await request.app["ws_manager"].broadcast("task_changed", {})
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
    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "task": updated})


async def delete_task(request: web.Request) -> web.Response:
    """Delete a single task. Body: {sessionId, taskId}.

    For Claude Code tasks, removes only that one ~/.claude/tasks JSON file (never
    the session directory). For console-created tasks (sessionId ==
    USER_SESSION_ID), removes the entry from ~/.claude-web/tasks.json. 404 if
    absent.
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
            return True
        deleted = await asyncio.to_thread(_do_user)
        if not deleted:
            raise web.HTTPNotFound(reason="task not found")
        await request.app["ws_manager"].broadcast("task_changed", {})
        return web.json_response({"ok": True, "deleted": f"{USER_SESSION_ID}/{task_id}"})

    path = _task_file(str(session_id), str(task_id))

    def _do() -> bool:
        if not path.is_file():
            return False
        filestore.delete_file(path)
        return True

    deleted = await asyncio.to_thread(_do)
    if not deleted:
        raise web.HTTPNotFound(reason="task not found")
    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "deleted": f"{session_id}/{task_id}"})


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
    """Create a console task. Body: {subject, description?, project?, priority?}.

    Stored in ~/.claude-web/tasks.json with a "user:<token>" id and stage
    "draft". Returns the created task in the standard list record shape.
    """
    body = await read_json_body(request)
    subject, description, project, project_path = _validate_user_task_body(body)

    # Validate optional priority.
    priority = body.get("priority", "p2")
    if priority not in VALID_PRIORITIES:
        raise web.HTTPBadRequest(reason=f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")

    def _do() -> dict:
        tasks = _load_user_tasks()
        now = int(time.time() * 1000)
        task = {
            "id": f"user:{secrets.token_hex(4)}",
            "subject": subject,
            "description": description,
            "stage": "draft",
            "priority": priority,
            "tags": [],
            "clarification": None,
            "plan": None,
            "execution": None,
            "project": project,
            "projectPath": project_path,
            "createdAt": now,
            "updatedAt": now,
        }
        tasks.append(task)
        _save_user_tasks(tasks)
        return task

    task = await asyncio.to_thread(_do)
    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "task": _user_task_view(task)}, status=201)


async def update_user_task(request: web.Request) -> web.Response:
    """Edit a console task.

    Body: {id, subject?, description?, project?, priority?, tags?,
           clarification?, plan?, execution?}.

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

    # Validate priority if provided.
    priority = body.get("priority")
    if priority is not None and priority not in VALID_PRIORITIES:
        raise web.HTTPBadRequest(reason=f"priority must be one of: {', '.join(sorted(VALID_PRIORITIES))}")

    # Validate tags if provided.
    tags = body.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise web.HTTPBadRequest(reason="tags must be a list of strings")

    # Validate structured fields if provided.
    clarification = body.get("clarification")
    if "clarification" in body and clarification is not None and not isinstance(clarification, dict):
        raise web.HTTPBadRequest(reason="clarification must be a dict or null")
    plan = body.get("plan")
    if "plan" in body and plan is not None and not isinstance(plan, dict):
        raise web.HTTPBadRequest(reason="plan must be a dict or null")
    execution = body.get("execution")
    if "execution" in body and execution is not None and not isinstance(execution, dict):
        raise web.HTTPBadRequest(reason="execution must be a dict or null")

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
                if priority is not None:
                    t["priority"] = priority
                if tags is not None:
                    t["tags"] = tags
                if "clarification" in body:
                    t["clarification"] = clarification
                if "plan" in body:
                    t["plan"] = plan
                if "execution" in body:
                    t["execution"] = execution
                t["updatedAt"] = int(time.time() * 1000)
                _save_user_tasks(tasks)
                return t
        return None

    updated = await asyncio.to_thread(_do)
    if not updated:
        raise web.HTTPNotFound(reason="task not found")
    await request.app["ws_manager"].broadcast("task_changed", {})
    return web.json_response({"ok": True, "task": _user_task_view(updated)})
