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

import asyncio
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
    app.router.add_get("/api/projects/{project_id}/agents/{name}/context/ephemeral", get_context_ephemeral)
    app.router.add_post("/api/projects/{project_id}/agents/{name}/context/reconcile", reconcile_context)
    app.router.add_post("/api/projects/{project_id}/agents/{name}/context/mark-reviewed", mark_context_reviewed)
    app.router.add_post("/api/projects/{project_id}/agents/{name}/context/oversize-ack", acknowledge_oversize)
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
        summary = {
            "entryCount": 0, "newEntryCount": 0, "conflictClusterCount": 0,
            "oversized": False, "oversizeActionable": False, "oversizeAcknowledged": False,
        }
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
        "oversized": summary.get("oversized", False),
        "oversizeActionable": summary.get("oversizeActionable", False),
        "oversizeAcknowledged": summary.get("oversizeAcknowledged", False),
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


# ── Listing thread offload (B4) ──────────────────────────────────────────────
#
# `_list_agents` reads every agent .md AND runs `context_summary` per agent,
# which calls the O(N^2) `detect_conflicts` over each context file — that work is
# synchronous and CPU-and-IO heavy, and was blocking the event loop on every
# /api/projects/{id}/agents request. Run it off-thread via `asyncio.to_thread`
# so the loop stays responsive (the usage.py `asyncio.to_thread` reference).
#
# ACCURACY NOTE: the full `context_summary` (including `detect_conflicts`) still
# runs here — the offload only changes WHERE it runs, not WHAT it computes. So
# every list row keeps a true `conflictClusterCount`/`newEntryCount`/oversize
# signal; we never zero or defer the conflict-cluster count (the AgentsPage list
# badge depends on it — DEFERRED-COUNT-AS-ZERO trap). No TTL cache is added: the
# agent auto-APPENDS to its context file out-of-band at task end, so any
# time-windowed cache would make the "N new entries" / conflict badge lie for the
# window after each task — ACCURACY OVER SPEED, and the per-project scan is cheap
# (a handful of files) unlike the global usage scan that warrants caching.


async def _list_agents_async(project_dir: Path) -> list[dict]:
    """Off-thread `_list_agents` so the per-agent context scan + conflict
    detection never blocks the event loop. Never raises (delegates to the
    never-raising `_list_agents`)."""
    return await asyncio.to_thread(_list_agents, project_dir)


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

# Clustering tuning (see spec item 1). Pure heuristic, deterministic, no LLM.
# The linking rule is deliberately CONSERVATIVE: on a real file of distinct-but-
# related decisions about one subsystem (e.g. the Slack timeline D-010/D-014/
# D-017/D-018, each mentioning shared identifiers like `register(app)` or
# `slack_threads.json`) it yields ZERO clusters. Only a genuine duplicate pair or
# an explicit correction pair clusters — and tightly (usually 2 entries).
#   - _DF_FRACTION_CAP: a token appearing in more than this fraction of entries is
#     "ubiquitous" (test/etag/context on a real file) and is dropped before
#     linking, so it can't bind everything together. The remainder is each entry's
#     discriminative vocabulary.
#   - _OVERLAP_RATIO_MIN: two entries link only when the fraction of the smaller
#     entry's discriminative vocabulary that is SHARED with the other meets this
#     threshold — i.e. they are genuinely near-duplicate, not merely co-mentioning
#     one identifier. (A single shared identifier no longer links anything; that
#     shortcut was the mega-cluster root cause.)
#   - _OVERLAP_MIN_DENOM: a floor on the smaller discriminative-token count so a
#     1-token-vs-2-token coincidence can't trivially hit a high ratio.
#   - Explicit dated supersede: an entry carrying a _SUPERSEDE_RE marker plus a
#     referenced YYYY-MM-DD that matches ANOTHER entry's date (and at least one
#     shared discriminative token) links specifically to that entry, labelled
#     "superseded".
#   - _MAX_CLUSTER_SIZE stays a backstop only; with the tight rule clusters are
#     naturally ~2 entries, so the cap should rarely be the thing doing the work.
_DF_FRACTION_CAP = 0.4
# Tuned against the live qa.md (407 entries) + backend-dev.md/frontend-dev.md
# timelines: 0.60 still linked one borderline distinct-but-related pair (a
# launcher-env note vs a dedupe-rule note sharing the dedupe vocabulary at
# exactly 0.60); 0.65 drops it while keeping the genuine ceremony-repetition
# near-duplicates (which sit at 0.68–0.71). The two distinct-but-related
# decision timelines yield ZERO clusters at every threshold.
_OVERLAP_RATIO_MIN = 0.65
_OVERLAP_MIN_DENOM = 3
_MAX_CLUSTER_SIZE = 5
# A YYYY-MM-DD date reference inside an entry's body (used to bind a dated
# supersede marker to the specific entry it corrects).
_DATE_REF_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Size-ceiling nudge (design §4.4): a context file past these bounds is flagged
# for reconciliation even with zero detected conflicts.
_OVERSIZE_BYTES = 6 * 1024
_OVERSIZE_LINES = 400
# An oversize acknowledgement ("I know it's honestly large") goes stale once the
# file grows materially past the acknowledged size — so a future genuinely-fixable
# bloat re-surfaces rather than being silenced forever. Material = >10% past the
# acknowledged byte count (matches the run's agreed growth threshold). Below that,
# the acknowledgement holds and the badge stays neutral/informational.
_OVERSIZE_ACK_GROWTH = 0.10

