"""Per-project Role Agents framework — backend.

Phase 1 (this file's current scope): the project **design decisions** record,
`.claude/DESIGN.md`, an append-only ADR (Architecture Decision Record) log that
Xulai reviews on the Projects tab. Read-only here — the editable Accept/Reject
flow and the Agents/context endpoints arrive in later phases (see
`.claude/design/agent-framework-design.md`, §6a / §8).

Routes are mounted under the existing /api/projects/{project_id} namespace and
reuse the same path-traversal guard (`sessions._validate_project_id`) and
`filestore` helpers as the rest of the project endpoints.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from aiohttp import web

from server import filestore
from server.routes import read_json_body
# Import the module (not the names) so tests that monkeypatch
# sessions.WORKSPACE_DIR / _validate_project_id are honored at call time.
from server.routes import sessions
from server.routes.skills import _parse_frontmatter


# Global ("universal") agents live in ~/.claude/agents/ — the same place Claude
# Code reads user-level subagents from. They are visible in every project. A
# project agent can be promoted here ("tag to global") and a global one demoted
# back into a project. Module global so tests can monkeypatch it.
GLOBAL_AGENTS_DIR = Path.home() / ".claude" / "agents"


def register(app: web.Application):
    app.router.add_get("/api/projects/{project_id}/agents", list_agents)
    app.router.add_get("/api/projects/{project_id}/agents/{name}", get_agent)
    # Scope: promote a project agent to global (universal) or demote back.
    app.router.add_post("/api/projects/{project_id}/agents/{name}/scope", set_agent_scope)
    # Phase 3: append-only context layer + conflict-aware review.
    app.router.add_get("/api/projects/{project_id}/agents/{name}/context", get_agent_context)
    app.router.add_get("/api/projects/{project_id}/agents/{name}/context/conflicts", get_context_conflicts)
    app.router.add_post("/api/projects/{project_id}/agents/{name}/context/reconcile", reconcile_context)
    app.router.add_post("/api/projects/{project_id}/agents/{name}/context/mark-reviewed", mark_context_reviewed)
    # Phase 3: editable design doc (Accept/Reject proposed ADRs).
    app.router.add_get("/api/projects/{project_id}/design", get_project_design)
    app.router.add_put("/api/projects/{project_id}/design", put_project_design)


# ── Role agents (.claude/agents/*.md) — Phase 2 ──────────────────────────────
#
# Roles ARE native Claude Code subagents: each is a `.claude/agents/<name>.md`
# file with YAML frontmatter (name, description, tools, model) and a body that
# is the system prompt. Phase 2 is read-only: list them + show one. The
# append-only context layer (agent-context/<role>.md) and its conflict-review
# arrive in Phase 3.


def _validate_agent_name(name: str) -> None:
    """Reject agent/role names containing path-traversal characters.

    Mirrors sessions._validate_project_id — the name comes from the URL and is
    joined as <project>/.claude/agents/<name>.md.
    """
    if not name or ".." in name or "/" in name or "\\" in name:
        raise web.HTTPBadRequest(reason="invalid agent name")


def _agents_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "agents"


def _tools_list(meta: dict) -> list[str]:
    """Normalize the frontmatter `tools` field into a list.

    Claude Code subagents write tools as a comma-separated string
    (`tools: Read, Edit, Bash`). An absent field means "inherit all tools".
    """
    raw = meta.get("tools", "")
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _context_dir(scope_root: Path) -> Path:
    """The agent-context dir for a given scope root (project dir or global)."""
    return scope_root / "agent-context" if scope_root == GLOBAL_AGENTS_DIR.parent else scope_root / ".claude" / "agent-context"


def _agent_record(agent_file: Path, project_dir: Path, scope: str) -> dict | None:
    """Build one agent dict, or None if the file is unreadable.

    `scope` is "project" or "global". Context review signals are always computed
    against the PROJECT's context dir for project agents; global agents carry no
    per-project context (their review happens project-agnostically), so their
    signals are zeroed here — they're surfaced as universal, read-mostly roles.
    """
    try:
        content = agent_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta = _parse_frontmatter(content)
    slug = agent_file.stem
    if scope == "project":
        context_path = project_dir / ".claude" / "agent-context" / agent_file.name
        summary = context_summary(project_dir, slug)
    else:
        context_path = GLOBAL_AGENTS_DIR.parent / "agent-context" / agent_file.name
        summary = {"entryCount": 0, "newEntryCount": 0, "conflictClusterCount": 0}
    try:
        context_bytes = context_path.stat().st_size if context_path.is_file() else 0
    except OSError:
        context_bytes = 0
    return {
        "name": meta.get("name", slug),
        "description": meta.get("description", ""),
        "model": meta.get("model", ""),
        "tools": _tools_list(meta),
        "scope": scope,
        "contextExists": context_path.is_file(),
        "contextBytes": context_bytes,
        "contextEntryCount": summary["entryCount"],
        "newEntryCount": summary["newEntryCount"],
        "conflictClusterCount": summary["conflictClusterCount"],
        "path": str(agent_file),
        "contextPath": str(context_path),
        "slug": slug,
    }


def _list_agents(project_dir: Path) -> list[dict]:
    """Parse a project's role agents + the global (universal) agents.

    Project agents come first, then global ones not shadowed by a same-named
    project agent. Never raises — an unreadable agent file is skipped.
    """
    out: list[dict] = []
    seen: set[str] = set()
    agents_dir = _agents_dir(project_dir)
    if agents_dir.is_dir():
        for f in sorted(agents_dir.glob("*.md")):
            rec = _agent_record(f, project_dir, "project")
            if rec:
                out.append(rec)
                seen.add(rec["slug"])
    if GLOBAL_AGENTS_DIR.is_dir():
        for f in sorted(GLOBAL_AGENTS_DIR.glob("*.md")):
            if f.stem in seen:
                continue  # a project agent of the same name takes precedence
            rec = _agent_record(f, project_dir, "global")
            if rec:
                out.append(rec)
    return out


def agent_count(project_dir: Path) -> int:
    """Count role agents visible to a project = project agents + global agents
    not shadowed by a same-named project agent. Never raises.
    """
    try:
        names: set[str] = set()
        agents_dir = _agents_dir(project_dir)
        if agents_dir.is_dir():
            names.update(f.stem for f in agents_dir.glob("*.md"))
        if GLOBAL_AGENTS_DIR.is_dir():
            names.update(f.stem for f in GLOBAL_AGENTS_DIR.glob("*.md"))
        return len(names)
    except OSError:
        return 0


# ── Append-only context layer (.claude/agent-context/<role>.md) — Phase 3 ────
#
# The context file is an append-only list of dated bullet entries:
#
#     # backend-dev — project context
#     - 2026-06-01: Use read_json_body for body parsing. (B12)
#     - 2026-06-13: Supersedes the 2026-06-01 note — actually use X. (correction)
#
# The agent only ever APPENDS. The only way entries are removed is the reconcile
# endpoint (a human action). Two review signals:
#   - newEntryCount: entries appended since the last "mark reviewed" marker.
#   - conflictClusterCount: groups of entries that overlap/contradict (v1 heuristic).

# A context entry: a top-level "- " bullet, optionally beginning with a date.
_ENTRY_RE = re.compile(r"^-\s+(?:(?P<date>\d{4}-\d{2}-\d{2}):\s*)?(?P<text>.*)$")
# Stopwords excluded from lexical-overlap clustering — common English + words
# that appear in nearly every note and would over-cluster.
_STOPWORDS = frozenset("""
a an the and or but if then else of to in on at for with by from as is are was were be been
being it its this that these those use used using uses note when where which who what how why
not no do does done can could should would may might will shall must has have had instead
""".split())
# Tokens worth clustering on: words >=3 chars, plus dotted/underscored identifiers
# (read_json_body, filestore.write_text) which are the strongest topic signal.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
_SUPERSEDE_RE = re.compile(r"supersed|correct|replaces?\b|instead of", re.IGNORECASE)


def parse_context_entries(content: str) -> list[dict]:
    """Parse an append-only context file into entries in document order.

    Returns [{ "index", "date" | None, "text", "raw" }]. The leading `# Title`
    heading and any non-bullet lines are ignored. A multi-line bullet (wrapped
    continuation lines) folds its continuation into the entry text.
    """
    entries: list[dict] = []
    cur: dict | None = None
    for line in content.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            cur = {
                "index": len(entries),
                "date": m.group("date"),
                "text": m.group("text").strip(),
                "raw": line,
            }
            entries.append(cur)
        elif cur is not None and line.strip() and (line.startswith("  ") or line.startswith("\t")):
            # Continuation of the current bullet.
            cur["text"] = (cur["text"] + " " + line.strip()).strip()
            cur["raw"] = cur["raw"] + "\n" + line
    return entries


def _tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS
    }


def detect_conflicts(entries: list[dict], dismissed_keys: set[str] | None = None) -> list[dict]:
    """Group entries that overlap or supersede each other (v1, pure-Python).

    Two signals, no LLM:
      1. An explicit supersede/correction marker in an entry's text links it to
         every other entry it shares a key token with (a guaranteed cluster).
      2. Strong lexical overlap: entries sharing >=2 key tokens (or >=1 dotted
         identifier) are candidates for redundancy.

    Returns clusters: [{ "reason", "entryIndices": [...] }] where reason is
    "superseded" (an explicit marker is present) or "redundant". Singletons are
    not returned. Clusters are merged transitively (A~B, B~C → {A,B,C}).

    `dismissed_keys`: content-hash keys of clusters the user chose "Keep all" on
    — those are filtered out so they don't re-flag every reload.
    """
    n = len(entries)
    if n < 2:
        return []
    toks = [_tokens(e["text"]) for e in entries]
    has_marker = [bool(_SUPERSEDE_RE.search(e["text"])) for e in entries]

    # Union-find over entries that are "related".
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    marker_pairs: set[frozenset] = set()
    for i in range(n):
        for j in range(i + 1, n):
            shared = toks[i] & toks[j]
            dotted = any("." in s or "_" in s for s in shared)
            related = len(shared) >= 2 or dotted
            if related:
                union(i, j)
                if has_marker[i] or has_marker[j]:
                    marker_pairs.add(frozenset((i, j)))

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort()
        # Skip clusters the user has explicitly dismissed ("Keep all"). The key
        # is content-based so it survives entry reordering/index shifts.
        if dismissed_keys is not None and _cluster_key([entries[i]["text"] for i in members]) in dismissed_keys:
            continue
        reason = "superseded" if any(
            has_marker[i] for i in members
        ) else "redundant"
        clusters.append({"reason": reason, "entryIndices": members})
    clusters.sort(key=lambda c: c["entryIndices"][0])
    return clusters


def _cluster_key(texts: list[str]) -> str:
    """Stable content hash for a cluster, independent of entry order/index.

    Used to remember a dismissed ("keep all") cluster so it isn't re-flagged on
    every reload. Normalizes (strip + lowercase + collapse whitespace) so trivial
    differences don't change the key.
    """
    norm = sorted(re.sub(r"\s+", " ", t).strip().lower() for t in texts)
    return hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()[:16]


def _context_path(project_dir: Path, name: str) -> Path:
    return project_dir / ".claude" / "agent-context" / f"{name}.md"


def _review_marker_path(project_dir: Path, name: str) -> Path:
    """Sibling marker storing the content hash + entry count at last review.

    Lives under .claude/agent-context/.reviewed/ (gitignored like all of
    .claude/). Absence means "never reviewed" → every entry is new.
    """
    return project_dir / ".claude" / "agent-context" / ".reviewed" / f"{name}.txt"


def _read_review_marker(project_dir: Path, name: str) -> int:
    """Return the entry count recorded at last review (0 if never reviewed)."""
    try:
        raw = _review_marker_path(project_dir, name).read_text(encoding="utf-8").strip()
        return int(raw.split(":", 1)[0])
    except (OSError, ValueError):
        return 0


def context_summary(project_dir: Path, name: str) -> dict:
    """{entryCount, newEntryCount, conflictClusterCount} for a role's context.

    Never raises.
    """
    try:
        content, _ = filestore.read_text(_context_path(project_dir, name))
        entries = parse_context_entries(content)
        reviewed = _read_review_marker(project_dir, name)
        new_count = max(0, len(entries) - reviewed)
        dismissed = _read_dismissed_keys(project_dir, name)
        return {
            "entryCount": len(entries),
            "newEntryCount": new_count,
            "conflictClusterCount": len(detect_conflicts(entries, dismissed)),
        }
    except Exception:
        return {"entryCount": 0, "newEntryCount": 0, "conflictClusterCount": 0}


def unreviewed_entry_total(project_dir: Path) -> int:
    """Sum of new-since-review entries across all of a project's agents."""
    try:
        agents_dir = _agents_dir(project_dir)
        if not agents_dir.is_dir():
            return 0
        return sum(
            context_summary(project_dir, f.stem)["newEntryCount"]
            for f in agents_dir.glob("*.md")
        )
    except OSError:
        return 0


