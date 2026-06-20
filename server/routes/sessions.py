"""GET /api/sessions — list sessions across all projects, with proper title extraction."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aiohttp import web

from server.routes import read_json_body


def _resolve_workspace_dir() -> Path:
    """Resolve the workspace directory from config or environment.

    Returns the real (symlink-resolved) path so project slugs match the ones
    Claude Code wrote under ~/.claude/projects. On Cloud Desktops, $HOME is
    /home/<user> which symlinks to /local/home/<user>; Claude records cwds
    under the real /local/home path, so we must resolve() to match.
    """
    env = os.environ.get("CLAUDE_WEB_WORKSPACE")
    base = Path(env) if env else Path.home() / "workspace" / "projects"
    return base.resolve()


WORKSPACE_DIR = _resolve_workspace_dir()
CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"
LIVE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

logger = logging.getLogger(__name__)


def register(app: web.Application):
    app.router.add_get("/api/config", get_config)
    app.router.add_get("/api/projects", list_projects)
    app.router.add_get("/api/projects/{project_id}/detail", get_project_detail)
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

    # ── Session-transcript file-watcher (E2) ──────────────────────────────────
    # The CLI appends to a transcript .jsonl as it streams; nothing in-process
    # otherwise observes that write, so SessionsPage previously backstop-polled
    # the open transcript. This watcher broadcasts 'session_changed' when the
    # NEWEST transcript's mtime advances (the actively-written/live session is
    # almost always the newest), letting any open page refetch promptly. It is a
    # cheap one rglob().max() per tick run OFF the event loop — it spawns no
    # subprocess. It mirrors slack.py's _slack_watcher lifecycle EXACTLY
    # (idempotent create + cancel-and-await teardown, sleep-FIRST so it is inert
    # on startup — app.on_startup fires under aiohttp_client(app) in EVERY test,
    # so it must never broadcast during the suite). Self-contained: app.py is
    # untouched.
    app.on_startup.append(_start_watcher)
    app.on_cleanup.append(_stop_watcher)


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


def _is_print_mode_session(path: Path) -> bool:
    """True if the session was started in print mode (first record = queue-operation).

    Print-mode sessions are non-interactive one-off invocations (e.g. `claude -p`,
    queued prompts). `claude --resume` never lists them, so neither should we —
    in EVERY project bucket, not just the home one. They are detected by their
    first JSONL record having type 'queue-operation'; such a session can still
    contain real user/assistant turns, so _has_real_turn alone won't catch it.
    """
    try:
        with open(path) as fh:
            first_line = fh.readline()
            if not first_line:
                return False
            rec = json.loads(first_line)
            return rec.get("type") == "queue-operation"
    except (OSError, json.JSONDecodeError):
        return False


def _has_real_turn(path: Path) -> bool:
    """True if the session contains at least one actual user or assistant turn.

    Filters out orphaned/empty sessions that were started but never ran — files
    that only hold metadata records (last-prompt, custom-title, agent-name,
    mode, etc.) with no real exchange. Bounded to the first 200 lines so a
    healthy session short-circuits cheaply.
    """
    try:
        with open(path) as fh:
            for i, raw in enumerate(fh):
                if i >= 200:
                    break
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") in ("user", "assistant"):
                    return True
    except OSError:
        return False
    return False


# Title records live near the file ENDS, never the middle: GUI-created sessions
# PREPEND the custom-title at offset 0 (chat.py:_mark_session_interactive),
# user renames APPEND it at EOF (set_session_title), and CLI-written ai-title
# records sit near the file start. So reading a head window + a tail window
# catches every title-writer without ever scanning a multi-MB file end-to-end.
_TITLE_WINDOW_BYTES = 64 * 1024  # bytes read from each of the head and tail
_TITLE_WHOLE_FILE_MAX = 128 * 1024  # files <= this are read whole (no seek)


def _scan_title_records(lines: list[str], custom_title: str | None,
                        ai_title: str | None) -> tuple[str | None, str | None]:
    """Fold title records from a list of JSONL lines into (custom_title, ai_title).

    Preserves last-record-wins: each matching record overwrites the prior value.
    Caller feeds head lines first, then tail lines, so tail records (later in
    file order) correctly win over head records.
    """
    for raw_line in lines:
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        rtype = rec.get("type")
        if rtype == "custom-title":
            custom_title = rec.get("customTitle")
        elif rtype == "ai-title":
            ai_title = rec.get("aiTitle") or rec.get("title")
    return custom_title, ai_title


def _extract_title(path: Path) -> str | None:
    """Extract session title from JSONL using a head+tail windowed read.

    Returns the LAST custom-title if any exist (user renames append new entries,
    GUI creation prepends one), otherwise the last ai-title — matching CLI
    --resume behavior with custom-title taking precedence over ai-title.

    Small files (<= ~128KB) are read whole, preserving the original behavior.
    Larger files read only the first and last 64KB so a 37MB transcript is never
    scanned end-to-end. The head window catches the GUI-prepended title; the tail
    window catches an appended rename. KNOWN LIMITATION (acceptable, not in
    scope): an ai-title buried >64KB from BOTH ends of a huge file would be
    missed — but no code in this repo writes ai-title, and CLI titles sit near
    the file start which the head window always covers.
    """
    custom_title = None
    ai_title = None
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size <= _TITLE_WHOLE_FILE_MAX:
                head_bytes = fh.read()
                tail_bytes = b""
            else:
                head_bytes = fh.read(_TITLE_WINDOW_BYTES)
                fh.seek(-_TITLE_WINDOW_BYTES, os.SEEK_END)
                tail_bytes = fh.read(_TITLE_WINDOW_BYTES)
    except OSError:
        return None

    # Decode with errors='replace' so a multi-byte char split at a window
    # boundary can't raise; parse only COMPLETE lines.
    head_lines = head_bytes.decode("utf-8", errors="replace").splitlines()
    custom_title, ai_title = _scan_title_records(head_lines, custom_title, ai_title)

    if tail_bytes:
        # Drop the first (possibly partial) line of the tail chunk — a 64KB seek
        # from EOF can land mid-line. Whole-file reads have no tail chunk so no
        # complete head line is ever dropped.
        tail_lines = tail_bytes.decode("utf-8", errors="replace").splitlines()[1:]
        custom_title, ai_title = _scan_title_records(tail_lines, custom_title, ai_title)

    return custom_title or ai_title


_project_label_cache: dict[str, str] = {}


def _project_label(dirname: str) -> str:
    """Convert project dir name back to a real filesystem path.

    Claude Code encodes cwds by replacing / with - and prepending -.
    e.g. "$HOME/workspace/projects/oncall-kpi"
      → "-local-home-xulaicao-workspace-projects-oncall-kpi"

    The trick: we can't just replace all - with / because directory names
    contain hyphens. Instead, greedily match path segments against the real
    filesystem starting from /. Results are cached for the process lifetime
    since directories don't typically get renamed while the server is running,
    avoiding hundreds of redundant stat() calls on every page load.
    """
    cached = _project_label_cache.get(dirname)
    if cached is not None:
        return cached

    if not dirname.startswith("-"):
        _project_label_cache[dirname] = dirname
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

    _project_label_cache[dirname] = path
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


def _load_project_urls_config() -> dict:
    """Load project URLs from user config file."""
    config_path = Path(os.environ.get(
        "CLAUDE_WEB_CONFIG",
        Path.home() / ".claude-web" / "config.json",
    ))
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            return data.get("projectUrls", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _app_urls_for_project(project_name: str) -> list[dict]:
    """Return known URLs for a project from user config.

    Config keys are matched as substrings against the project name.
    """
    urls_config = _load_project_urls_config()
    for key, urls in urls_config.items():
        if key in project_name:
            return urls
    return []


def _collect_sop_files(project_path: Path) -> list[dict]:
    """Collect SOP/skill markdown files from known directories in a project.

    Searches (in order):
      - <project>/agent-sops/*.md (and recursive subdirs)
      - <project>/skills/*.md
      - <project>/docs/*.md
    """
    results: list[dict] = []
    seen_paths: set[str] = set()

    # .claude/commands/*.md — project-scoped skills/commands
    commands_dir = project_path / ".claude" / "commands"
    if commands_dir.is_dir():
        for md in sorted(commands_dir.glob("*.md")):
            if md.is_file() and str(md) not in seen_paths:
                seen_paths.add(str(md))
                results.append({"name": md.name, "path": str(md)})

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


def _last_activity_for_project(jsonl_files: list[Path]) -> tuple[float | None, str | None]:
    """Compute the most-recent-activity timestamp for a project.

    Returns (raw_mtime, formatted_label). The raw float timestamp is surfaced to
    the frontend (as lastActivityTs) so it can sort/badge on recency without
    re-parsing the human-readable label; the label preserves the existing
    lastActivity display string. Both are None when the project has no sessions.

    File IO is guarded like the other _collect_* helpers: a session file that
    vanishes or is unreadable mid-scan is skipped rather than failing the listing.
    """
    mtimes: list[float] = []
    for f in jsonl_files:
        try:
            mtimes.append(f.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None, None
    last_mtime = max(mtimes)
    return last_mtime, datetime.fromtimestamp(last_mtime).strftime("%b %d %H:%M")


# Candidate root README filenames, in preference order. Case variants are
# included because filesystems here are case-sensitive and projects vary.
_README_CANDIDATES = (
    "README.md", "README.markdown", "README.mdown", "README.rst",
    "README.txt", "README", "readme.md", "Readme.md",
)


def _read_project_readme(project_path: Path) -> tuple[str | None, str | None]:
    """Return (filename, content) for the project's root README, or (None, None).

    Inlined like CLAUDE.md since a README is a single top-level document. Only
    the project root is checked (not subdirectories) so this stays a cheap
    single-file read per project. A README that vanishes or is unreadable
    mid-scan is treated as absent rather than failing the listing.
    """
    for name in _README_CANDIDATES:
        candidate = project_path / name
        if candidate.is_file():
            try:
                return name, candidate.read_text(encoding="utf-8")
            except OSError:
                return None, None
    return None, None


_DESCRIPTION_MAX = 200


def _first_prose_line(text: str | None) -> str | None:
    """First non-empty prose line of a markdown doc, trimmed to 200 chars.

    Skips blank lines and ATX headings (leading '#') so the description is the
    project's first real sentence, not its title. Returns None when nothing
    qualifies. Used to give the projects LISTING a one-line summary instead of
    inlining the full multi-KB CLAUDE.md/README body.
    """
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:_DESCRIPTION_MAX]
    return None


def _project_description(claude_md: str | None, readme: str | None) -> str | None:
    """One-line description for a project: first prose line of CLAUDE.md, else README."""
    return _first_prose_line(claude_md) or _first_prose_line(readme)


def _collect_memory_files(session_dir: Path | None) -> list[dict]:
    """Collect memory file names from a project's Claude memory directory."""
    if not session_dir or not session_dir.is_dir():
        return []
    memory_dir = session_dir / "memory"
    if not memory_dir.is_dir():
        return []
    return [{"name": md.name} for md in sorted(memory_dir.glob("*.md")) if md.is_file()]


# Matches a GitFarm package remote, e.g.
#   ssh://git.amazon.com/pkg/ClaudeCodeConsole
#   ssh://git.amazon.com:2222/pkg/XiaoGeLao   (host may carry a :port)
#   https://git.amazon.com/pkg/OncallAgent
# and captures the package name. A trailing .git suffix (if present) is stripped.
_GITFARM_PKG_RE = re.compile(r"git\.amazon\.com(?::\d+)?/pkg/(?P<pkg>[^/\s]+?)(?:\.git)?/?$")


def _git_remote_url(project_path: Path) -> str | None:
    """Read the origin remote URL from a git checkout's .git/config, if any.

    `project_path` is the directory that directly contains `.git`. Parses the
    config file directly rather than shelling out to git so the call is cheap
    and side-effect free. Returns None when the directory is not a git repo, has
    no [remote "origin"] section, or the file is unreadable.
    """
    config_path = project_path / ".git" / "config"
    if not config_path.is_file():
        return None
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    in_origin = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_origin = line.replace(" ", "") == '[remote"origin"]'
            continue
        if in_origin and line.startswith("url"):
            _, _, value = line.partition("=")
            value = value.strip()
            if value:
                return value
    return None


def _gitfarm_code_url(remote: str | None) -> str | None:
    """code.amazon.com package URL for a GitFarm remote, else None."""
    if not remote:
        return None
    match = _GITFARM_PKG_RE.search(remote)
    if not match:
        return None
    return f"https://code.amazon.com/packages/{match.group('pkg')}"


# Where to look for git checkouts when the project root itself isn't one.
# Brazil workspaces nest the real package(s) under workspace_<ts>/src/<Pkg>/,
# so a project dir may contain zero, one, or several GitFarm repos. These
# bounded glob patterns cover the common layouts without an expensive deep walk
# (list_projects runs for every project on each page load).
_NESTED_GIT_GLOBS = (
    "*/.git",            # <project>/<repo>/.git
    "src/*/.git",        # <project>/src/<Pkg>/.git
    "*/src/*/.git",      # <project>/workspace_<ts>/src/<Pkg>/.git  (Brazil)
)


