"""GET /api/sessions — list sessions across all projects, with proper title extraction."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aiohttp import web


WORKSPACE_DIR = Path("$HOME/workspace/projects")
CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"
LIVE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


def register(app: web.Application):
    app.router.add_get("/api/projects", list_projects)
    app.router.add_put("/api/projects/{project_id}/claude-md", save_project_claude_md)
    app.router.add_get("/api/projects/{project_id}/sop/{filename}", get_project_sop)
    app.router.add_get("/api/projects/{project_id}/memory/{filename}", get_project_memory)
    app.router.add_put("/api/projects/{project_id}/memory/{filename}", save_project_memory)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_get("/api/sessions/live", list_live_sessions)
    app.router.add_get("/api/sessions/{session_id}/transcript", get_transcript)
    app.router.add_get("/api/sessions/{session_id}/title", get_session_title)
    app.router.add_put("/api/sessions/{session_id}/title", set_session_title)
    app.router.add_delete("/api/sessions/{session_id}", delete_session)


def _is_interactive_session(path: Path) -> bool:
    """Check if a session should appear in the session list (matches claude --resume).

    Only true interactive sessions are shown: first line type is 'mode', 'custom-title',
    or 'last-prompt'. Print-mode sessions (first line = 'queue-operation') are always hidden,
    regardless of whether they have a title — this matches CLI behavior exactly.
    """
    try:
        with open(path) as fh:
            first_line = fh.readline()
            if not first_line:
                return False
            rec = json.loads(first_line)
            return rec.get("type") in ("mode", "custom-title", "last-prompt")
    except (OSError, json.JSONDecodeError):
        return False


def _extract_title(path: Path) -> str | None:
    """Extract session title from JSONL.

    Returns the LAST custom-title if any exist (user renames append new entries),
    otherwise the last ai-title. This matches CLI --resume behavior.
    """
    custom_title = None
    ai_title = None
    try:
        with open(path) as fh:
            for raw_line in fh:
                try:
                    rec = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype == "custom-title":
                    custom_title = rec.get("customTitle")
                elif rtype == "ai-title":
                    ai_title = rec.get("aiTitle") or rec.get("title")
    except OSError:
        pass
    return custom_title or ai_title


def _project_label(dirname: str) -> str:
    """Convert project dir name back to a real filesystem path.

    Claude Code encodes cwds by replacing / with - and prepending -.
    e.g. "$HOME/workspace/projects/oncall-kpi"
      → "-local-home-xulaicao-workspace-projects-oncall-kpi"

    The trick: we can't just replace all - with / because directory names
    contain hyphens. Instead, greedily match path segments against the real
    filesystem starting from /.
    """
    if not dirname.startswith("-"):
        return dirname
    parts = dirname[1:].split("-")
    # Greedily reconstruct the path by testing which combinations are real dirs
    path = "/"
    i = 0
    while i < len(parts):
        # Try increasingly longer hyphenated segments
        found = False
        for end in range(len(parts), i, -1):
            candidate = "-".join(parts[i:end])
            test_path = path.rstrip("/") + "/" + candidate
            if Path(test_path).exists():
                path = test_path
                i = end
                found = True
                break
        if not found:
            # Fallback: treat single part as a path segment
            path = path.rstrip("/") + "/" + parts[i]
            i += 1
    return path


EXCLUDED_DIRS = {"subagents", "transcripts", "memory"}


def _get_all_project_dirs() -> list[Path]:
    """List project directories that contain real user sessions.

    Excludes: subagents/, transcripts/, wf_*/ (workflow logs), memory/,
    and any nested subdirectory (real project dirs are immediate children).
    """
    if not CLAUDE_PROJECTS_BASE.is_dir():
        return []
    dirs = []
    for d in CLAUDE_PROJECTS_BASE.iterdir():
        if not d.is_dir():
            continue
        # Skip internal dirs
        if d.name in EXCLUDED_DIRS or d.name.startswith("wf_") or "--" in d.name:
            continue
        # Only include dirs that have .jsonl files directly (not nested)
        if any(d.glob("*.jsonl")):
            dirs.append(d)
    return dirs


def _app_urls_for_project(project_name: str) -> list[dict]:
    """Return known URLs for a project (local dev servers and deployed apps)."""
    urls = []
    if "claude-web" in project_name:
        urls.append({"url": "http://127.0.0.1:7780", "label": "Local", "type": "local"})
    if "oncall-kpi" in project_name:
        urls.append({"url": "http://127.0.0.1:8080", "label": "Local", "type": "local"})
        urls.append({"url": "https://black-falcon-oncall-dashboard.beta.harmony.a2z.com/", "label": "Harmony", "type": "deployed"})
    return urls


def _collect_sop_files(project_path: Path) -> list[dict]:
    """Collect SOP/skill markdown files from known directories in a project.

    Searches (in order):
      - <project>/agent-sops/*.md (and recursive subdirs)
      - <project>/skills/*.md
      - <project>/docs/*.md
    """
    results: list[dict] = []
    seen_paths: set[str] = set()

    # agent-sops — recursive
    agent_sops_dir = project_path / "agent-sops"
    if agent_sops_dir.is_dir():
        for md in sorted(agent_sops_dir.rglob("*.md")):
            if md.is_file() and str(md) not in seen_paths:
                seen_paths.add(str(md))
                results.append({"name": md.name, "path": str(md)})

    # skills/*.md — flat
    skills_dir = project_path / "skills"
    if skills_dir.is_dir():
        for md in sorted(skills_dir.glob("*.md")):
            if md.is_file() and str(md) not in seen_paths:
                seen_paths.add(str(md))
                results.append({"name": md.name, "path": str(md)})

    # docs/*.md — flat
    docs_dir = project_path / "docs"
    if docs_dir.is_dir():
        for md in sorted(docs_dir.glob("*.md")):
            if md.is_file() and str(md) not in seen_paths:
                seen_paths.add(str(md))
                results.append({"name": md.name, "path": str(md)})

    return results


def _collect_memory_files(session_dir: Path | None) -> list[dict]:
    """Collect memory file names from a project's Claude memory directory."""
    if not session_dir or not session_dir.is_dir():
        return []
    memory_dir = session_dir / "memory"
    if not memory_dir.is_dir():
        return []
    return [{"name": md.name} for md in sorted(memory_dir.glob("*.md")) if md.is_file()]


def _project_path_to_claude_slug(project_path: str) -> str:
    """Convert a real project path to the Claude session directory slug.

    Claude Code encodes cwds by replacing / with - and prepending -.
    e.g. "$HOME/workspace/projects/oncall-kpi"
      -> "-local-home-xulaicao-workspace-projects-oncall-kpi"
    """
    return "-" + project_path.lstrip("/").replace("/", "-")


async def list_projects(request: web.Request) -> web.Response:
    """List all real projects from WORKSPACE_DIR with Claude Code activity metadata."""
    projects = []

    if not WORKSPACE_DIR.is_dir():
        return web.json_response([])

    for d in WORKSPACE_DIR.iterdir():
        if not d.is_dir():
            continue
        # Skip hidden directories
        if d.name.startswith("."):
            continue

        project_name = d.name
        project_path = str(d)

        # a. Check for CLAUDE.md in the project
        claude_md_content = None
        claude_md_path = d / "CLAUDE.md"
        if claude_md_path.is_file():
            try:
                claude_md_content = claude_md_path.read_text(encoding="utf-8")
            except OSError:
                pass

        # b. Check for .claude/settings.json
        has_settings = (d / ".claude" / "settings.json").is_file()

        # c. Find matching session dir in ~/.claude/projects/ and count .jsonl files
        slug = _project_path_to_claude_slug(project_path)
        session_dir = CLAUDE_PROJECTS_BASE / slug
        jsonl_files = list(session_dir.glob("*.jsonl")) if session_dir.is_dir() else []
        session_count = len(jsonl_files)

        # d. Check for memory files
        memory_dir = session_dir / "memory" if session_dir.is_dir() else None
        memory_count = 0
        if memory_dir and memory_dir.is_dir():
            memory_count = len(list(memory_dir.glob("*.md")))

        # e. appUrls heuristic
        app_urls = _app_urls_for_project(project_name)

        # f. lastActivity from most recent .jsonl mtime
        last_activity = None
        last_mtime = None
        if jsonl_files:
            last_mtime = max(f.stat().st_mtime for f in jsonl_files)
            last_activity = datetime.fromtimestamp(last_mtime).strftime("%b %d %H:%M")

        # g. SOP/skill files in the project directory
        sop_files = _collect_sop_files(d)

        # h. Memory files from Claude's project memory dir
        memory_files = _collect_memory_files(session_dir)

        projects.append({
            "id": project_name,
            "name": project_name,
            "path": project_path,
            "sessionCount": session_count,
            "memoryCount": memory_count,
            "lastActivity": last_activity,
            "claudeMd": claude_md_content,
            "hasSettings": has_settings,
            "appUrl": app_urls[0]["url"] if app_urls else None,
            "appUrls": app_urls,
            "sopFiles": sop_files,
            "memoryFiles": memory_files,
            "_mtime": last_mtime,  # internal sort key
        })

    # Sort by lastActivity (most recent first), nulls at the end
    projects.sort(key=lambda p: (p["_mtime"] is None, -(p["_mtime"] or 0)))

    # Remove internal sort key before returning
    for p in projects:
        del p["_mtime"]

    return web.json_response(projects)


async def save_project_claude_md(request: web.Request) -> web.Response:
    """Write content to a project's CLAUDE.md file."""
    project_id = request.match_info["project_id"]
    body = await request.json()
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(reason="content field required")

    project_dir = WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    claude_md_path = project_dir / "CLAUDE.md"
    try:
        claude_md_path.write_text(content, encoding="utf-8")
    except OSError as e:
        raise web.HTTPInternalServerError(reason=f"failed to write CLAUDE.md: {e}")

    return web.json_response({"ok": True})


async def get_project_sop(request: web.Request) -> web.Response:
    """Read and return the content of a specific SOP file from a project."""
    project_id = request.match_info["project_id"]
    filename = request.match_info["filename"]

    # Validate filename: no path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise web.HTTPBadRequest(reason="invalid filename")

    project_dir = WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    # Search for the file in known SOP directories
    sop_files = _collect_sop_files(project_dir)
    matching = [f for f in sop_files if f["name"] == filename]
    if not matching:
        raise web.HTTPNotFound(reason="SOP file not found")

    # Use the first match
    file_path = Path(matching[0]["path"])
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise web.HTTPInternalServerError(reason=f"failed to read SOP file: {e}")

    return web.json_response({"name": filename, "content": content})


async def get_project_memory(request: web.Request) -> web.Response:
    """Read a memory file from the project-specific Claude memory directory."""
    project_id = request.match_info["project_id"]
    filename = request.match_info["filename"]

    # Validate filename: no path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise web.HTTPBadRequest(reason="invalid filename")

    project_dir = WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    slug = _project_path_to_claude_slug(str(project_dir))
    memory_dir = CLAUDE_PROJECTS_BASE / slug / "memory"
    file_path = memory_dir / filename

    if not file_path.is_file():
        raise web.HTTPNotFound(reason="memory file not found")

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise web.HTTPInternalServerError(reason=f"failed to read memory file: {e}")

    return web.json_response({"name": filename, "content": content})


async def save_project_memory(request: web.Request) -> web.Response:
    """Write content to a memory file in the project-specific Claude memory directory."""
    project_id = request.match_info["project_id"]
    filename = request.match_info["filename"]

    # Validate filename: no path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise web.HTTPBadRequest(reason="invalid filename")

    project_dir = WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    body = await request.json()
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(reason="content field required")

    slug = _project_path_to_claude_slug(str(project_dir))
    memory_dir = CLAUDE_PROJECTS_BASE / slug / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    file_path = memory_dir / filename

    try:
        file_path.write_text(content, encoding="utf-8")
    except OSError as e:
        raise web.HTTPInternalServerError(reason=f"failed to write memory file: {e}")

    return web.json_response({"name": filename, "content": content})


def _cwd_to_project_dir(cwd: str) -> str:
    """Convert a cwd path to the project directory name Claude Code uses."""
    # Claude Code replaces / with - and prepends -
    return "-" + cwd.lstrip("/").replace("/", "-")


async def list_sessions(request: web.Request) -> web.Response:
    """List sessions from all projects by default, or filtered to a single project.

    - GET /api/sessions (no params) → return sessions from ALL project dirs.
      Apply _is_interactive_session only for the home-cwd bucket (where it's needed
      to match CLI behavior and hide print-mode sessions). For other project dirs,
      include all sessions unconditionally.
    - GET /api/sessions?project=<slug> → return all sessions from that specific dir
      (no interactive filter).
    """
    project = request.query.get("project")
    limit = int(request.query.get("limit", "50"))
    offset = int(request.query.get("offset", "0"))

    # Determine the home-cwd bucket name (used to decide where interactive filter applies)
    default_cwd = str(request.app["default_cwd"])
    home_bucket = _cwd_to_project_dir(default_cwd)

    if project:
        # Specific project requested — show all sessions from that dir (no filter)
        dirs = [CLAUDE_PROJECTS_BASE / project]
    else:
        # No project param — scan ALL project dirs
        dirs = _get_all_project_dirs()

    all_files = []
    for d in dirs:
        if d.is_dir():
            for f in d.glob("*.jsonl"):
                all_files.append((f, d.name))

    # Sort by mtime descending
    all_files.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)

    # Filter logic:
    # - For the home bucket: ALWAYS apply _is_interactive_session to hide print-mode
    #   sessions (regardless of whether accessed via ?project= or all-scan).
    # - For other project dirs: no filter (show everything).

    sessions = []
    skipped = 0
    for f, proj_name in all_files:
        if proj_name == home_bucket:
            # Home bucket: always apply interactive filter to hide print-mode sessions
            if not _is_interactive_session(f):
                skipped += 1
                continue
        title = _extract_title(f)
        if len(sessions) >= offset + limit:
            break
        if len(sessions) < offset:
            sessions.append(None)  # placeholder for offset counting
            continue
        stat = f.stat()
        size_kb = stat.st_size // 1024
        sessions.append({
            "id": f.stem,
            "title": title or f.stem[:8],
            "project": proj_name,
            "projectPath": _project_label(proj_name),
            "date": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d %H:%M"),
            "mtime": stat.st_mtime,
            "size": f"{size_kb}KB" if size_kb < 1024 else f"{size_kb // 1024}MB",
        })

    # Remove offset placeholders
    sessions = [s for s in sessions if s is not None]
    total_with_titles = len(all_files) - skipped
    return web.json_response({"sessions": sessions, "total": total_with_titles})


async def list_live_sessions(request: web.Request) -> web.Response:
    """List currently running Claude sessions."""
    live = []
    if not LIVE_SESSIONS_DIR.is_dir():
        return web.json_response([])
    for f in LIVE_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            live.append({
                "pid": data.get("pid"),
                "sessionId": data.get("sessionId"),
                "cwd": data.get("cwd"),
                "name": data.get("name"),
                "status": data.get("status"),
                "kind": data.get("kind"),
                "startedAt": data.get("startedAt"),
                "version": data.get("version"),
            })
        except (OSError, json.JSONDecodeError):
            continue
    return web.json_response(live)


async def get_transcript(request: web.Request) -> web.Response:
    """Stream a session transcript as JSON array (paginated by line count)."""
    session_id = request.match_info["session_id"]

    # Search across all project dirs for this session
    path = None
    for d in _get_all_project_dirs():
        candidate = d / f"{session_id}.jsonl"
        if candidate.exists():
            path = candidate
            break

    if not path:
        raise web.HTTPNotFound(reason="session not found")

    limit = int(request.query.get("limit", "200"))
    offset = int(request.query.get("offset", "0"))

    messages = []
    with open(path) as fh:
        for i, line in enumerate(fh):
            if i < offset:
                continue
            if len(messages) >= limit:
                break
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return web.json_response({"messages": messages, "offset": offset, "limit": limit})


def _find_session_path(session_id: str) -> Path | None:
    """Find the JSONL file for a session across all project dirs."""
    for d in _get_all_project_dirs():
        candidate = d / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


async def get_session_title(request: web.Request) -> web.Response:
    """Get the title of a session."""
    session_id = request.match_info["session_id"]
    path = _find_session_path(session_id)
    if not path:
        raise web.HTTPNotFound(reason="session not found")
    title = _extract_title(path)
    return web.json_response({"sessionId": session_id, "title": title or ""})


async def set_session_title(request: web.Request) -> web.Response:
    """Set/update the session title by appending a custom-title entry to the JSONL."""
    session_id = request.match_info["session_id"]
    path = _find_session_path(session_id)
    if not path:
        raise web.HTTPNotFound(reason="session not found")

    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        raise web.HTTPBadRequest(reason="title required")

    entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    with open(path, "a") as fh:
        fh.write(entry + "\n")

    return web.json_response({"sessionId": session_id, "title": title})


async def delete_session(request: web.Request) -> web.Response:
    """Delete a session's JSONL file."""
    session_id = request.match_info["session_id"]

    # Search all project dirs for the session file
    for d in _get_all_project_dirs():
        candidate = d / f"{session_id}.jsonl"
        if candidate.exists():
            candidate.unlink()
            return web.json_response({"deleted": session_id})

    raise web.HTTPNotFound(reason="session not found")