# ── ADR parsing ──────────────────────────────────────────────────────────────
#
# DESIGN.md is a sequence of ADR entries. Each starts with a header line:
#
#     ## D-003: Some title  (2026-06-13, accepted)
#
# followed by free-form markdown body until the next `## ` header or EOF. The
# trailing `(date, status)` is optional metadata; a header without it still
# parses (status/date come back as None) so a hand-started doc isn't rejected.

_ADR_HEADER_RE = re.compile(
    r"^##\s+(?P<id>D-\d+):\s*(?P<title>.*?)"
    r"(?:\s*\((?P<date>\d{4}-\d{2}-\d{2}),\s*(?P<status>[A-Za-z]+)\))?\s*$"
)


def parse_adrs(content: str) -> list[dict]:
    """Parse DESIGN.md into a list of ADR entries.

    Returns entries in document order:
        [{ "id", "title", "date" | None, "status" | None, "body" }]
    Lines before the first `## D-NNN:` header (e.g. the `# Title` heading) are
    ignored. A file with no ADR headers yields an empty list.
    """
    entries: list[dict] = []
    current: dict | None = None
    body_lines: list[str] = []

    def _flush():
        if current is not None:
            current["body"] = "\n".join(body_lines).strip()
            entries.append(current)

    for line in content.splitlines():
        m = _ADR_HEADER_RE.match(line)
        if m:
            _flush()
            current = {
                "id": m.group("id"),
                "title": m.group("title").strip(),
                "date": m.group("date"),
                "status": (m.group("status").lower() if m.group("status") else None),
            }
            body_lines = []
        elif current is not None:
            body_lines.append(line)
    _flush()
    return entries