def _code_urls_for_project(project_path: Path) -> list[dict]:
    """All GitFarm code.amazon.com repos reachable from a project.

    Checks the project root's own remote first; if the root isn't a GitFarm
    checkout (e.g. a Brazil workspace wrapper), scans a bounded set of nested
    locations for package repos. Returns a deduped list of {name, url}, ordered
    with the root repo (if any) first. Empty when nothing GitFarm-shaped is
    found, so the field is always present.
    """
    found: list[dict] = []
    seen: set[str] = set()

    def add(checkout_dir: Path):
        url = _gitfarm_code_url(_git_remote_url(checkout_dir))
        if url and url not in seen:
            seen.add(url)
            found.append({"name": url.rsplit("/", 1)[-1], "url": url})

    # 1. The project root itself.
    add(project_path)

    # 2. Nested package repos (Brazil workspace layout, etc.).
    for pattern in _NESTED_GIT_GLOBS:
        for git_dir in project_path.glob(pattern):
            if git_dir.is_dir():
                add(git_dir.parent)

    return found


def _project_path_to_claude_slug(project_path: str) -> str:
    """Convert a real project path to the Claude session directory slug.

    Claude Code encodes cwds by replacing / with - and prepending -.
    e.g. "$HOME/workspace/projects/oncall-kpi"
      -> "-local-home-xulaicao-workspace-projects-oncall-kpi"
    """
    return "-" + project_path.lstrip("/").replace("/", "-")