# Durable-vs-ephemeral classifier (spec item 4): verification-ceremony phrase
# families observed live in the qa context — point-in-time proof with no future
# value. Case-insensitive, anchored at the start of an entry where the family is
# a recurring prefix. Pure heuristic, no LLM.
_EPHEMERAL_RES = [
    re.compile(r"skeptic\s+sabotage\s+that\s+paid\s+off", re.IGNORECASE),
    re.compile(r"live\s+in-?process\s+proof", re.IGNORECASE),
    re.compile(r"unverified-by-design:\s*browser\s+pixel\s+render", re.IGNORECASE),
    re.compile(r"scope\s+clean:\s*head\s+unchanged", re.IGNORECASE),
    re.compile(r"re-?ran\s+the\s+full\s+uat\b.*\bsuite\s+\d+\s+green", re.IGNORECASE),
]


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
    """Group entries that are genuinely redundant or explicitly superseding (pure-Python).

    Pure heuristic, no LLM, deterministic, and deliberately CONSERVATIVE — Merge
    must only ever see entries it can safely fuse into ONE non-colliding version.
    A file of distinct-but-related decisions about one subsystem (each merely
    co-mentioning a shared identifier) must yield ZERO clusters; a true duplicate
    pair or an explicit correction pair yields exactly one tight (≈2-entry)
    cluster. So:

      1. Document-frequency filter: tokens appearing in more than
         `_DF_FRACTION_CAP` of entries are "ubiquitous" (test/etag/context on a
         real file) and dropped before linking. What's left is each entry's
         discriminative vocabulary `disc[i]`.
      2. HIGH OVERLAP RATIO links two entries: with `lo = min(|disc[i]|,|disc[j]|)`
         guarded at `>= _OVERLAP_MIN_DENOM`, they link when
         `|disc[i] & disc[j]| / lo >= _OVERLAP_RATIO_MIN` — i.e. a large fraction
         of the smaller entry's discriminative vocabulary is shared. Co-mentioning
         a single identifier (the old mega-cluster shortcut) no longer links
         anything.
      3. EXPLICIT DATED SUPERSEDE links two entries: an entry carrying a
         `_SUPERSEDE_RE` marker AND a referenced `YYYY-MM-DD` (not its own date)
         that equals ANOTHER entry's date, with >=1 shared discriminative token,
         links specifically to that entry and labels the cluster "superseded".
      4. Cluster-size cap (`_MAX_CLUSTER_SIZE`) stays a backstop only — with the
         tight rule clusters are naturally ~2 entries.

    Returns clusters: [{ "reason", "entryIndices": [...] }], reason
    "superseded" or "redundant", smallest-index first. Singletons are skipped.

    `dismissed_keys`: content-hash keys of clusters the user chose "Keep all" on
    — those are filtered out so they don't re-flag every reload.
    """
    n = len(entries)
    if n < 2:
        return []
    toks = [_tokens(e["text"]) for e in entries]

    # Document frequency per token, then the ubiquitous set to ignore.
    df: dict[str, int] = {}
    for ts in toks:
        for t in ts:
            df[t] = df.get(t, 0) + 1
    df_cap = max(2, int(n * _DF_FRACTION_CAP))
    ubiquitous = {t for t, c in df.items() if c > df_cap}
    disc = [ts - ubiquitous for ts in toks]  # discriminative tokens per entry

    # Union-find over entries that are strongly related.
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

    # (a) High overlap ratio of discriminative vocabulary → genuine redundancy.
    for i in range(n):
        for j in range(i + 1, n):
            lo = min(len(disc[i]), len(disc[j]))
            if lo < _OVERLAP_MIN_DENOM:
                continue
            shared = len(disc[i] & disc[j])
            if shared / lo >= _OVERLAP_RATIO_MIN:
                union(i, j)

    # (b) Explicit dated supersede: a marker-bearing entry that names another
    # entry's date (and shares a discriminative token with it) links to exactly
    # that entry and taints the resulting cluster "superseded".
    by_date: dict[str, list[int]] = {}
    for idx, e in enumerate(entries):
        if e["date"]:
            by_date.setdefault(e["date"], []).append(idx)
    marker_linked: set[int] = set()
    for i, e in enumerate(entries):
        if not _SUPERSEDE_RE.search(e["text"]):
            continue
        for ref in set(_DATE_REF_RE.findall(e["text"])):
            if ref == e["date"]:
                continue  # the entry's own date is not a supersede reference
            for j in by_date.get(ref, ()):
                if j == i:
                    continue
                if disc[i] & disc[j]:  # require at least some topic overlap
                    union(i, j)
                    marker_linked.add(i)
                    marker_linked.add(j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list[dict] = []
    for members in groups.values():
        if len(members) < 2 or len(members) > _MAX_CLUSTER_SIZE:
            continue
        members.sort()
        # Skip clusters the user has explicitly dismissed ("Keep all"). The key
        # is content-based so it survives entry reordering/index shifts.
        if dismissed_keys is not None and _cluster_key([entries[i]["text"] for i in members]) in dismissed_keys:
            continue
        reason = "superseded" if any(i in marker_linked for i in members) else "redundant"
        clusters.append({"reason": reason, "entryIndices": members})
    clusters.sort(key=lambda c: c["entryIndices"][0])
    return clusters


def classify_ephemeral(entries: list[dict]) -> list[bool]:
    """Tag each entry as likely-ephemeral (verification-ceremony, no future value).

    Pure heuristic, no LLM — matches the phrase families observed live in the qa
    context (skeptic-sabotage / live-proof / unverified-by-design / scope-clean /
    re-ran-UAT). Returns a per-entry boolean list parallel to `entries`. Nothing
    is deleted here; the UI groups these for a human-gated bulk sweep.
    """
    return [
        any(rx.search(e["text"]) for rx in _EPHEMERAL_RES)
        for e in entries
    ]


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


def _entry_hash(text: str) -> str:
    """Content hash of one entry's text, normalized the same way as _cluster_key
    (strip + lowercase + collapse whitespace) so trivial diffs don't churn it."""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _read_review_marker(project_dir: Path, name: str) -> int:
    """Return the entry count recorded at last review (0 if never reviewed).

    Backward-compatible reader for the legacy callers that only need the count.
    The richer set-of-hashes form (see `_read_reviewed_hashes`) keeps the same
    first line (`count:digest`) so this stays correct against new markers too.
    """
    try:
        raw = _review_marker_path(project_dir, name).read_text(encoding="utf-8").strip()
        return int(raw.split("\n", 1)[0].split(":", 1)[0])
    except (OSError, ValueError):
        return 0


def _read_reviewed_hashes(project_dir: Path, name: str) -> set[str] | None:
    """Return the set of reviewed entry-content hashes, or None for a legacy
    (count-only) marker so callers can fall back to count-based detection."""
    try:
        raw = _review_marker_path(project_dir, name).read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return None  # legacy "count:digest" only → no content hashes recorded
    return set(lines[1:])


def _new_entry_indices(project_dir: Path, name: str, entries: list[dict]) -> list[int]:
    """Indices of entries whose content was NOT present at last review.

    Content-based, so an entry inserted mid-file (not appended at the tail) is
    still counted as new. Falls back to count-based ("tail is new") when only a
    legacy count marker is stored. Never-reviewed → every entry is new.
    """
    hashes = _read_reviewed_hashes(project_dir, name)
    if hashes is None:
        reviewed = _read_review_marker(project_dir, name)
        return [e["index"] for e in entries if e["index"] >= reviewed]
    return [e["index"] for e in entries if _entry_hash(e["text"]) not in hashes]


# ── Context-summary parse memo (E1) ──────────────────────────────────────────
#
# `context_summary` is called per-agent from BOTH the /agents listing and the
# projects listing, so a multi-tab burst re-runs the O(N) `parse_context_entries`
# (and feeds the O(N^2) `detect_conflicts`) over the SAME unchanged .md repeatedly.
# This is a pure read-through memo of ONLY the .md-CONTENT-DERIVED expensive input:
# the parsed entries, keyed by (project_dir, name, etag) where etag is the .md's
# st_mtime_ns (filestore.etag_for). On an etag hit we skip parse_context_entries.
#
# CACHE BOUNDARY (load-bearing — see ADR D-032 / the D-031 traps): we cache ONLY
# the parsed entries. The sidecar-driven signals — newEntryCount (.reviewed marker),
# the dismissed set (.dismissed), and oversizeActionable/Acknowledged (.oversize) —
# change OUT OF BAND (a user acks/dismisses/reviews WITHOUT touching the .md), so
# they are RECOMPUTED FRESH ON EVERY CALL. In particular detect_conflicts is
# RE-RUN against the freshly-read dismissed set every call (the conflict COUNT is
# never cached: a cluster dismiss lowers the true count without moving the .md
# mtime, so keying the count on the .md etag alone would serve a stale count).
_PARSE_CACHE: dict[tuple, list[dict]] = {}


def context_summary(project_dir: Path, name: str) -> dict:
    """{entryCount, newEntryCount, conflictClusterCount, oversized,
    oversizeActionable, oversizeAcknowledged, contextBytes, lineCount} for a
    role's context. Never raises.

    `oversized` (design §4.4): true when the file exceeds ~6KB OR ~400 lines,
    even when conflictClusterCount == 0 — a size-ceiling nudge to reconcile.
    `oversizeActionable` / `oversizeAcknowledged` distinguish a genuinely-fixable
    oversize (warn) from an honestly-large, fully-reconciled one (informational).
    """
    try:
        context_path = _context_path(project_dir, name)
        content, _ = filestore.read_text(context_path)
        # Memo ONLY the .md-content-derived parse, keyed by the .md's mtime_ns
        # etag. On an etag hit reuse the parsed entries (skip the walk); on a miss
        # (or absent etag) parse and store. The sidecar signals below ALWAYS
        # recompute fresh so an out-of-band ack/dismiss/review is never stale.
        etag = filestore.etag_for(context_path)
        cache_key = (str(project_dir), name, etag)
        if etag is not None and cache_key in _PARSE_CACHE:
            entries = _PARSE_CACHE[cache_key]
        else:
            entries = parse_context_entries(content)
            if etag is not None:
                _PARSE_CACHE[cache_key] = entries
        new_count = len(_new_entry_indices(project_dir, name, entries))
        dismissed = _read_dismissed_keys(project_dir, name)
        context_bytes = len(content.encode("utf-8"))
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        oversized = context_bytes > _OVERSIZE_BYTES or line_count > _OVERSIZE_LINES
        conflict_count = len(detect_conflicts(entries, dismissed))
        has_ephemeral = any(classify_ephemeral(entries))
        actionable, acknowledged = _oversize_signals(
            project_dir, name,
            context_bytes=context_bytes, oversized=oversized,
            conflict_count=conflict_count, has_ephemeral=has_ephemeral,
        )
        return {
            "entryCount": len(entries),
            "newEntryCount": new_count,
            "conflictClusterCount": conflict_count,
            "oversized": oversized,
            "oversizeActionable": actionable,
            "oversizeAcknowledged": acknowledged,
            "contextBytes": context_bytes,
            "lineCount": line_count,
        }
    except Exception:
        return {
            "entryCount": 0, "newEntryCount": 0, "conflictClusterCount": 0,
            "oversized": False, "oversizeActionable": False, "oversizeAcknowledged": False,
            "contextBytes": 0, "lineCount": 0,
        }


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

    return web.json_response(await _list_agents_async(project_dir))


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
    # Surface the oversize signals so the detail-header badge can render the same
    # honest warn-vs-informational state as the list row. Project agents compute
    # against THIS project's context; global agents carry no per-project context.
    if scope == "project":
        summary = context_summary(project_dir, name)
    else:
        summary = {"oversized": False, "oversizeActionable": False, "oversizeAcknowledged": False}
    return web.json_response({
        "scope": scope,
        "name": meta.get("name", name),
        "description": meta.get("description", ""),
        "model": meta.get("model", ""),
        "tools": _tools_list(meta),
        "content": content,
        "etag": etag,
        "oversized": summary.get("oversized", False),
        "oversizeActionable": summary.get("oversizeActionable", False),
        "oversizeAcknowledged": summary.get("oversizeAcknowledged", False),
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
    new_indices = _new_entry_indices(project_dir, name, entries)
    context_bytes = len(content.encode("utf-8"))
    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    oversized = context_bytes > _OVERSIZE_BYTES or line_count > _OVERSIZE_LINES
    conflict_count = len(detect_conflicts(entries, _read_dismissed_keys(project_dir, name)))
    has_ephemeral = any(classify_ephemeral(entries))
    actionable, acknowledged = _oversize_signals(
        project_dir, name,
        context_bytes=context_bytes, oversized=oversized,
        conflict_count=conflict_count, has_ephemeral=has_ephemeral,
    )
    return web.json_response({
        "exists": path.is_file(),
        "content": content,
        "etag": etag,
        "entries": entries,
        "lastReviewedCount": reviewed,
        # Kept for backward compat; newEntryIndices is the content-based signal.
        "newEntryCount": len(new_indices),
        "newEntryIndices": new_indices,
        "contextBytes": context_bytes,
        "lineCount": line_count,
        "oversized": oversized,
        "oversizeActionable": actionable,
        "oversizeAcknowledged": acknowledged,
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


async def get_context_ephemeral(request: web.Request) -> web.Response:
    """Return every entry tagged durable-vs-ephemeral for the sweep review mode.

    { entries: [{index, date, text, ephemeral}], ephemeralIndices: [...], etag }.
    Classification only — nothing is removed. The UI groups the ephemeral ones
    so the human can bulk-drop them via reconcile action "sweep".
    """
    project_dir, name = _resolve_context(request)
    content, etag = filestore.read_text(_context_path(project_dir, name))
    entries = parse_context_entries(content)
    flags = classify_ephemeral(entries)
    out = [
        {"index": e["index"], "date": e["date"], "text": e["text"], "ephemeral": flags[i]}
        for i, e in enumerate(entries)
    ]
    return web.json_response({
        "entries": out,
        "ephemeralIndices": [e["index"] for e, f in zip(entries, flags) if f],
        "etag": etag,
    })


def _render_entries(entries: list[dict]) -> str:
    """Re-serialize entries back to append-only bullet lines (preserving raw)."""
    return "\n".join(e["raw"] for e in entries)


def _entry_prefix(entry: dict) -> str:
    """The bullet prefix preserving the entry's date: '- {date}: ' or '- '."""
    return f"- {entry['date']}: " if entry["date"] else "- "


async def reconcile_context(request: web.Request) -> web.Response:
    """Apply a reconciliation. The ONLY endpoint that removes/rewrites entries.

    Body: { action: "keep"|"merge"|"compact"|"dismiss"|"sweep", entryIndices: [...],
            mergedText?, compactText?, keepIndex?, etag }
      - keep:    keep ONE survivor, drop the rest. Survivor selection:
                 explicit `keepIndex` if given, else superseded-cluster → the
                 NEWEST (max index, the correction), plain redundancy → oldest
                 (min index). Never silently drops the correcting entry.
      - merge:   replace the cluster with one new entry (mergedText) at the
                 position of the first index.
      - compact: replace ONE over-long entry's text in place with compactText,
                 preserving its date prefix. Needs exactly one index.
      - dismiss: remove nothing; remember the cluster so it stops flagging.
      - sweep:   bulk-drop the provided entryIndices in one write (the ephemeral
                 sweep — the human supplies the indices; nothing else is touched).

    Etag-guarded (409 on concurrent external edit, mirroring put_settings),
    preserving the file's non-entry lines (the `# Title` heading etc.). The file
    is re-read here at write time and the etag is the safety net: entries that
    appeared after the panel loaded are never dropped.
    """
    project_dir, name = _resolve_context(request)
    body = await read_json_body(request)
    action = body.get("action")
    if action not in ("keep", "merge", "compact", "dismiss", "sweep"):
        raise web.HTTPBadRequest(reason="action must be keep|merge|compact|dismiss|sweep")
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
    # Snapshot the pre-action entries (by content) so we can carry forward exactly
    # which entries were already reviewed — captured BEFORE any in-place rewrite of
    # entries[idx]["raw"] mutates the merge/compact targets.
    pre_entries = [dict(e) for e in entries]
    prev_reviewed = _previously_reviewed_hashes(project_dir, name, pre_entries)
    # Heading / preamble = lines before the first entry's first raw line. Capture
    # it now, before any in-place rewrite mutates entries[0]["raw"].
    first_entry_line = lines.index(entries[0]["raw"].split("\n")[0]) if entries else len(lines)

    if action == "dismiss":
        # "Keep all": remove nothing, but remember this cluster (by content hash)
        # so detection won't re-flag it on the next load.
        key = _cluster_key([e["text"] for e in entries if e["index"] in set(indices)])
        _add_dismissed_key(project_dir, name, key)
        return web.json_response({"ok": True, "action": "dismiss", "removed": 0})

    drop: set[int] = set(indices)
    # Indices this action actually produced/selected as part of the cluster/sweep
    # the user reviewed — these become reviewed; entries NOT in this set keep their
    # "new" flag (only the entries the human touched are cleared from the badge).
    touched: set[int] = set(indices)
    if action == "keep":
        survivor = _keep_survivor(body, indices, entries)
        drop.discard(survivor)
    elif action == "merge":
        merged_text = (body.get("mergedText") or "").strip()
        if not merged_text:
            raise web.HTTPBadRequest(reason="mergedText required for merge")
        # Rewrite the first entry's line to the merged text (keep its date if any),
        # then drop the others.
        keep_first = min(indices)
        e0 = next(e for e in entries if e["index"] == keep_first)
        entries[keep_first]["raw"] = _entry_prefix(e0) + merged_text
        drop.discard(keep_first)
    elif action == "compact":
        if len(indices) != 1:
            raise web.HTTPBadRequest(reason="compact requires exactly one entryIndex")
        compact_text = (body.get("compactText") or body.get("mergedText") or "").strip()
        if not compact_text:
            raise web.HTTPBadRequest(reason="compactText required for compact")
        idx = indices[0]
        e0 = next(e for e in entries if e["index"] == idx)
        entries[idx]["raw"] = _entry_prefix(e0) + compact_text
        drop.discard(idx)  # compact rewrites in place; drops nothing
    # action == "sweep": drop is exactly the supplied indices (bulk delete).

    # Rebuild the file: keep every non-entry line in place; for entry lines, emit
    # the (possibly-rewritten) entry unless it's being dropped.
    kept_entries = [e for e in entries if e["index"] not in drop]
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

    # PARTIAL review advance: carry forward the previously-reviewed entries that
    # still exist, plus the entries THIS reconcile produced/touched (the survivor,
    # the merged/compacted entry, the kept members of a sweep selection). Entries
    # the user never reviewed and never touched here keep their "new" flag, so
    # reconciling one cluster can't silently clear unrelated entries' badge.
    # `kept_entries` carries the ORIGINAL index plus the (possibly-rewritten) raw,
    # so re-parse each kept entry's final raw to hash its CURRENT text — merge and
    # compact rewrote `raw` but not the stale `text` field.
    kept_hashes: set[str] = set()
    touched_hashes: set[str] = set()
    for e in kept_entries:
        parsed = parse_context_entries(e["raw"])
        h = _entry_hash(parsed[0]["text"]) if parsed else _entry_hash(e["text"])
        kept_hashes.add(h)
        if e["index"] in touched:
            touched_hashes.add(h)
    reviewed_hashes = (prev_reviewed & kept_hashes) | touched_hashes
    _write_review_marker_hashes(project_dir, name, new_content, reviewed_hashes)
    return web.json_response({"ok": True, "action": action, "removed": len(drop), "etag": new_etag})


def _keep_survivor(body: dict, indices: list[int], entries: list[dict]) -> int:
    """Pick which entry survives a 'keep'.

    Explicit `keepIndex` from the UI wins (the user picked the survivor). Else
    fall back on cluster reason: a superseded cluster keeps the NEWEST entry (the
    correction = max index); plain redundancy keeps the oldest (min index). The
    reason is re-detected from the current entries so we never depend on a stale
    client-side label.
    """
    keep_index = body.get("keepIndex")
    if isinstance(keep_index, int) and keep_index in indices:
        return keep_index
    reason = body.get("reason")
    if reason not in ("superseded", "redundant"):
        reason = "redundant"
        idxset = set(indices)
        for c in detect_conflicts(entries):
            if set(c["entryIndices"]) == idxset:
                reason = c["reason"]
                break
    return max(indices) if reason == "superseded" else min(indices)


def _write_review_marker(project_dir: Path, name: str, count: int, content: str) -> None:
    """Persist the last-reviewed marker in the richer content-hash format.

    Line 1 stays `count:digest` for backward compatibility (legacy readers).
    Subsequent lines are the per-entry content hashes of every reviewed entry,
    enabling content-based new-detection regardless of insertion position.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    entries = parse_context_entries(content)
    lines = [f"{count}:{digest}"] + [_entry_hash(e["text"]) for e in entries]
    filestore.write_text(_review_marker_path(project_dir, name), "\n".join(lines) + "\n")


def _write_review_marker_hashes(project_dir: Path, name: str, content: str, reviewed_hashes: set[str]) -> None:
    """Persist a review marker from an EXPLICIT set of reviewed entry hashes.

    Used by reconcile to advance reviewed-ness ONLY for the entries the action
    actually touched/produced — entries that were never reviewed and were not part
    of the action keep their "new" flag (they get no hash here). Line 1 keeps the
    legacy `count:digest` shape (count = number of reviewed hashes that still exist
    in the file) so old count-only readers don't choke; the subsequent lines are
    the reviewed content hashes.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    sorted_hashes = sorted(reviewed_hashes)
    lines = [f"{len(sorted_hashes)}:{digest}"] + sorted_hashes
    filestore.write_text(_review_marker_path(project_dir, name), "\n".join(lines) + "\n")


def _previously_reviewed_hashes(project_dir: Path, name: str, pre_entries: list[dict]) -> set[str]:
    """The set of entry-content hashes that were reviewed BEFORE the current action.

    Reads the rich content-hash marker directly when present. Falls back
    conservatively for a legacy count-only marker by seeding from the first
    `reviewed_count` entries (by content) of the pre-action file — so unrelated
    untouched tail entries that the user never saw are NOT treated as reviewed.
    """
    hashes = _read_reviewed_hashes(project_dir, name)
    if hashes is not None:
        return hashes
    reviewed_count = _read_review_marker(project_dir, name)
    return {_entry_hash(e["text"]) for e in pre_entries[:reviewed_count]}


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


def _oversize_ack_path(project_dir: Path, name: str) -> Path:
    """Sidecar recording that the user acknowledged this file is honestly large.

    Stores the acknowledged byte size, under .claude/agent-context/.reviewed/
    (gitignored), one per agent. Absence means "not acknowledged".
    """
    return project_dir / ".claude" / "agent-context" / ".reviewed" / f"{name}.oversize"


def _read_oversize_ack(project_dir: Path, name: str) -> int | None:
    """The acknowledged byte size, or None if not acknowledged / unreadable.

    Read defensively (missing or corrupt sidecar => None => not acknowledged) so a
    bad file degrades to "still flagged" rather than 500ing the listing.
    """
    try:
        raw = _oversize_ack_path(project_dir, name).read_text(encoding="utf-8").strip()
        return int(raw.split("\n", 1)[0])
    except (OSError, ValueError):
        return None


def _oversize_signals(
    project_dir: str | Path,
    name: str,
    *,
    context_bytes: int,
    oversized: bool,
    conflict_count: int,
    has_ephemeral: bool,
) -> tuple[bool, bool]:
    """Compute (oversizeActionable, oversizeAcknowledged) for a context file.

    - oversizeActionable: oversized AND there is real reduction work left —
      at least one conflict cluster to reconcile OR at least one ephemeral entry
      to sweep. (A file that is honestly large with nothing left to reconcile is
      NOT actionable — the signal downgrades to informational.)
    - oversizeAcknowledged: a sidecar is present, the file has not grown materially
      past the acknowledged byte count, AND there is no actionable work left. A
      STALE acknowledgement (file grew >10% OR new actionable work appeared) is
      treated as NOT acknowledged so the signal re-surfaces.

    Never raises — a corrupt sidecar read degrades to "not acknowledged".
    """
    actionable = bool(oversized) and (conflict_count > 0 or has_ephemeral)
    project_dir = Path(project_dir)
    acked_bytes = _read_oversize_ack(project_dir, name)
    if acked_bytes is None or actionable:
        return actionable, False
    grew_materially = context_bytes > acked_bytes * (1 + _OVERSIZE_ACK_GROWTH)
    return actionable, not grew_materially


async def mark_context_reviewed(request: web.Request) -> web.Response:
    """Advance the last-reviewed marker to the current entry count.

    Clears the "N new entries" signal without removing anything.

    Etag-guarded (design §6a): the agent auto-appends to these context files at
    task end, so if a brand-new entry arrived between the panel load and this
    click, marking the WHOLE current file reviewed would silently bury that
    never-seen entry. When the client forwards its `etag`, we compare it to the
    file's CURRENT etag and, on a mismatch, refuse with the SAME 409 shape
    reconcile uses ({error, message, current, etag}) and write NO marker — the
    panel reloads from `current`/`etag` to reveal the arrival and the user must
    click again (a real re-review, NOT a blind retry). For back-compat, a client
    that sends no etag keeps the one-click behavior so the unchanged-file happy
    path is unbroken.
    """
    project_dir, name = _resolve_context(request)
    # Tolerate a body-less POST (legacy one-click clients sent no body): only a
    # malformed NON-empty body is a 400; an empty body means "no etag forwarded".
    if request.can_read_body:
        body = await read_json_body(request)
    else:
        body = {}
    expected_etag = body.get("etag")
    context_path = _context_path(project_dir, name)
    content, current_etag = filestore.read_text(context_path)
    # The marker is a sidecar (write_text's etag guard would check the marker file,
    # not the context file), so compare the CONTEXT file's etag explicitly here —
    # mirroring the ConflictError → 409 mapping reconcile uses.
    if expected_etag is not None and current_etag is not None and current_etag != expected_etag:
        return web.json_response(
            {
                "error": "conflict",
                "message": (
                    "The context file changed since you opened this panel; "
                    "review the new entries, then mark reviewed again."
                ),
                "current": content,
                "etag": current_etag,
            },
            status=409,
        )
    entries = parse_context_entries(content)
    _write_review_marker(project_dir, name, len(entries), content)
    return web.json_response({"ok": True, "reviewedCount": len(entries), "etag": current_etag})


async def acknowledge_oversize(request: web.Request) -> web.Response:
    """Acknowledge that an oversized context file is honestly large (informational).

    The honesty gate (the user must never dodge real cleanup): the ack is ACCEPTED
    ONLY when the file is currently oversized AND there is NOTHING left to reduce —
    zero conflict clusters to reconcile AND zero ephemeral entries to sweep. If
    real reduction work remains, refuse with a 409 + message so the user does the
    cleanup first. On accept, persist the current byte size in the sidecar so a
    later material growth (>10%) or newly-detected actionable work re-surfaces the
    warning (no permanent blindfold).
    """
    project_dir, name = _resolve_context(request)
    content, _ = filestore.read_text(_context_path(project_dir, name))
    entries = parse_context_entries(content)
    context_bytes = len(content.encode("utf-8"))
    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    oversized = context_bytes > _OVERSIZE_BYTES or line_count > _OVERSIZE_LINES
    if not oversized:
        raise web.HTTPBadRequest(reason="context file is not oversized; nothing to acknowledge")
    conflict_count = len(detect_conflicts(entries, _read_dismissed_keys(project_dir, name)))
    has_ephemeral = any(classify_ephemeral(entries))
    if conflict_count > 0 or has_ephemeral:
        return web.json_response(
            {
                "error": "actionable",
                "message": (
                    "This file still has reconcilable conflicts or sweepable "
                    "ephemeral entries — reconcile/sweep those first; "
                    "they would actually reduce the file."
                ),
                "conflictClusterCount": conflict_count,
                "hasEphemeral": has_ephemeral,
            },
            status=409,
        )
    filestore.write_text(_oversize_ack_path(project_dir, name), f"{context_bytes}\n")
    return web.json_response({"ok": True, "acknowledgedBytes": context_bytes})


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