def design_summary(project_dir: Path) -> dict:
    """Cheap summary of a project's DESIGN.md for the projects listing.

    Returns {"hasDesignDoc": bool, "proposedDecisionCount": int}. Never raises —
    a missing or unreadable file degrades to {False, 0} (callers wrap in their
    own try/except too, but this keeps the listing robust on its own).
    """
    path = project_dir / ".claude" / "DESIGN.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"hasDesignDoc": False, "proposedDecisionCount": 0, "designDecisionCount": 0}
    adrs = parse_adrs(content)
    proposed = sum(1 for a in adrs if a["status"] == "proposed")
    return {
        "hasDesignDoc": True,
        "proposedDecisionCount": proposed,
        "designDecisionCount": len(adrs),
    }


# ── Endpoints ────────────────────────────────────────────────────────────────


async def list_agents(request: web.Request) -> web.Response:
    """List a project's role agents (.claude/agents/*.md)."""
    project_id = request.match_info["project_id"]
    sessions._validate_project_id(project_id)

    project_dir = sessions.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    return web.json_response(_list_agents(project_dir))


def _resolve_agent_file(project_dir: Path, name: str) -> tuple[Path | None, str]:
    """Find an agent .md by name, project scope first then global.

    Returns (path, scope). path is None if not found in either scope.
    """
    proj = _agents_dir(project_dir) / f"{name}.md"
    if proj.is_file():
        return proj, "project"
    glob = GLOBAL_AGENTS_DIR / f"{name}.md"
    if glob.is_file():
        return glob, "global"
    return None, ""