async def get_config(request: web.Request) -> web.Response:
    """Expose environment paths so the frontend doesn't hardcode them.

    The UI previously baked in '/home/xulaicao' and '-local-home-xulaicao'
    slugs, which only worked on the original author's machine. This endpoint
    surfaces the resolved home dir, its session-dir slug, the default chat cwd,
    and the workspace projects (name + real path + slug) so the Chat, Sessions,
    and cwd-picker UIs can be portable.
    """
    home = Path.home().resolve()
    home_slug = _project_path_to_claude_slug(str(home))
    default_cwd = str(request.app["default_cwd"])

    projects = []
    if WORKSPACE_DIR.is_dir():
        for d in sorted(WORKSPACE_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            projects.append({
                "name": d.name,
                "path": str(d),
                "slug": _project_path_to_claude_slug(str(d)),
            })

    return web.json_response({
        "home": str(home),
        "homeSlug": home_slug,
        "defaultCwd": default_cwd,
        "workspaceDir": str(WORKSPACE_DIR),
        "projects": projects,
    })


@dataclass
class _ProjectsCache:
    response: list | None = None
    updated_at: float = 0.0


_PROJECTS_CACHE_TTL = 5.0  # seconds
_projects_cache = _ProjectsCache()
_projects_cache_lock = asyncio.Lock()


def _compute_projects() -> list:
    """Build the projects LISTING (sync). Returns the slim per-project dicts.

    The listing carries a one-line `description` (first prose line of CLAUDE.md,
    else README) instead of the full markdown bodies — the full claudeMd/readme/
    readmeName are fetched on demand via GET /api/projects/{id}/detail. Every
    other field is unchanged. Runs entirely off the event loop via to_thread.
    """
    projects: list = []

    if not WORKSPACE_DIR.is_dir():
        return projects

    # Per-project task counts (keyed by project name). Imported lazily to avoid a
    # circular import (tasks imports sessions._project_label). Failures here must
    # not break the projects listing, so guard defensively.
    try:
        from server.routes.tasks import project_task_counts
        task_counts = project_task_counts()
    except Exception:
        task_counts = {}

    for d in WORKSPACE_DIR.iterdir():
        if not d.is_dir():
            continue
        # Skip hidden directories
        if d.name.startswith("."):
            continue

        project_name = d.name
        project_path = str(d)

        # a. CLAUDE.md / README — read only for the one-line description; the full
        #    bodies ride GET /api/projects/{id}/detail, not the listing.
        claude_md_content = None
        claude_md_path = d / "CLAUDE.md"
        if claude_md_path.is_file():
            try:
                claude_md_content = claude_md_path.read_text(encoding="utf-8")
            except OSError:
                pass
        _, readme_content = _read_project_readme(d)
        description = _project_description(claude_md_content, readme_content)

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

        # f. lastActivity (display label) + lastActivityTs (raw mtime) from most
        #    recent .jsonl mtime. The raw timestamp is surfaced so the frontend can
        #    sort/badge on recency; it doubles as the internal sort key below.
        last_mtime, last_activity = _last_activity_for_project(jsonl_files)

        # g. SOP/skill files in the project directory
        sop_files = _collect_sop_files(d)

        # h. Memory files from Claude's project memory dir
        memory_files = _collect_memory_files(session_dir)

        # i. code.amazon.com package URL(s) derived from git origin remote(s).
        #    Scans the project root and (for Brazil-style wrappers) nested
        #    package repos, so a workspace with multiple repos links them all.
        code_urls = _code_urls_for_project(d)

        # j. task counts (open/total) for this project, by name.
        tc = task_counts.get(project_name, {"total": 0, "open": 0})

        # k. design-decisions summary (.claude/DESIGN.md) + role-agent count.
        #    Lazy import + guard so a parse error here can never break the
        #    projects listing.
        try:
            from server.routes.agents import design_summary, agent_count, unreviewed_entry_total
            design = design_summary(d)
            agents_n = agent_count(d)
            unreviewed_entries = unreviewed_entry_total(d)
        except Exception:
            design = {"hasDesignDoc": False, "proposedDecisionCount": 0, "designDecisionCount": 0}
            agents_n = 0
            unreviewed_entries = 0

        projects.append({
            "id": project_name,
            "name": project_name,
            "path": project_path,
            "sessionCount": session_count,
            "memoryCount": memory_count,
            "taskCount": tc["total"],
            "openTaskCount": tc["open"],
            "lastActivity": last_activity,
            "lastActivityTs": last_mtime,
            # Slim one-liner instead of the full claudeMd/readme/readmeName bodies
            # (those ride /api/projects/{id}/detail). Keeps the listing payload
            # small without losing the at-a-glance summary the cards show.
            "description": description,
            "hasSettings": has_settings,
            "appUrl": app_urls[0]["url"] if app_urls else None,
            "appUrls": app_urls,
            # codeUrls: every GitFarm repo found; codeUrl kept as the first for
            # backward compatibility with any existing consumer.
            "codeUrls": code_urls,
            "codeUrl": code_urls[0]["url"] if code_urls else None,
            "sopFiles": sop_files,
            "memoryFiles": memory_files,
            "hasDesignDoc": design["hasDesignDoc"],
            "proposedDecisionCount": design["proposedDecisionCount"],
            "designDecisionCount": design.get("designDecisionCount", 0),
            "agentCount": agents_n,
            "unreviewedAgentEntries": unreviewed_entries,
        })

    # Sort by lastActivity (most recent first), nulls at the end.
    # lastActivityTs is the raw mtime surfaced above; reuse it as the sort key.
    projects.sort(key=lambda p: (p["lastActivityTs"] is None, -(p["lastActivityTs"] or 0)))

    return projects


async def list_projects(request: web.Request) -> web.Response:
    """List all real projects from WORKSPACE_DIR with Claude Code activity metadata.

    The full crawl runs in a thread (it stats/reads hundreds of files) and is
    cached for a few seconds so back-to-back page loads don't re-walk the tree.
    """
    global _projects_cache
    now = time.monotonic()
    if now - _projects_cache.updated_at < _PROJECTS_CACHE_TTL and _projects_cache.response is not None:
        return web.json_response(_projects_cache.response)

    async with _projects_cache_lock:
        now = time.monotonic()
        if now - _projects_cache.updated_at < _PROJECTS_CACHE_TTL and _projects_cache.response is not None:
            return web.json_response(_projects_cache.response)
        projects = await asyncio.to_thread(_compute_projects)
        _projects_cache = _ProjectsCache(response=projects, updated_at=time.monotonic())
        return web.json_response(projects)


async def get_project_detail(request: web.Request) -> web.Response:
    """Full CLAUDE.md / README bodies for ONE project, fetched on demand.

    Split out of the listing (B2) so the projects list stays lightweight; the
    ProjectsPage README / CLAUDE.md tabs fetch this lazily when a card expands.
    Reuses _read_project_readme and is _validate_project_id-guarded against
    path traversal like the other project endpoints.
    """
    project_id = request.match_info["project_id"]
    _validate_project_id(project_id)

    project_dir = WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    def _read_detail():
        claude_md = None
        claude_md_path = project_dir / "CLAUDE.md"
        if claude_md_path.is_file():
            try:
                claude_md = claude_md_path.read_text(encoding="utf-8")
            except OSError:
                pass
        readme_name, readme_content = _read_project_readme(project_dir)
        return claude_md, readme_name, readme_content

    claude_md, readme_name, readme_content = await asyncio.to_thread(_read_detail)
    return web.json_response({
        "id": project_id,
        "claudeMd": claude_md,
        "readme": readme_content,
        "readmeName": readme_name,
    })


def _validate_project_id(project_id: str) -> None:
    """Reject project ids containing path-traversal characters.

    project_id comes from the URL and is joined as WORKSPACE_DIR / project_id,
    then used to derive write/read paths. Without this guard a URL-encoded
    "..%2f.." escapes the workspace (the segment reaches the handler decoded as
    "../.."), turning the project endpoints into write/read-anywhere primitives.
    """
    if not project_id or ".." in project_id or "/" in project_id or "\\" in project_id:
        raise web.HTTPBadRequest(reason="invalid project id")


async def save_project_claude_md(request: web.Request) -> web.Response:
    """Write content to a project's CLAUDE.md file."""
    project_id = request.match_info["project_id"]
    _validate_project_id(project_id)
    body = await read_json_body(request)
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
    _validate_project_id(project_id)

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
    _validate_project_id(project_id)

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
    _validate_project_id(project_id)

    project_dir = WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    body = await read_json_body(request)
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


def _validate_session_id(session_id: str) -> None:
    """Reject session ids containing path-traversal characters.

    session_id is joined as f'{session_id}.jsonl' against project dirs, so guard
    against traversal for defense in depth (mirrors the filename checks elsewhere).
    """
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise web.HTTPBadRequest(reason="invalid session id")


@dataclass
class _SessionsCache:
    # response keyed by (project, limit, offset) so different pages don't collide.
    responses: dict = field(default_factory=dict)
    updated_at: float = 0.0


_SESSIONS_CACHE_TTL = 4.0  # seconds
_sessions_cache = _SessionsCache()
_sessions_cache_lock = asyncio.Lock()


def _compute_sessions(project: str | None, limit: int, offset: int, home_bucket: str) -> dict:
    """Scan + filter + title-extract for the sessions listing (sync).

    Globs every project dir's *.jsonl, applies the interactive/print-mode/real-turn
    filters, then extracts a title for only the page being returned. Runs entirely
    off the event loop via to_thread — it opens hundreds of files and (via
    _extract_title) reads windows of the page's files.
    """
    if project:
        dirs = [CLAUDE_PROJECTS_BASE / project]
    else:
        dirs = _get_all_project_dirs()

    all_files = []
    for d in dirs:
        if d.is_dir():
            for f in d.glob("*.jsonl"):
                # stat() once here, guarded: the CLI can delete a session file
                # between the glob and the stat. Skip vanished files rather than
                # letting the whole listing fail with a 500. The captured stat is
                # reused below so we never stat the same file twice.
                try:
                    st = f.stat()
                except OSError:
                    continue
                all_files.append((f, d.name, st))

    # Sort by mtime descending using the captured stat.
    all_files.sort(key=lambda x: x[2].st_mtime, reverse=True)

    # Filter logic:
    # - For the home bucket: ALWAYS apply _is_interactive_session to hide print-mode
    #   sessions (regardless of whether accessed via ?project= or all-scan).
    # - For other project dirs: no filter (show everything).

    sessions = []
    matched = 0  # count of files that pass the interactive filter (== total)
    for f, proj_name, stat in all_files:
        if proj_name == home_bucket:
            # Home bucket: always apply interactive filter to hide print-mode sessions
            if not _is_interactive_session(f):
                continue
        # Print-mode sessions (first record = queue-operation) are non-interactive
        # one-off invocations that `claude --resume` never lists. Hide them in EVERY
        # bucket, not just home — a non-home dir (e.g. a Brazil workspace) can hold
        # a print-mode session that still has real user/assistant turns, so the
        # _has_real_turn guard below would otherwise let it leak through.
        elif _is_print_mode_session(f):
            continue
        # Hide orphaned/empty sessions (started but never had a real exchange) in
        # every bucket — these are noise (e.g. an interrupted one-off invocation).
        if not _has_real_turn(f):
            continue
        # This file counts toward the total; its zero-based rank is `matched`.
        rank = matched
        matched += 1
        # Skip files before the requested page, and stop once the page is full.
        # _extract_title reads only head/tail windows, so only do it for the page.
        if rank < offset or len(sessions) >= limit:
            continue
        title = _extract_title(f)
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

    return {"sessions": sessions, "total": matched}


async def list_sessions(request: web.Request) -> web.Response:
    """List sessions from all projects by default, or filtered to a single project.

    - GET /api/sessions (no params) → return sessions from ALL project dirs.
      Apply _is_interactive_session only for the home-cwd bucket (where it's needed
      to match CLI behavior and hide print-mode sessions). For other project dirs,
      include all sessions unconditionally.
    - GET /api/sessions?project=<slug> → return all sessions from that specific dir
      (no interactive filter).

    The scan runs in a thread (it opens hundreds of files) and is cached per
    (project, limit, offset) for a few seconds so paging/back-to-back loads
    don't re-walk the tree.
    """
    global _sessions_cache
    project = request.query.get("project")
    limit = int(request.query.get("limit", "50"))
    offset = int(request.query.get("offset", "0"))

    # Determine the home-cwd bucket name (used to decide where interactive filter applies)
    default_cwd = str(request.app["default_cwd"])
    home_bucket = _cwd_to_project_dir(default_cwd)

    if project:
        # Specific project requested — show all sessions from that dir (no filter).
        # Reject path traversal in the project slug (defense in depth; mirrors the
        # filename checks in get_project_sop/get_project_memory). Validate BEFORE
        # consulting the cache so a malicious slug always 400s.
        if ".." in project or "/" in project or "\\" in project:
            raise web.HTTPBadRequest(reason="invalid project")

    # Cache key includes home_bucket because the filter behavior depends on it
    # (default_cwd is stable per process, but keying on it keeps the cache correct
    # if it ever varies, e.g. across test apps).
    key = (project, limit, offset, home_bucket)

    now = time.monotonic()
    if now - _sessions_cache.updated_at < _SESSIONS_CACHE_TTL and key in _sessions_cache.responses:
        return web.json_response(_sessions_cache.responses[key])

    async with _sessions_cache_lock:
        now = time.monotonic()
        # Drop the whole keyed map when the TTL lapses so a stale page can't be
        # served past the window; double-check the requested key inside the lock.
        if now - _sessions_cache.updated_at >= _SESSIONS_CACHE_TTL:
            _sessions_cache = _SessionsCache(updated_at=now)
        elif key in _sessions_cache.responses:
            return web.json_response(_sessions_cache.responses[key])

        result = await asyncio.to_thread(_compute_sessions, project, limit, offset, home_bucket)
        _sessions_cache.responses[key] = result
        return web.json_response(result)


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
    _validate_session_id(session_id)

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
    # tail=true returns the LAST `limit` records instead of the first ones —
    # the right default for a transcript viewer, which should show the most
    # recent activity. Sessions can be thousands of lines long; showing the
    # head means the recent messages are never visible.
    tail = request.query.get("tail", "").lower() in ("1", "true", "yes")

    # Parse all records (session files are local and at most a few thousand
    # lines, so a full read is cheap and lets us compute total + tail slice).
    records = []
    with open(path) as fh:
        for line in fh:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    total = len(records)
    if tail:
        # Last `limit` records; offset counts backward from the end so the UI
        # can page toward older messages.
        end = total - offset
        start = max(0, end - limit)
        window = records[start:max(start, end)]
    else:
        window = records[offset:offset + limit]

    return web.json_response({
        "messages": window,
        "offset": offset,
        "limit": limit,
        "total": total,
    })


def _find_session_path(session_id: str) -> Path | None:
    """Find the JSONL file for a session across all project dirs."""
    _validate_session_id(session_id)
    for d in _get_all_project_dirs():
        candidate = d / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


async def get_session_title(request: web.Request) -> web.Response:
    """Get the title of a session, plus the cwd it belongs to.

    cwd is the real path the session was created under (decoded from its
    project-dir slug). Resuming a session requires running `claude --resume` in
    that same cwd — resume is cwd-scoped, so a mismatched cwd yields
    "No conversation found".
    """
    session_id = request.match_info["session_id"]
    path = _find_session_path(session_id)
    if not path:
        raise web.HTTPNotFound(reason="session not found")
    title = _extract_title(path)
    cwd = _project_label(path.parent.name)
    return web.json_response({"sessionId": session_id, "title": title or "", "cwd": cwd})


async def set_session_title(request: web.Request) -> web.Response:
    """Set/update the session title by appending a custom-title entry to the JSONL."""
    session_id = request.match_info["session_id"]
    path = _find_session_path(session_id)
    if not path:
        raise web.HTTPNotFound(reason="session not found")

    body = await read_json_body(request)
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
    _validate_session_id(session_id)

    # Search all project dirs for the session file
    for d in _get_all_project_dirs():
        candidate = d / f"{session_id}.jsonl"
        if candidate.exists():
            candidate.unlink()
            return web.json_response({"deleted": session_id})

    raise web.HTTPNotFound(reason="session not found")


# ── Session-transcript file-watcher (E2) ─────────────────────────────────────

SESSION_WATCH_INTERVAL_S = 5


def _newest_transcript_mtime() -> int | None:
    """Newest mtime_ns across every transcript under CLAUDE_PROJECTS_BASE.

    The actively-written (live) session is almost always the newest .jsonl, so
    its mtime advancing is a cheap proxy for "a transcript changed" without
    re-statting all ~thousands of files individually against a remembered map.
    Runs OFF the event loop (via asyncio.to_thread). Returns None — never raises
    — when the base is missing or every file vanishes mid-scan, so one unreadable
    file can't kill the watcher loop.
    """
    try:
        return max(
            (p.stat().st_mtime_ns for p in CLAUDE_PROJECTS_BASE.rglob("*.jsonl")),
            default=None,
        )
    except OSError:
        return None


async def _session_watcher(app) -> None:
    """Broadcast 'session_changed' when the newest transcript's mtime advances.

    Every SESSION_WATCH_INTERVAL_S it computes the newest transcript mtime_ns
    (off the event loop) and, when that value STRICTLY advances past the last
    seen one, broadcasts 'session_changed' over the existing ws manager so any
    open transcript repaints near-instantly instead of waiting for the client's
    backstop poll.

    The baseline is seeded on the FIRST tick (a `seeded` flag) so startup emits
    NO broadcast — critical because app.on_startup fires under aiohttp_client(app)
    in EVERY test. It mirrors slack.py:_slack_watcher's robustness: it sleeps
    FIRST (inert on startup), CancelledError propagates so shutdown can
    cancel+await cleanly, and a per-iteration try/except catches+logs everything
    else so one bad stat never kills the loop. It spawns no subprocess.
    """
    last_mtime: int | None = None
    seeded = False
    while True:
        try:
            await asyncio.sleep(SESSION_WATCH_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        try:
            newest = await asyncio.to_thread(_newest_transcript_mtime)
            if not seeded:
                # First tick seeds the baseline — no broadcast on startup.
                last_mtime = newest
                seeded = True
            elif newest is not None and (last_mtime is None or newest > last_mtime):
                last_mtime = newest
                await app["ws_manager"].broadcast("session_changed", {"watched": True})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one bad stat must not kill the loop
            logger.warning("Session file-watcher iteration failed: %s", e)


async def _start_watcher(app) -> None:
    """on_startup: launch the session-transcript file-watcher task (idempotent)."""
    task = app.get("session_watch_task")
    if task is None or task.done():
        app["session_watch_task"] = asyncio.create_task(_session_watcher(app))


async def _stop_watcher(app) -> None:
    """on_cleanup: cancel AND await the watcher so it tears down cleanly.

    Mirrors slack.py:_stop_watcher (the shape that keeps the suite warning-free).
    The watcher holds no subprocess, so there is only the task to cancel and await.
    """
    task = app.pop("session_watch_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