async def get_agent(request: web.Request) -> web.Response:
    """Return a single role agent's .md content + etag + parsed frontmatter."""
    project_id = request.match_info["project_id"]
    name = request.match_info["name"]
    sessions._validate_project_id(project_id)
    _validate_agent_name(name)

    project_dir = sessions.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    agent_file, scope = _resolve_agent_file(project_dir, name)
    if agent_file is None:
        raise web.HTTPNotFound(reason=f"agent {name} not found")

    content, etag = filestore.read_text(agent_file)
    meta = _parse_frontmatter(content)
    return web.json_response({
        "scope": scope,
        "name": meta.get("name", name),
        "description": meta.get("description", ""),
        "model": meta.get("model", ""),
        "tools": _tools_list(meta),
        "content": content,
        "etag": etag,
        "path": str(agent_file),
    })


async def set_agent_scope(request: web.Request) -> web.Response:
    """Move an agent between project and global ("universal") scope.

    Body: { scope: "project" | "global" }.
      - "global": promote the project agent to ~/.claude/agents/ so it's
        available in every project.
      - "project": demote a global agent into THIS project's .claude/agents/.

    The .md file is moved (read → write target → delete source). Refuses if a
    same-named agent already exists at the destination (no silent overwrite).
    """
    project_id = request.match_info["project_id"]
    name = request.match_info["name"]
    sessions._validate_project_id(project_id)
    _validate_agent_name(name)

    project_dir = sessions.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    body = await read_json_body(request)
    target_scope = body.get("scope")
    if target_scope not in ("project", "global"):
        raise web.HTTPBadRequest(reason="scope must be 'project' or 'global'")

    src, src_scope = _resolve_agent_file(project_dir, name)
    if src is None:
        raise web.HTTPNotFound(reason=f"agent {name} not found")
    if src_scope == target_scope:
        return web.json_response({"ok": True, "scope": target_scope, "moved": False})

    if target_scope == "global":
        dest = GLOBAL_AGENTS_DIR / f"{name}.md"
    else:
        dest = _agents_dir(project_dir) / f"{name}.md"
    if dest.exists():
        raise web.HTTPConflict(reason=f"an agent named {name} already exists in {target_scope} scope")

    content, _ = filestore.read_text(src)
    filestore.write_text(dest, content)  # creates parent dirs
    filestore.delete_file(src)
    return web.json_response({"ok": True, "scope": target_scope, "moved": True})


# ── Phase 3 context endpoints ────────────────────────────────────────────────


def _resolve_context(request: web.Request) -> tuple[Path, str]:
    """Validate the project_id + name from the URL and return (project_dir, name).

    `name` is the role slug (the agent .md filename stem), which is also the
    context filename stem. Raises 400 on traversal, 404 if the project is gone.
    """
    project_id = request.match_info["project_id"]
    name = request.match_info["name"]
    sessions._validate_project_id(project_id)
    _validate_agent_name(name)
    project_dir = sessions.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")
    return project_dir, name


async def get_agent_context(request: web.Request) -> web.Response:
    """Return a role's append-only context: parsed entries + raw content + etag.

    An absent context file is a valid empty state ({exists: False, entries: []}).
    """
    project_dir, name = _resolve_context(request)
    path = _context_path(project_dir, name)
    content, etag = filestore.read_text(path)
    entries = parse_context_entries(content)
    reviewed = _read_review_marker(project_dir, name)
    return web.json_response({
        "exists": path.is_file(),
        "content": content,
        "etag": etag,
        "entries": entries,
        "lastReviewedCount": reviewed,
        "newEntryCount": max(0, len(entries) - reviewed),
        "path": str(path),
    })


async def get_context_conflicts(request: web.Request) -> web.Response:
    """Return detected conflict/redundancy clusters for a role's context.

    Each cluster: { reason, entryIndices: [...], entries: [{index,date,text}] }.
    The embedded entries save the UI a second fetch to render the cluster.
    """
    project_dir, name = _resolve_context(request)
    content, _ = filestore.read_text(_context_path(project_dir, name))
    entries = parse_context_entries(content)
    clusters = detect_conflicts(entries, _read_dismissed_keys(project_dir, name))
    for c in clusters:
        c["entries"] = [
            {"index": i, "date": entries[i]["date"], "text": entries[i]["text"]}
            for i in c["entryIndices"]
        ]
    return web.json_response({"clusters": clusters})


def _render_entries(entries: list[dict]) -> str:
    """Re-serialize entries back to append-only bullet lines (preserving raw)."""
    return "\n".join(e["raw"] for e in entries)


async def reconcile_context(request: web.Request) -> web.Response:
    """Apply a reconciliation. The ONLY endpoint that removes context entries.

    Body: { action: "keep"|"merge"|"dismiss", entryIndices: [...],
            mergedText?, etag }
      - keep:    keep the FIRST index in entryIndices, drop the rest.
      - merge:   replace the cluster with one new entry (mergedText) at the
                 position of the first index.
      - dismiss: remove nothing (the cluster isn't actually redundant); just
                 acknowledged. Returned so the UI can stop flagging it.

    Etag-guarded (409 on concurrent external edit), preserving the file's
    non-entry lines (the `# Title` heading etc.).
    """
    project_dir, name = _resolve_context(request)
    body = await read_json_body(request)
    action = body.get("action")
    if action not in ("keep", "merge", "dismiss"):
        raise web.HTTPBadRequest(reason="action must be keep|merge|dismiss")
    indices = body.get("entryIndices")
    if not isinstance(indices, list) or not indices:
        raise web.HTTPBadRequest(reason="entryIndices must be a non-empty list")
    expected_etag = body.get("etag")

    path = _context_path(project_dir, name)
    content, _ = filestore.read_text(path)
    lines = content.splitlines()
    entries = parse_context_entries(content)
    valid = {e["index"] for e in entries}
    if not set(indices) <= valid:
        raise web.HTTPBadRequest(reason="entryIndices out of range")

    if action == "dismiss":
        # "Keep all": remove nothing, but remember this cluster (by content hash)
        # so detection won't re-flag it on the next load.
        key = _cluster_key([e["text"] for e in entries if e["index"] in set(indices)])
        _add_dismissed_key(project_dir, name, key)
        return web.json_response({"ok": True, "action": "dismiss", "removed": 0})

    keep_first = min(indices)
    drop = set(indices)
    if action == "keep":
        drop.discard(keep_first)
    elif action == "merge":
        merged_text = (body.get("mergedText") or "").strip()
        if not merged_text:
            raise web.HTTPBadRequest(reason="mergedText required for merge")
        # Rewrite the first entry's line to the merged text (keep its date if any),
        # then drop the others.
        e0 = next(e for e in entries if e["index"] == keep_first)
        prefix = f"- {e0['date']}: " if e0["date"] else "- "
        entries[keep_first]["raw"] = prefix + merged_text
        drop.discard(keep_first)

    # Rebuild the file: keep every non-entry line in place; for entry lines, emit
    # the (possibly-rewritten) entry unless it's being dropped.
    kept_entries = [e for e in entries if e["index"] not in drop]
    # Heading / preamble = lines before the first entry's first raw line.
    first_entry_line = lines.index(entries[0]["raw"].split("\n")[0]) if entries else len(lines)
    head = "\n".join(lines[:first_entry_line]).rstrip()
    new_content = (head + "\n" + _render_entries(kept_entries)).strip() + "\n"

    try:
        new_etag = filestore.write_text(path, new_content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(path)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    # Reconciliation changed entry count → reset the review marker to the new
    # total (these entries have just been reviewed by the human reconciling them).
    _write_review_marker(project_dir, name, len(kept_entries), new_content)
    return web.json_response({"ok": True, "action": action, "removed": len(drop), "etag": new_etag})


def _write_review_marker(project_dir: Path, name: str, count: int, content: str) -> None:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    path = _review_marker_path(project_dir, name)
    filestore.write_text(path, f"{count}:{digest}\n")


def _dismissed_path(project_dir: Path, name: str) -> Path:
    """Sidecar recording cluster keys the user chose "Keep all" on.

    One key per line, under .claude/agent-context/.reviewed/ (gitignored).
    """
    return project_dir / ".claude" / "agent-context" / ".reviewed" / f"{name}.dismissed"


def _read_dismissed_keys(project_dir: Path, name: str) -> set[str]:
    try:
        raw = _dismissed_path(project_dir, name).read_text(encoding="utf-8")
        return {ln.strip() for ln in raw.splitlines() if ln.strip()}
    except OSError:
        return set()


def _add_dismissed_key(project_dir: Path, name: str, key: str) -> None:
    keys = _read_dismissed_keys(project_dir, name)
    keys.add(key)
    filestore.write_text(_dismissed_path(project_dir, name), "\n".join(sorted(keys)) + "\n")


async def mark_context_reviewed(request: web.Request) -> web.Response:
    """Advance the last-reviewed marker to the current entry count.

    Clears the "N new entries" signal without removing anything.
    """
    project_dir, name = _resolve_context(request)
    content, _ = filestore.read_text(_context_path(project_dir, name))
    entries = parse_context_entries(content)
    _write_review_marker(project_dir, name, len(entries), content)
    return web.json_response({"ok": True, "reviewedCount": len(entries)})


async def get_project_design(request: web.Request) -> web.Response:
    """Return a project's .claude/DESIGN.md content + etag + parsed ADR entries.

    404 only if the project directory itself is missing; an absent DESIGN.md is
    a valid empty state ({exists: False, content: "", decisions: []}) so the UI
    can show a "no design decisions yet" view rather than an error.
    """
    project_id = request.match_info["project_id"]
    sessions._validate_project_id(project_id)

    project_dir = sessions.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    design_path = project_dir / ".claude" / "DESIGN.md"
    content, etag = filestore.read_text(design_path)
    return web.json_response({
        "exists": design_path.is_file(),
        "content": content,
        "etag": etag,
        "decisions": parse_adrs(content),
        "path": str(design_path),
    })


async def put_project_design(request: web.Request) -> web.Response:
    """Save a project's .claude/DESIGN.md (etag-guarded).

    Used by the Accept/Reject flow on proposed ADRs (the frontend flips a
    `proposed` status to `accepted` or removes the entry, then PUTs the result)
    and for direct editing. Creates the file on first write.
    """
    project_id = request.match_info["project_id"]
    sessions._validate_project_id(project_id)

    project_dir = sessions.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise web.HTTPNotFound(reason="project directory not found")

    body = await read_json_body(request)
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(reason="content field required")
    expected_etag = body.get("etag")

    design_path = project_dir / ".claude" / "DESIGN.md"
    try:
        new_etag = filestore.write_text(design_path, content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(design_path)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )
    return web.json_response({
        "ok": True,
        "etag": new_etag,
        "decisions": parse_adrs(content),
    })
