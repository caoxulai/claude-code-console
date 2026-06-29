"""Assistant -> Email: draft-queue + style memory + MCP delegation seam.

ARCHITECTURE: mirrors the Slack module (server/routes/slack.py) exactly.
A JSON sidecar (email_threads.json) stores the queue; a _run_email_agent
delegation seam handles agent operations; a _EMAIL_READ_ONLY_TOOLS frozenset
enforces the send-safety boundary.

The email system NEVER sends email directly. The approve path saves a draft
via the delegation seam (action 'save_draft') -- it does NOT call 'reply' or
'send'. The user sends from their own email client.

DETERMINISTIC SCAN: the _scan_worker drives the email MCP server DIRECTLY
via the `mcp` Python SDK (call_read_tool -> stdio_client -> ClientSession).
The send-safety boundary is _EMAIL_READ_ONLY_TOOLS: call_read_tool REFUSES
by name any tool NOT in that frozenset BEFORE the SDK touches the server.

WORKERS:
  _scan_worker   — periodic inbox fetch + skeleton creation (300s interval)
  _classify_worker — batches needs-classify items through MCP-FREE classify (6s)
  _draft_worker  — drafts needs-draft items via MCP-FREE draft action (7s)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import glob
import html
import json
import logging
import os
import re
import secrets
import shutil
import signal
import tempfile
import time
from pathlib import Path

from aiohttp import web

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from server import filestore
from server.config import cfg
from server.routes import read_json_body
from server.routes.workers import register_worker, mark_worker_run
from server.session_manager import is_auth_error

logger = logging.getLogger(__name__)


# ── Path resolution ─────────────────────────────────────────────────────────

def _resolve_email_path() -> Path:
    """Resolve the email_threads.json sidecar.

    Precedence: cfg.email_path (sourced from CLAUDE_WEB_EMAIL_PATH env or
    data_dir default in server/config.py).
    """
    return cfg.email_path


EMAIL_PATH = _resolve_email_path()


def _style_path() -> Path:
    """Human-readable learned-style store, sibling to the sidecar."""
    return EMAIL_PATH.parent / "email_style.md"


def _email_topics_dir() -> Path:
    """Per-contact topic-memory dir, sibling to the sidecar.

    The KEY divergence from Slack (which keys topics by raw display name): email
    contacts are addresses, so we slugify them into one file per contact
    (``email_topics/<slug>.md``). The slug derivation (_slugify_contact) is shared
    by read/write/append/prompt so all paths resolve the SAME file.
    """
    return EMAIL_PATH.parent / "email_topics"


# ── Constants ───────────────────────────────────────────────────────────────

# emailBody cap: spec mandates capping at 32k chars when storing items.
_EMAIL_BODY_CAP = 32768

# A GFM table separator row: '| --- | :--: | ... |' — every cell between the outer
# pipes is a dash delimiter (optionally with leading/trailing alignment colons).
_GFM_SEP_ROW = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")


def _safe_body_truncate(text: str, cap: int) -> str:
    """Cap ``text`` at ``cap`` chars WITHOUT severing a GFM table mid-row.

    ``text`` is the OUTPUT of ``_html_to_text`` (full GFM, tables already
    converted), so a raw ``text[:cap]`` slice can cut a table data row in half ->
    remark-gfm then rejects the whole table and renders it as literal pipe text.

    The truncation is no-word-loss beyond the intentional cap: it only trims the
    trailing PARTIAL line the cap cut through (back to the last complete-line
    boundary), and — if that leaves a GFM table header+separator with NO data rows
    (a ghost table) — drops the orphaned header+separator too. Everything BEFORE
    the severed row is byte-identical; nothing is reordered or rewritten.
    """
    if text is None:
        return text
    if len(text) <= cap:
        return text

    sliced = text[:cap]
    # The slice ended mid-line UNLESS the very next char is a newline (i.e. the cut
    # landed exactly at the end of a complete line). When it cut mid-line, drop the
    # trailing partial line back to the last complete-line boundary.
    if text[cap:cap + 1] != "\n":
        nl = sliced.rfind("\n")
        sliced = sliced[: nl + 1] if nl != -1 else ""

    # Drop a now-orphaned table header+separator: if the retained tail is a GFM
    # separator row whose data rows were all trimmed away, removing only the
    # separator would still leave a 1-row "ghost" header. Drop BOTH so no empty
    # table renders. (The header is the non-blank line immediately above it.)
    lines = sliced.split("\n")
    # Trailing empty strings come from the final newline / blank-line table
    # terminator; ignore them when locating the last content line.
    last = len(lines) - 1
    while last >= 0 and lines[last].strip() == "":
        last -= 1
    if last >= 1 and _GFM_SEP_ROW.match(lines[last].strip()):
        # lines[last] is an orphan separator (no data row beneath survived) and
        # lines[last-1] is its header — drop both, keep everything before.
        head = lines[: last - 1]
        # Preserve a single trailing newline if the kept content had one.
        sliced = "\n".join(head)
        if head and sliced:
            sliced += "\n"
    return sliced

_SNIPPET_CAP = 2000
_DRAFT_CAP = 8000

# threadHistory caps (spec 1a): a multi-turn conversation is stored on the item
# as [{sender, timestamp, body}] turns so the draft worker needs no further MCP
# call. Each turn body is capped and the turn count is bounded so a long thread
# can't bloat the sidecar.
_THREAD_TURN_BODY_CAP = 4096
_THREAD_HISTORY_MAX_TURNS = 20

# senderList cap: the From column shows the latest sender + a "+N" participant
# count derived from this list, so a runaway thread can't bloat the item.
_SENDER_LIST_MAX = 12

# Lines that may carry credentials -- never persisted or surfaced.
_SECRET_MARKERS = ("~/.midway", ".midway", "cookie", "mwinit", "aws_secret", "authorization:")

_VALID_STATUS = {
    "needs-classify", "needs-draft", "needs-review", "edited", "approved",
    "dismissed",
}

_VALID_CLASSIFICATIONS = {"needs-reply", "fyi", "actionable"}

# Classification routing. needs-reply / actionable flow to needs-draft so the
# draft worker auto-drafts them. FYI does NOT auto-draft (D-056 #2): it lands in
# the review queue with NO draft and the user generates one ON DEMAND (the
# Generate-reply button -> regenerate_item). needs-review is non-draft (not picked
# up by _needs_draft / _draft_pending) AND non-terminal (still counted by
# _count_actionable, so an undrafted FYI stays an active to-do, D-035 lockstep).
_CLASSIFY_NEXT_STATUS = {
    "needs-reply": "needs-draft",
    "actionable": "needs-draft",
    "fyi": "needs-review",
}

# Cap on the ACTIVE TO-DO LIST (the actionable queue), NOT the per-fetch read
# window. This is a REPLENISH-ON-CLEAR cap: each scan admits at most
# (CAP - current_actionable) new items, so as the user clears items (approve/
# dismiss/delete drops them out of _count_actionable) the next scan tops the list
# back up from the remaining unread inbox, oldest-first, until the inbox is drained.
# The actionable set is defined by _count_actionable / _TERMINAL_STATUSES (the SAME
# predicate as the "N to review" count, D-035) — never a second inline definition.
EMAIL_ACTIVE_TODO_CAP = 100

# Inbox SUBFOLDERS to triage in addition to the Inbox ROOT (D-058). These are
# matched by DISPLAY NAME at scan time and resolved to their CURRENT folder id via
# email_list_folders -- NEVER hardcode AAMk ids (they change if a folder is
# recreated/renamed). Read through the aws-outlook-mcp (OWA/EWS) backend via the
# existing call_read_tool routing, which is OFF the GRASP 500/day quota. A
# management UI/API for this list is OUT of scope -- edit it here. See D-058.
EMAIL_SCAN_SUBFOLDERS = (
    "1 managers", "1 GSD", "1 CRIS OOR", "1 my directs", "1 me in TO",
    "1 me only", "1 me in CC", "1 leads", "1 DG", "1 AMP 2.0", "0 to do",
    "1 MX", "2 glenn dev", "2 black falcon",
)

# Worker tuning constants (mirroring Slack module):
EMAIL_SCAN_INTERVAL_S = 300        # 5 minutes between scans
EMAIL_CLASSIFY_POLL_INTERVAL_S = 6  # classify worker poll interval
EMAIL_CLASSIFY_BATCH = 10          # max items per classify LLM call
EMAIL_DRAFT_POLL_INTERVAL_S = 7    # draft worker poll interval
EMAIL_DRAFT_CONCURRENCY = 3        # parallel draft subprocess cap

# Idle teardown of the persistent Graph (manager-outlook-mcp) session. The bundle's
# internal TokenRefreshRunner fires a background re-auth while the session is held
# open. On the now-default OWA backend that's a cheap refresh-token call against
# Microsoft (no shared quota), so this is defense-in-depth rather than the primary
# guard -- but on the legacy GRASP backend it was a FULL SAML re-auth every ~10 min
# that drained GRASP's shared API-Gateway quota. Either way we do NOT hold the
# session alive while the Email tab is idle: if no Graph call has run for this long
# (or the queue is paused), the reaper tears it down so the refresh timer dies with
# the subprocess. Kept UNDER one ~10-min refresh cycle. See
# docs/email-owa-backend-runbook.md.
_GRAPH_IDLE_TEARDOWN_S = 180

# When a Graph read 429s / hits a GRASP quota_exceeded, back the scan worker off
# for this long before retrying so it can't keep hammering the exhausted quota.
_GRAPH_THROTTLE_WINDOW_S = 15 * 60  # 15 minutes

# Subprocess timeout budgets:
_AGENT_TIMEOUT_S = 120   # classify/draft/regenerate
_SAVE_DRAFT_TIMEOUT_S = 60  # save_draft (simpler, shorter)

# StreamReader limit for the subprocess stdout (large result lines):
_STREAM_LIMIT_BYTES = 8 * 1024 * 1024

# Mute TTL: 30 days (matching Slack)
_MUTE_TTL_S = 30 * 24 * 3600

# MCP config keys we copy from an existing server entry (scrubbed):
_MCP_ENTRY_KEYS = ("command", "args", "env", "type", "url", "headers")

# Fallback email MCP launch spec when no entry is found in claude configs:
_EMAIL_MCP_FALLBACK = {
    "command": "aim",
    "args": ["mcp", "start-server", "aws-outlook-mcp"],
    "env": {},
}

# Fallback manager-outlook-mcp (Microsoft Graph) launch spec. The wrapper at
# ~/.aim/mcp-servers/manager-outlook-mcp runs `aim mcp start-server
# manager-outlook-mcp`; prefer the bare command (it's on PATH after `aim mcp
# install`), else fall back to the explicit aim invocation. The auth backend is
# forced to OWA in _graph_mcp_params (see runbook), not here.
_GRAPH_MCP_FALLBACK = {
    "command": "manager-outlook-mcp",
    "args": [],
    "env": {},
}

# Node-24 resolution: manager-outlook-mcp HARD-ABORTS on Node < 24 (logs
# "Node version 22 detected, but version 24 or higher is required" and the MCP
# handshake dies with "Connection closed"). The aws-outlook-mcp launch runs fine
# on the default Node 22, so we PREPEND a Node-24 bin dir onto PATH for the GRAPH
# subprocess ONLY — never the global toolchain. The bin dir is discovered by
# globbing the mise install dir; an env override (CLAUDE_WEB_NODE24_BIN) wins.
_NODE24_ENV_OVERRIDE = "CLAUDE_WEB_NODE24_BIN"
_NODE24_GLOB = str(Path.home() / ".local" / "share" / "mise" / "installs" / "node" / "24*" / "bin")


# ── Send-safety boundary ───────────────────────────────────────────────────

# THE SEND-SAFETY BOUNDARY: the email module may ONLY call read tools via MCP.
# Write tools (reply, send, draft, forward, move, update, mark_email_read) are
# NEVER in this set. call_read_tool REFUSES by name any tool NOT in this frozenset
# BEFORE the SDK ever touches the server.
#
# The Graph read tools (get_emails, get_email) from manager-outlook-mcp join the
# legacy aws-outlook-mcp read tools here -- the frozenset gates by tool NAME, so
# call_read_tool accepts both and routes by name (see call_read_tool). The Graph
# WRITE tool mark_email_read is DELIBERATELY ABSENT: it goes through a SEPARATE
# explicitly-gated path (_call_graph_write_tool) so the read client can never
# invoke send/reply/forward/delete/move/mark-read.
_EMAIL_READ_ONLY_TOOLS = frozenset({
    # aws-outlook-mcp (OWA/EWS) legacy reads -- retained for back-compat.
    "email_inbox",
    "email_read",
    "email_search",
    "email_folders",
    "email_list_folders",
    "email_contacts",
    "email_attachments",
    "email_categories",
    # manager-outlook-mcp (Microsoft Graph via GRASP) reads -- the migrated path.
    "get_emails",
    "get_email",
})

# Tools that route to the GRAPH (manager-outlook-mcp) persistent session rather
# than the legacy aws-outlook-mcp session. call_read_tool dispatches by this set.
_GRAPH_READ_TOOLS = frozenset({"get_emails", "get_email"})


# ── Helpers ─────────────────────────────────────────────────────────────────

def _looks_secret(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


def _scrub(text: str) -> str:
    """Drop any line that looks like it carries a credential/cookie/token."""
    if not text:
        return ""
    kept = [ln for ln in text.splitlines() if not _looks_secret(ln)]
    return "\n".join(kept)


def _one_line(text: str, cap: int = 300) -> str:
    """Collapse multi-line text to a single capped line."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    return collapsed[:cap]


def _slugify_contact(contact: str) -> str:
    """Slugify a contact into a filesystem-safe topic-file stem.

    lowercase, non-alphanumeric -> dash, collapse dash runs, strip leading/
    trailing dashes. SHARED by read/write/append/prompt so they all resolve the
    SAME ``email_topics/<slug>.md`` file. An empty slug (e.g. all punctuation)
    returns "" — callers reject it rather than writing a dotfile.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(contact or "").strip().lower())
    return slug.strip("-")


def _validate_contact(contact: str) -> None:
    """Reject a URL contact segment with path-traversal chars BEFORE slugifying.

    Mirrors slack._validate_contact — ``contact`` comes from the URL and indexes a
    topic file. The traversal guard runs first; slugification is a second line of
    defense (it would also strip these), but we fail loud on an obvious attack.
    """
    if not contact or ".." in contact or "/" in contact or "\\" in contact:
        raise web.HTTPBadRequest(reason="invalid contact")


def read_email_style_notes() -> str:
    """Read the human-readable learned-style store (empty string if absent)."""
    content, _ = filestore.read_text(_style_path())
    return content


def read_email_topic_summary(contact: str) -> str:
    """Read a contact's running topic/context summary (empty if absent).

    Resolves the file via the SHARED slug so it matches what _append_email_topic_note
    writes and what get_topics/put_topics serve.
    """
    slug = _slugify_contact(contact)
    if not slug:
        return ""
    content, _ = filestore.read_text(_email_topics_dir() / f"{slug}.md")
    return content


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _ensure_heading(content: str, heading: str) -> str:
    return content if content.strip() else f"{heading}\n"


def _append_email_topic_note(contact: str, item: dict, final_draft: str) -> None:
    """Append a one-line dated note to a contact's topic file (APPEND-ONLY).

    Mirrors slack._append_topic_note: read -> ensure the heading -> append a note
    capturing the subject + a brief draft summary -> write. Resolves the file via
    the SHARED slug (so read/append/prompt all agree). It RE-READS then appends so
    a concurrent note isn't blind-overwritten, and NEVER rewrites/deletes prior
    notes — reconciling duplicates is a human action. This is read back into the
    draft prompt (read_email_topic_summary) so future drafts reference prior
    context.
    """
    slug = _slugify_contact(contact)
    if not slug:
        return
    path = _email_topics_dir() / f"{slug}.md"
    contact_label = _scrub(_one_line(str(contact or ""), 200)) or "this contact"
    content, _ = filestore.read_text(path)
    content = _ensure_heading(content, f"# Topics with {contact_label}")
    subject = _scrub(_one_line(str(item.get("subject", "")), 200))
    note = (
        f"- {_today()}: Re: {subject or '(no subject)'} — "
        f"I replied: {_one_line(str(final_draft))}."
    )
    new_content = content.rstrip("\n") + "\n" + _scrub(note) + "\n"
    filestore.write_text(path, new_content)


def _normalize_loaded(data) -> dict:
    """Apply the in-memory shape normalization shared by _load/_load_async."""
    if not data:
        data = {"items": []}
    if "items" not in data:
        data["items"] = []
    return data


def _load() -> tuple[dict, str | None]:
    """Load the email sidecar, returning (data, etag) -- SYNC (workers/watchers)."""
    data, etag = filestore.read_json(EMAIL_PATH)
    return _normalize_loaded(data), etag


async def _load_async() -> tuple[dict, str | None]:
    """Async sibling of _load for REQUEST-PATH handlers (offloads the read to a
    thread so a slow disk read never blocks the event loop). Background
    workers/watchers keep using the sync _load(). Returns the same (data, etag).
    """
    data, etag = await filestore.async_read_json(EMAIL_PATH)
    return _normalize_loaded(data), etag


# ── MCP config helpers ─────────────────────────────────────────────────────

def _mcp_config_paths() -> list[Path]:
    """Claude config paths to search for MCP server entries."""
    home = Path.home()
    return [
        Path.cwd() / ".mcp.json",
        home / ".claude.json",
        home / ".claude" / "mcp.json",
        home / ".config" / "claude" / "mcp.json",
    ]


def _find_email_mcp_entry() -> dict | None:
    """Best-effort: read the existing aws-outlook-mcp server entry from a config.

    Looks for a server entry whose name contains 'outlook' or 'email' in the
    same config files the Slack module searches. Returns the raw entry dict or
    None if none is readable. NEVER spawns anything.
    """
    def _scan(servers) -> dict | None:
        if not isinstance(servers, dict):
            return None
        for name, entry in servers.items():
            low = str(name).lower()
            if ("outlook" in low or "email" in low) and isinstance(entry, dict):
                return entry
        return None

    for path in _mcp_config_paths():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        found = _scan(doc.get("mcpServers"))
        if found is not None:
            return found
        projects = doc.get("projects")
        if isinstance(projects, dict):
            for proj in projects.values():
                if isinstance(proj, dict):
                    found = _scan(proj.get("mcpServers"))
                    if found is not None:
                        return found
    return None


def _email_mcp_config(*, enable_writes: bool = False) -> dict:
    """Build a minimal --mcp-config doc that loads ONLY the email MCP server.

    When enable_writes is True (save_draft action), pass env
    OUTLOOK_MCP_ENABLE_WRITES=true so the server exposes write tools.
    """
    entry = _find_email_mcp_entry()
    if entry is None:
        entry = dict(_EMAIL_MCP_FALLBACK)
    clean: dict = {}
    for key in _MCP_ENTRY_KEYS:
        if key not in entry:
            continue
        val = entry[key]
        if isinstance(val, str):
            if _looks_secret(val):
                continue
            clean[key] = val
        elif isinstance(val, list):
            clean[key] = [v for v in val if not (isinstance(v, str) and _looks_secret(v))]
        elif isinstance(val, dict):
            clean[key] = {
                k: v for k, v in val.items()
                if not (_looks_secret(str(k)) or (isinstance(v, str) and _looks_secret(v)))
            }
        else:
            clean[key] = val
    if enable_writes:
        env = clean.setdefault("env", {})
        env["OUTLOOK_MCP_ENABLE_WRITES"] = "true"
    return {"mcpServers": {"aws-outlook-mcp": clean}}


def _email_mcp_params() -> StdioServerParameters:
    """Build StdioServerParameters for the email MCP server (persistent session)."""
    entry = _find_email_mcp_entry()
    if entry is None:
        entry = dict(_EMAIL_MCP_FALLBACK)

    command = entry.get("command") or _EMAIL_MCP_FALLBACK["command"]
    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []

    env = None
    raw_env = entry.get("env")
    if isinstance(raw_env, dict):
        env = {
            str(k): str(v)
            for k, v in raw_env.items()
            if not (_looks_secret(str(k)) or _looks_secret(str(v)))
        }
        if not env:
            env = None

    return StdioServerParameters(command=str(command), args=args, env=env)


# ── manager-outlook-mcp (Graph) config + Node-24 resolution ─────────────────

class GraphMcpNodeError(Exception):
    """Node 24+ could not be located for the manager-outlook-mcp subprocess.

    manager-outlook-mcp hard-aborts on Node < 24 (the handshake dies with the
    opaque 'Connection closed'). We raise this BEFORE spawning so the failure is
    actionable instead of presenting as an empty queue ('inbox is empty').
    """


def _find_graph_mcp_entry() -> dict | None:
    """Best-effort: read the manager-outlook-mcp server entry from a claude config.

    Mirrors _find_email_mcp_entry but matches a server name containing
    'manager-outlook' or 'graph' (NOT 'aws-outlook' -- a SIBLING finder so the
    aws-outlook draft path is untouched). Returns the raw entry dict or None.
    NEVER spawns anything.
    """
    def _scan(servers) -> dict | None:
        if not isinstance(servers, dict):
            return None
        for name, entry in servers.items():
            low = str(name).lower()
            if ("manager-outlook" in low or "graph" in low) and isinstance(entry, dict):
                return entry
        return None

    for path in _mcp_config_paths():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        found = _scan(doc.get("mcpServers"))
        if found is not None:
            return found
        projects = doc.get("projects")
        if isinstance(projects, dict):
            for proj in projects.values():
                if isinstance(proj, dict):
                    found = _scan(proj.get("mcpServers"))
                    if found is not None:
                        return found
    return None


def _resolve_node24_bin() -> str:
    """Resolve a Node 24+ bin dir for the manager-outlook-mcp subprocess.

    Precedence: the CLAUDE_WEB_NODE24_BIN env override, else the newest match of
    the mise glob (~/.local/share/mise/installs/node/24*/bin). FAILS LOUD with a
    GraphMcpNodeError naming the expected location + install command when none is
    found -- NEVER silently falls back to the default Node 22 (which produces the
    opaque 'Connection closed').
    """
    override = cfg.node24_bin_override
    if override and Path(override).is_dir():
        return override
    matches = sorted(p for p in glob.glob(_NODE24_GLOB) if Path(p).is_dir())
    if matches:
        # Prefer the newest patch (lexical sort is fine across 24.x dirs).
        return matches[-1]
    raise GraphMcpNodeError(
        "manager-outlook-mcp requires Node 24+, but no Node-24 bin dir was found "
        f"(searched {_NODE24_GLOB} and ${_NODE24_ENV_OVERRIDE}). Install it via "
        "`mise install node@24` (or set "
        f"{_NODE24_ENV_OVERRIDE} to a Node-24 bin dir). Without it the MCP "
        "handshake dies with an opaque 'Connection closed'."
    )


def _graph_mcp_params() -> StdioServerParameters:
    """Build StdioServerParameters for manager-outlook-mcp (Graph) with Node 24.

    Discovers the entry from the same configs _find_email_mcp_entry scans (matching
    'manager-outlook'/'graph'); falls back to the bare `manager-outlook-mcp`
    command. PREPENDS the resolved Node-24 bin dir onto a COPY of PATH in the
    subprocess env (never mutates os.environ, never touches the aws-outlook launch).
    Raises GraphMcpNodeError if Node 24 is absent (fail loud).
    """
    entry = _find_graph_mcp_entry()
    if entry is None:
        entry = dict(_GRAPH_MCP_FALLBACK)

    command = entry.get("command") or _GRAPH_MCP_FALLBACK["command"]
    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []

    # Build the subprocess env from a scrubbed copy of the entry's env, then
    # PREPEND the Node-24 bin dir to PATH for THIS subprocess only.
    env: dict[str, str] = {}
    raw_env = entry.get("env")
    if isinstance(raw_env, dict):
        env = {
            str(k): str(v)
            for k, v in raw_env.items()
            if not (_looks_secret(str(k)) or _looks_secret(str(v)))
        }

    node24_bin = _resolve_node24_bin()  # fail-loud if absent
    base_path = env.get("PATH") or os.environ.get("PATH", "")
    env["PATH"] = node24_bin + (os.pathsep + base_path if base_path else "")

    # Force the OWA auth backend (direct-to-Microsoft) instead of the bundle's
    # DEFAULT 'grasp' backend. This is the durable fix for the quota outage
    # (verified 2026-06-26; see docs/email-owa-backend-runbook.md):
    #   - GRASP routes every call through an API-Gateway proxy with a SHARED
    #     500-requests/day quota AND requests no offline_access -> the bundle's
    #     TokenRefreshRunner does a FULL SAML re-auth every ~10 min, draining that
    #     quota until every call (reads AND delete) 429s `quota_exceeded`.
    #   - OWA hits login.microsoftonline.com + outlook.office.com DIRECTLY (no
    #     GRASP quota) and its scope includes offline_access -> it holds a real
    #     refresh_token and refreshes cheaply, so there is no SAML storm.
    # setdefault so an explicit override in ~/.claude.json's mcpServers env still
    # wins; the safe baked-in default is owa. Tokens cache to ~/.o365_mcp/.
    env.setdefault("MMCP_AUTH_BACKEND", "owa")

    return StdioServerParameters(command=str(command), args=args, env=env)


# ── Persistent MCP session state ──────────────────────────────────────────

@dataclass
class _PersistentMcpState:
    """Holds the long-lived email MCP session + context managers for teardown."""
    session: ClientSession | None = None
    read_stream: object | None = None
    write_stream: object | None = None
    stdio_cm: object | None = None
    session_cm: object | None = None
    connected_at: float | None = None


_mcp_state = _PersistentMcpState()
_mcp_connect_lock: asyncio.Lock | None = None

# Second persistent session: manager-outlook-mcp (Microsoft Graph via GRASP). It
# is the migrated READ path (get_emails/get_email). It has its OWN state +
# connect-lock, mirroring the aws-outlook session.
_graph_mcp_state = _PersistentMcpState()
_graph_mcp_connect_lock: asyncio.Lock | None = None

# Wall-clock epoch of the last Graph call that actually ran on the PERSISTENT
# session. The idle reaper (_reap_idle_graph_session) tears the session down once
# no Graph call has run for _GRAPH_IDLE_TEARDOWN_S (or the queue is paused) so the
# bundle's TokenRefreshRunner can't churn auth while the tab is idle. Stamped only
# from the persistent path; the short-lived write spawn does NOT stamp it (it
# spawns + exits, leaving no refresh timer to keep alive).
_graph_last_activity: float = 0.0

# Third persistent session: aws-outlook-mcp with WRITES ENABLED (for email_draft).
# Distinct from _mcp_state (which is read-only) and _graph_mcp_state (Graph tools).
_owa_write_state = _PersistentMcpState()
_owa_write_lock: asyncio.Lock | None = None

# Strong refs to in-flight fire-and-forget mark-read tasks so the event loop
# does not GC them mid-flight (asyncio only holds weak refs to bare tasks).
_MARK_READ_TASKS: set = set()


def _get_mcp_lock() -> asyncio.Lock:
    """Lazy-init the module-level connect/reconnect lock."""
    global _mcp_connect_lock
    if _mcp_connect_lock is None:
        _mcp_connect_lock = asyncio.Lock()
    return _mcp_connect_lock


def _get_graph_mcp_lock() -> asyncio.Lock:
    """Lazy-init the graph (manager-outlook-mcp) connect/reconnect lock."""
    global _graph_mcp_connect_lock
    if _graph_mcp_connect_lock is None:
        _graph_mcp_connect_lock = asyncio.Lock()
    return _graph_mcp_connect_lock


async def _connect_mcp() -> None:
    """Lazily connect the persistent email MCP session (idempotent under lock).

    Acquires the connect lock, short-circuits if already connected, else spawns
    the email MCP server via stdio_client, performs the initialize handshake,
    and stores everything in _mcp_state. On any exception during connect, tears
    down partially-opened resources and re-raises.
    """
    lock = _get_mcp_lock()
    async with lock:
        if _mcp_state.session is not None:
            return  # Already connected.

        params = _email_mcp_params()
        stdio_cm = None
        session_cm = None
        try:
            stdio_cm = stdio_client(params)
            read_stream, write_stream = await stdio_cm.__aenter__()

            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()

            await session.initialize()

            _mcp_state.session = session
            _mcp_state.stdio_cm = stdio_cm
            _mcp_state.session_cm = session_cm
            _mcp_state.read_stream = read_stream
            _mcp_state.write_stream = write_stream
            _mcp_state.connected_at = time.time()
            logger.info("Email persistent MCP session connected.")
        except BaseException:
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            if stdio_cm is not None:
                try:
                    await stdio_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            raise


async def _disconnect_mcp() -> None:
    """Tear down the persistent email MCP session (idempotent).

    Acquires the connect lock so a concurrent connect can't race the teardown.
    """
    lock = _get_mcp_lock()
    async with lock:
        if _mcp_state.session is None:
            return

        if _mcp_state.session_cm is not None:
            try:
                await _mcp_state.session_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Email MCP session_cm teardown: %s", _scrub(str(e)))
        if _mcp_state.stdio_cm is not None:
            try:
                await _mcp_state.stdio_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Email MCP stdio_cm teardown: %s", _scrub(str(e)))

        _mcp_state.session = None
        _mcp_state.stdio_cm = None
        _mcp_state.session_cm = None
        _mcp_state.read_stream = None
        _mcp_state.write_stream = None
        _mcp_state.connected_at = None
        logger.info("Email persistent MCP session disconnected.")


async def _connect_graph_mcp() -> None:
    """Lazily connect the persistent manager-outlook-mcp (Graph) session.

    Mirrors _connect_mcp exactly but spawns manager-outlook-mcp with Node 24 on
    PATH (via _graph_mcp_params). Idempotent under its OWN connect lock. A missing
    Node 24 raises GraphMcpNodeError (fail loud) before any spawn.
    """
    lock = _get_graph_mcp_lock()
    async with lock:
        if _graph_mcp_state.session is not None:
            return  # Already connected.

        params = _graph_mcp_params()  # may raise GraphMcpNodeError (fail loud)
        stdio_cm = None
        session_cm = None
        try:
            stdio_cm = stdio_client(params)
            read_stream, write_stream = await stdio_cm.__aenter__()

            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()

            await session.initialize()

            _graph_mcp_state.session = session
            _graph_mcp_state.stdio_cm = stdio_cm
            _graph_mcp_state.session_cm = session_cm
            _graph_mcp_state.read_stream = read_stream
            _graph_mcp_state.write_stream = write_stream
            _graph_mcp_state.connected_at = time.time()
            logger.info("Email Graph (manager-outlook-mcp) MCP session connected.")
        except BaseException:
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            if stdio_cm is not None:
                try:
                    await stdio_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            raise


async def _disconnect_graph_mcp() -> None:
    """Tear down the persistent manager-outlook-mcp (Graph) session (idempotent)."""
    lock = _get_graph_mcp_lock()
    async with lock:
        if _graph_mcp_state.session is None:
            return

        if _graph_mcp_state.session_cm is not None:
            try:
                await _graph_mcp_state.session_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Graph MCP session_cm teardown: %s", _scrub(str(e)))
        if _graph_mcp_state.stdio_cm is not None:
            try:
                await _graph_mcp_state.stdio_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Graph MCP stdio_cm teardown: %s", _scrub(str(e)))

        _graph_mcp_state.session = None
        _graph_mcp_state.stdio_cm = None
        _graph_mcp_state.session_cm = None
        _graph_mcp_state.read_stream = None
        _graph_mcp_state.write_stream = None
        _graph_mcp_state.connected_at = None
        logger.info("Email Graph (manager-outlook-mcp) MCP session disconnected.")


def _get_owa_write_lock() -> asyncio.Lock:
    """Lazy-init the aws-outlook-mcp write session connect lock."""
    global _owa_write_lock
    if _owa_write_lock is None:
        _owa_write_lock = asyncio.Lock()
    return _owa_write_lock


def _owa_write_params() -> StdioServerParameters:
    """Build StdioServerParameters for aws-outlook-mcp WITH writes enabled."""
    entry = _find_email_mcp_entry()
    if entry is None:
        entry = dict(_EMAIL_MCP_FALLBACK)

    command = entry.get("command") or _EMAIL_MCP_FALLBACK["command"]
    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []

    raw_env = entry.get("env")
    env = {}
    if isinstance(raw_env, dict):
        env = {
            str(k): str(v)
            for k, v in raw_env.items()
            if not (_looks_secret(str(k)) or _looks_secret(str(v)))
        }
    env["OUTLOOK_MCP_ENABLE_WRITES"] = "true"
    return StdioServerParameters(command=str(command), args=args, env=env)


async def _connect_owa_write() -> None:
    """Lazily connect the persistent aws-outlook-mcp write session."""
    lock = _get_owa_write_lock()
    async with lock:
        if _owa_write_state.session is not None:
            return

        params = _owa_write_params()
        stdio_cm = None
        session_cm = None
        try:
            stdio_cm = stdio_client(params)
            read_stream, write_stream = await stdio_cm.__aenter__()

            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()

            await session.initialize()

            _owa_write_state.session = session
            _owa_write_state.stdio_cm = stdio_cm
            _owa_write_state.session_cm = session_cm
            _owa_write_state.read_stream = read_stream
            _owa_write_state.write_stream = write_stream
            _owa_write_state.connected_at = time.time()
            logger.info("Email OWA write (aws-outlook-mcp) MCP session connected.")
        except BaseException:
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            if stdio_cm is not None:
                try:
                    await stdio_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            raise


async def _disconnect_owa_write() -> None:
    """Tear down the persistent aws-outlook-mcp write session."""
    lock = _get_owa_write_lock()
    async with lock:
        if _owa_write_state.session is None:
            return

        if _owa_write_state.session_cm is not None:
            try:
                await _owa_write_state.session_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("OWA write session_cm teardown: %s", _scrub(str(e)))
        if _owa_write_state.stdio_cm is not None:
            try:
                await _owa_write_state.stdio_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("OWA write stdio_cm teardown: %s", _scrub(str(e)))

        _owa_write_state.session = None
        _owa_write_state.stdio_cm = None
        _owa_write_state.session_cm = None
        _owa_write_state.read_stream = None
        _owa_write_state.write_stream = None
        _owa_write_state.connected_at = None
        logger.info("Email OWA write (aws-outlook-mcp) MCP session disconnected.")


_OWA_GATED_WRITE_TOOLS = frozenset({"email_draft"})


async def _call_owa_write_tool(name: str, arguments: dict) -> object:
    """Gated path for aws-outlook-mcp write tools (email_draft only)."""
    if name not in _OWA_GATED_WRITE_TOOLS:
        raise EmailMcpError(
            f"Refusing OWA write tool {name!r}: not in the gated allowlist."
        )
    return await _call_session_tool(
        name=name, arguments=arguments, state=_owa_write_state,
        connect=_connect_owa_write, disconnect=_disconnect_owa_write,
        label="Email OWA write",
    )


class EmailMcpError(Exception):
    """A direct-MCP read failed (connect/handshake/call/parse/tool error)."""


def _is_transient_mcp_error(exc: BaseException) -> bool:
    """Recursively unwrap exc to decide if it is a retryable stream break."""
    seen: set[int] = set()

    def _walk(e: BaseException | None, depth: int) -> bool:
        if e is None or depth > 20 or id(e) in seen:
            return False
        seen.add(id(e))
        if isinstance(e, (BrokenPipeError, ConnectionResetError)):
            return True
        if isinstance(e, OSError) and getattr(e, "errno", None) in (
            os.errno.EPIPE if hasattr(os, "errno") else None,
        ):
            return True
        # Try anyio types if available (they're our dep via mcp SDK)
        try:
            import anyio
            if isinstance(e, (anyio.BrokenResourceError, anyio.ClosedResourceError,
                              anyio.EndOfStream)):
                return True
        except ImportError:
            pass
        for sub in getattr(e, "exceptions", None) or ():
            if _walk(sub, depth + 1):
                return True
        if _walk(getattr(e, "__cause__", None), depth + 1):
            return True
        if _walk(getattr(e, "__context__", None), depth + 1):
            return True
        return False

    return _walk(exc, 0)


# ── MCP tool guard ──────────────────────────────────────────────────────────

async def _call_session_tool(*, name: str, arguments: dict, state: _PersistentMcpState,
                             connect, disconnect, label: str) -> object:
    """Shared persistent-session call: connect -> call_tool -> normalize, with a
    reconnect-once retry on a transient stream break. FAIL LOUD on a tool error.

    Used by BOTH the read path (aws-outlook / graph reads) and the separate gated
    graph write path; the boundary refusal lives in the callers, NOT here.
    """
    for attempt in range(2):
        try:
            await connect()
            if state.session is None:
                raise EmailMcpError(f"{label} MCP session failed to connect.")

            # Stamp last-activity on the PERSISTENT Graph session so the idle reaper
            # only tears down a genuinely-quiet session. The one-shot write spawn
            # uses its OWN state, so it never refreshes this clock.
            if state is _graph_mcp_state:
                global _graph_last_activity
                _graph_last_activity = time.time()

            result = await state.session.call_tool(name, arguments)

            if getattr(result, "isError", False):
                # UN-TRUNCATED error body: the HTTP status code + GRASP error code
                # ("quota_exceeded" / "-> 429") often sit at the END of a long body,
                # so a 200-char cap chopped exactly the bytes an operator needs to
                # tell a GRASP-quota 429 from a Microsoft Graph throttle. Cap at a
                # generous 2000 chars so the code+reason always survive.
                raise EmailMcpError(
                    _scrub(f"{label} MCP tool {name!r} returned an error: "
                           + _one_line(str(getattr(result, "content", "")), 2000))
                )

            return _normalize_tool_result(result)

        except EmailMcpError:
            raise
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if _is_transient_mcp_error(e) and attempt == 0:
                logger.debug("%s MCP transient error, reconnecting: %s", label, _scrub(str(e)))
                await disconnect()
                await asyncio.sleep(1.0)
                continue
            raise EmailMcpError(_scrub(f"{label} MCP call failed: {e}")) from e

    raise EmailMcpError(f"{label} MCP call failed after retry.")


async def call_read_tool(name: str, arguments: dict) -> object:
    """Call ONE email MCP READ tool via a PERSISTENT session; FAIL LOUD on error.

    THE SEND-SAFETY BOUNDARY: rejects by name BEFORE any connection or SDK
    interaction (the by-name refusal is FIRST, before any connect). Routes by tool
    name -- Graph reads (get_emails/get_email) to the manager-outlook-mcp session,
    everything else to the legacy aws-outlook-mcp session.
    """
    if name not in _EMAIL_READ_ONLY_TOOLS:
        raise EmailMcpError(
            f"Refusing to call email tool {name!r}: not in the read-only allowlist."
        )

    if name in _GRAPH_READ_TOOLS:
        return await _call_session_tool(
            name=name, arguments=arguments, state=_graph_mcp_state,
            connect=_connect_graph_mcp, disconnect=_disconnect_graph_mcp,
            label="Email Graph",
        )
    return await _call_session_tool(
        name=name, arguments=arguments, state=_mcp_state,
        connect=_connect_mcp, disconnect=_disconnect_mcp, label="Email",
    )


# Graph WRITE tools reachable ONLY through the separate gated path below. This is
# DELIBERATELY tiny: mark_email_read is the only write this change authorizes. It
# is NOT in _EMAIL_READ_ONLY_TOOLS and NEVER routes through call_read_tool, so the
# read client can never reach send/reply/forward/delete/move.
_GRAPH_GATED_WRITE_TOOLS = frozenset({"mark_email_read", "delete_email"})


async def _call_graph_write_tool(name: str, arguments: dict) -> object:
    """SEPARATE explicitly-gated path for the one authorized Graph WRITE tool.

    Distinct from call_read_tool by design: it accepts ONLY the gated write tools
    (mark_email_read), routes to the manager-outlook-mcp session, and FAILS LOUD on
    anything else -- the structural guarantee that the read client never sends.
    """
    if name not in _GRAPH_GATED_WRITE_TOOLS:
        raise EmailMcpError(
            f"Refusing graph write tool {name!r}: not in the gated write allowlist."
        )
    return await _call_session_tool(
        name=name, arguments=arguments, state=_graph_mcp_state,
        connect=_connect_graph_mcp, disconnect=_disconnect_graph_mcp,
        label="Email Graph write",
    )


async def _call_graph_write_tool_oneshot(name: str, arguments: dict) -> object:
    """SHORT-LIVED spawn-on-demand path for a gated Graph WRITE tool.

    A user-initiated write (delete_email, mark_email_read) must NOT depend on the
    always-on persistent session (which we tear down while idle/paused so the
    bundle's TokenRefreshRunner can't churn auth in the background). So this spawns a
    FRESH manager-outlook-mcp subprocess, reuses the cached on-disk auth token
    (~/.o365_mcp on the default OWA backend; _graph_mcp_params forces that backend),
    performs the single tool call, and ALWAYS tears the subprocess down in a finally
    -- nothing keeps a refresh timer alive afterward.

    SAME boundary as the persistent path: it refuses any name not in
    _GRAPH_GATED_WRITE_TOOLS FIRST, before any spawn. It does NOT stamp
    _graph_last_activity (it owns its own subprocess; there is no persistent session
    to reap). FAILS LOUD (EmailMcpError) on connect/handshake/call/tool error so a
    quota-exhausted delete surfaces honestly instead of silently "succeeding".
    """
    if name not in _GRAPH_GATED_WRITE_TOOLS:
        raise EmailMcpError(
            f"Refusing graph write tool {name!r}: not in the gated write allowlist."
        )

    params = _graph_mcp_params()  # may raise GraphMcpNodeError (fail loud)
    stdio_cm = None
    session_cm = None
    try:
        stdio_cm = stdio_client(params)
        read_stream, write_stream = await stdio_cm.__aenter__()

        session_cm = ClientSession(read_stream, write_stream)
        session = await session_cm.__aenter__()
        await session.initialize()

        result = await session.call_tool(name, arguments)
        if getattr(result, "isError", False):
            raise EmailMcpError(
                _scrub(f"Email Graph write (one-shot) tool {name!r} returned an "
                       "error: " + _one_line(str(getattr(result, "content", "")), 2000))
            )
        return _normalize_tool_result(result)
    except EmailMcpError:
        raise
    except asyncio.CancelledError:
        raise
    except GraphMcpNodeError:
        raise
    except BaseException as e:
        raise EmailMcpError(
            _scrub(f"Email Graph write (one-shot) call failed: {e}")
        ) from e
    finally:
        if session_cm is not None:
            try:
                await session_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Graph one-shot session_cm teardown: %s", _scrub(str(e)))
        if stdio_cm is not None:
            try:
                await stdio_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Graph one-shot stdio_cm teardown: %s", _scrub(str(e)))


def _normalize_tool_result(result) -> object:
    """Normalize a CallToolResult into a plain dict/list/str."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, (dict, list)):
        return structured

    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    blob = "".join(parts).strip()
    if not blob:
        return {}
    # Strip <untrusted_content_...> XML safety markers and trailing safety
    # directives ("IMPORTANT: ...") that the Outlook MCP wraps responses in.
    blob = re.sub(r"</?untrusted_content[^>]*>", "", blob).strip()
    # Remove trailing "IMPORTANT: ..." safety directive (after the JSON)
    imp_match = re.search(r"\n\s*IMPORTANT:.*$", blob, re.DOTALL)
    if imp_match:
        blob = blob[: imp_match.start()].strip()
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # Try to find the first { or [ and parse the JSON object from there
        for i, ch in enumerate(blob):
            if ch in "{[":
                # Try progressively shorter suffixes to handle trailing text
                remainder = blob[i:]
                try:
                    return json.loads(remainder)
                except json.JSONDecodeError:
                    pass
                # Try scanning from the end for the matching close
                close = "}" if ch == "{" else "]"
                rpos = remainder.rfind(close)
                if rpos > 0:
                    try:
                        return json.loads(remainder[: rpos + 1])
                    except json.JSONDecodeError:
                        pass
                break
        return blob


_QUOTA_BODY_RE = re.compile(r"quota_exceeded", re.IGNORECASE)
_HTTP_429_RE = re.compile(r"(?<!\d)429(?!\d)")


def _is_graph_quota_error(exc_or_result) -> bool:
    """True if exc/result is an HTTP 429 or a GRASP ``quota_exceeded`` body.

    Distinguishes the GRASP API-Gateway quota exhaustion (no Retry-After, body
    ``{"error":"quota_exceeded"}`` with x-amz-apigw-id headers) from an ordinary
    400 so the scan worker can back off ONLY on the condition that warrants it.
    Matches the EmailMcpError message text we raise un-truncated (which carries the
    full body incl. the status code and error code), a raw string, or a dict body.
    """
    if exc_or_result is None:
        return False
    if isinstance(exc_or_result, dict):
        text = json.dumps(exc_or_result)
    else:
        text = str(exc_or_result)
    if not text:
        return False
    return bool(_QUOTA_BODY_RE.search(text) or _HTTP_429_RE.search(text))


# ── Prompt builders ────────────────────────────────────────────────────────

def _build_prompt(action: str, payload: dict) -> str:
    """Build the STRICT-JSON-returning prompt for one seam action."""
    if action == "classify":
        items = payload.get("items") or []
        lines = []
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id", ""))
            sender = _one_line(str(it.get("sender", "")), 200)
            subject = _one_line(str(it.get("subject", "")), 300)
            snippet = _one_line(str(it.get("snippet", "")), 1000)
            lines.append(
                f"- id: {iid}\n  from: {sender}\n  subject: {subject}\n  snippet: {snippet}"
            )
        rendered = "\n".join(lines) if lines else "(no items)"
        prompt = (
            "Classify each of MY unread emails below -- do NOT call any tools, "
            "do NOT fetch anything, do NOT send anything. For each email, classify "
            "as needs-reply (someone is asking me a question or requesting action), "
            "fyi (notification, acknowledgment, newsletter, no reply needed), or "
            "actionable (I need to do something but not reply by email).\n\n"
            "Emails:\n"
            f"{rendered}\n\n"
            "Return ONLY a STRICT JSON object (no prose, no markdown fences): "
            '{"classifications": [{"id": "<id>", "classification": "<label>"}]}. '
            'classification must be exactly one of "needs-reply", "fyi", or "actionable". '
            "Include every id above exactly once."
        )
    elif action in ("draft", "regenerate"):
        # When the caller pre-assembled the rich style+topic+threadHistory prompt
        # (_build_email_draft_prompt), use it verbatim as the base. Fall back to the
        # inline build only when no prompt was injected (older call sites/tests).
        base = str(payload.get("prompt", "")).strip()
        if not base:
            item = payload.get("item") or {}
            subject = _one_line(str(item.get("subject", "")), 300)
            sender = _one_line(str(item.get("sender", "")), 200)
            body = _safe_body_truncate(str(item.get("emailBody", "")), _EMAIL_BODY_CAP)
            snippet = str(item.get("snippet", ""))[:_SNIPPET_CAP]
            # Use full body if available, else snippet
            context = body if body.strip() else snippet
            # Load style notes if available
            style_content = ""
            try:
                style_text, _ = filestore.read_text(_style_path())
                if style_text:
                    style_content = f"\n\n## Style notes\n{style_text}"
            except (OSError, UnicodeDecodeError):
                pass
            base = (
                f"{style_content}\n\n"
                f"## Email to reply to\n"
                f"From: {sender}\n"
                f"Subject: {subject}\n\n"
                f"{context}"
            )
        prompt = (
            "Draft a reply to this email in MY voice using ONLY the context below "
            "-- do NOT call any tools, do NOT fetch anything, do NOT send anything."
            f"\n\n{base}\n\n"
            "Return ONLY a STRICT JSON object (no prose, no markdown fences): "
            '{"draft": "<the reply text>", "generatedDraft": "<the same reply text>", '
            '"threadContext": "<a 1-2 sentence summary of what the email is about>", '
            '"threadAsk": "<a short phrase naming what the sender needs FROM ME -- a '
            "question, decision, or action they are waiting on; if the email asks "
            "nothing of me (an FYI, newsletter, or automated notice) return an EMPTY "
            'string; do NOT invent an ask>"}. '
            "generatedDraft must equal draft."
        )
    elif action == "polish":
        # MCP-FREE: polish the user's CURRENT draft for fluency while preserving
        # THEIR voice and language. It reasons ONLY over the text in the prompt — no
        # tool calls, no fetch, no send. Deliberately conservative: improve
        # flow/grammar/clarity but do NOT change meaning, translate, or formalize.
        text = str(payload.get("text", ""))
        prompt = (
            "Improve the FLUENCY of the email reply below — do NOT call any tools, "
            "do NOT fetch anything, do NOT send anything. Fix grammar, awkward "
            "phrasing, and clarity so it reads smoothly, but you MUST preserve MY "
            "own voice and writing style and the SAME language the text is written "
            "in (do NOT translate). Do NOT change the meaning, do NOT add or remove "
            "information, do NOT make it more formal or more casual than I wrote it, "
            "and keep it roughly the same length. If the text is already fluent, "
            "return it essentially unchanged.\n\n"
            "My draft:\n"
            f"{text}\n\n"
            'Return ONLY a STRICT JSON object (no prose, no markdown fences): '
            '{"draft": "<the polished reply text>"}.'
        )
    elif action == "save_draft":
        item = payload.get("item") or {}
        draft_text = str(payload.get("draft", ""))
        conv_id = str(item.get("conversationId", ""))
        subject = _one_line(str(item.get("subject", "")), 300)
        prompt = (
            "Save this text as an Outlook draft reply using the email_draft tool. "
            "Pass it exactly as written -- do not modify the text.\n\n"
            f"Conversation ID: {conv_id}\n"
            f"Subject: {subject}\n"
            f"Draft text:\n{draft_text}\n\n"
            "Return ONLY a STRICT JSON object (no prose, no markdown fences): "
            '{"draftSaved": true, "draftId": "<the draft id>"}. '
            'If the save fails, return {"draftSaved": false, "draftId": null}.'
        )
    else:
        prompt = f"Unknown action: {action}. Return ONLY {{}}."
    return _scrub(prompt)


def _parse_agent_result(text: str, action: str) -> dict:
    """Parse the subprocess's final result text as STRICT JSON.

    On any unparseable/empty/wrong-shape reply, return a fail-loud unavailable
    result. NEVER fabricate data.
    """
    text = (text or "").strip()
    if not text:
        return {"available": False, "reason": "Email agent returned an empty reply."}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"available": False, "reason": _scrub(
            "Email agent reply was not valid JSON: " + _one_line(text, 200))}

    if action == "classify":
        if not isinstance(parsed, dict) or "classifications" not in parsed:
            return {"available": False, "reason": "Email classify reply missing 'classifications'."}
        classifications = parsed["classifications"]
        if not isinstance(classifications, list):
            return {"available": False, "reason": "Email classify classifications is not an array."}
        return {"available": True, "classifications": classifications}
    if action in ("draft", "regenerate"):
        # 'draft' is the ONLY required key; threadContext (summary) and threadAsk
        # (what the sender needs from me) are OPTIONAL -- a missing ask resolves to
        # "" and must NEVER make a good draft unavailable nor get fabricated.
        if not isinstance(parsed, dict) or "draft" not in parsed:
            return {"available": False, "reason": f"Email {action} reply missing 'draft'."}
        return {
            "available": True,
            "draft": parsed.get("draft", ""),
            "generatedDraft": parsed.get("generatedDraft", parsed.get("draft", "")),
            "threadContext": parsed.get("threadContext", ""),
            "threadAsk": parsed.get("threadAsk", ""),
        }
    if action == "polish":
        # Fail loud on a missing 'draft' rather than fabricating a polished text.
        if not isinstance(parsed, dict) or "draft" not in parsed:
            return {"available": False, "reason": "Email polish reply missing 'draft'."}
        return {"available": True, "draft": parsed.get("draft", "")}
    if action == "save_draft":
        if not isinstance(parsed, dict):
            return {"available": False, "reason": "Email save_draft reply was not a dict."}
        return {
            "available": True,
            "draftSaved": bool(parsed.get("draftSaved")),
            "draftId": parsed.get("draftId"),
        }
    return {"available": False, "reason": f"Unknown action: {action}."}


# ── Subprocess reaping ─────────────────────────────────────────────────────

def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Send a signal to the child's whole process GROUP, falling back to pid."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError, RuntimeError):
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Terminate->kill the subprocess GROUP with bounded waits (no orphan left)."""
    if proc.returncode is not None:
        return
    if proc.stdin:
        try:
            proc.stdin.close()
        except (OSError, RuntimeError):
            pass

    async def _wait(timeout: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except (RuntimeError, ProcessLookupError):
            return True

    if await _wait(5):
        return
    _signal_group(proc, signal.SIGTERM)
    if await _wait(5):
        return
    _signal_group(proc, signal.SIGKILL)
    await _wait(5)


def _unlink_quiet(path: str) -> None:
    """Best-effort remove of the temp --mcp-config file."""
    try:
        os.unlink(path)
    except OSError:
        pass


# ── Delegation seam ─────────────────────────────────────────────────────────

def _timeout_for(action: str) -> float:
    """Per-action timeout budget."""
    return _SAVE_DRAFT_TIMEOUT_S if action == "save_draft" else _AGENT_TIMEOUT_S


async def _drive_agent(action: str, payload: dict) -> dict:
    """Spawn ONE `claude --print` subprocess, run the action, parse STRICT JSON.

    MCP-FREE actions (classify, draft, regenerate): --strict-mcp-config with an
    empty {"mcpServers": {}} config and NO --allowedTools.
    MCP action (save_draft): email MCP config with write tool allowed.
    """
    claude_bin = shutil.which("claude") or "claude"

    # Only save_draft needs MCP (the write tool).
    needs_mcp = action == "save_draft"
    if needs_mcp:
        allowed_tools = "mcp__aws-outlook-mcp__email_draft"
    else:
        allowed_tools = ""

    # Always write a --mcp-config + --strict-mcp-config to prevent inheriting
    # global MCP servers.
    mcp_config_path: str | None = None
    mcp_doc = _email_mcp_config(enable_writes=True) if needs_mcp else {"mcpServers": {}}
    try:
        fd, mcp_config_path = tempfile.mkstemp(prefix="email-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(mcp_doc, fh)
    except OSError:
        if mcp_config_path:
            _unlink_quiet(mcp_config_path)
        mcp_config_path = None

    args = [
        claude_bin, "--print",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
    ]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path, "--strict-mcp-config"]
    else:
        args += ["--strict-mcp-config"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.cwd()),
            limit=_STREAM_LIMIT_BYTES,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as e:
        if mcp_config_path:
            _unlink_quiet(mcp_config_path)
        return {"available": False, "reason": _scrub(f"Could not spawn claude: {e}")}

    stderr_tail: list[str] = []

    async def _drain_stderr():
        if proc.stderr:
            try:
                async for raw in proc.stderr:
                    line = raw.decode(errors="replace").rstrip("\n")
                    if line:
                        stderr_tail.append(line)
                        if len(stderr_tail) > 50:
                            stderr_tail[:] = stderr_tail[-50:]
            except ValueError:
                pass

    stderr_task = asyncio.create_task(_drain_stderr())

    prompt = _build_prompt(action, payload)
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    })

    async def _run() -> dict:
        if proc.stdin:
            proc.stdin.write((msg + "\n").encode())
            await proc.stdin.drain()
        result_text = ""
        result_evt: dict | None = None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "result":
                result_evt = evt
                result_text = str(evt.get("result", ""))
                break
        return {"text": result_text, "evt": result_evt}

    timeout_s = _timeout_for(action)
    try:
        outcome = await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return {"available": False, "reason": _scrub(
            f"Email agent timed out after {timeout_s}s.")}
    except (OSError, RuntimeError, ValueError) as e:
        return {"available": False, "reason": _scrub(f"Email agent I/O error: {e}")}
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        await _reap(proc)
        if mcp_config_path:
            _unlink_quiet(mcp_config_path)

    stderr_blob = "\n".join(stderr_tail)
    evt = outcome["evt"]
    result_text = outcome["text"]

    # Auth failure detection
    auth_blob = " ".join(filter(None, [result_text, stderr_blob]))
    if is_auth_error(auth_blob):
        return {"available": False, "reason": _scrub(
            "Email agent failed with an auth/credential error -- re-authenticate "
            "and retry.")}

    # Non-zero exit or error-flagged result -> fail loud
    if proc.returncode not in (0, None):
        return {"available": False, "reason": _scrub(
            f"Email agent exited with code {proc.returncode}. "
            + _one_line(stderr_blob, 200))}
    if evt is None:
        return {"available": False, "reason": _scrub(
            "Email agent produced no result event. " + _one_line(stderr_blob, 200))}
    if evt.get("is_error"):
        return {"available": False, "reason": _scrub(
            "Email agent reported an error: " + _one_line(result_text, 200))}

    return _parse_agent_result(result_text, action)


async def _run_email_agent(action: str, payload: dict) -> dict:
    """THE seam: the ONLY function that delegates to the email agent.

    Actions:
      'classify'   -> {available:True, classifications:[...]}
      'draft'      -> {available:True, draft:..., generatedDraft:..., threadContext:..., threadAsk:...}
      'regenerate' -> {available:True, draft:..., generatedDraft:..., threadContext:..., threadAsk:...}
      'save_draft' -> {available:True, draftSaved:True/False, draftId:...}

    Tests monkeypatch this to exercise the contract without spawning anything.
    FAIL-LOUD: on any failure returns {available:False, reason:<scrubbed>}.
    NEVER sends email directly.
    """
    return await _drive_agent(action, payload)


# ── Workers (scan, classify, draft) ────────────────────────────────────────

# Module-level asyncio primitives (lazily created, reset by tests).
_scan_in_progress_lock: asyncio.Lock | None = None
_scan_wake_event: asyncio.Event | None = None

# Tracks last successful scan (epoch ms) -- survives across cycles.
_LAST_SCAN_TS_MS: int | None = None


def _get_scan_lock() -> asyncio.Lock:
    global _scan_in_progress_lock
    if _scan_in_progress_lock is None:
        _scan_in_progress_lock = asyncio.Lock()
    return _scan_in_progress_lock


def _get_scan_event() -> asyncio.Event:
    global _scan_wake_event
    if _scan_wake_event is None:
        _scan_wake_event = asyncio.Event()
    return _scan_wake_event


def _graph_node_status() -> dict:
    """Resolve the Node-24 readiness for manager-outlook-mcp WITHOUT spawning.

    Returns {graphNodeReady: bool, graphNodeReason: <actionable msg or ''>}. A
    missing Node 24 is the SILENT-NODE-22-FALLBACK trap (an empty queue that reads
    as 'inbox is empty'), so we surface the actionable reason instead of guessing.
    """
    try:
        _resolve_node24_bin()
        return {"graphNodeReady": True, "graphNodeReason": ""}
    except GraphMcpNodeError as e:
        return {"graphNodeReady": False, "graphNodeReason": _scrub(str(e))}


def _probe_readiness() -> dict:
    """Report seam readiness WITHOUT any I/O.

    Returns whether claude is on PATH, email MCP is configured, and whether the
    Graph (manager-outlook-mcp) read path can launch (Node 24 present). The scan
    READ path now needs Node 24; a missing Node 24 surfaces as an actionable
    reason rather than an empty queue.
    """
    claude_on_path = shutil.which("claude") is not None
    mcp_entry = _find_email_mcp_entry()
    mcp_configured = mcp_entry is not None
    node = _graph_node_status()
    ready = claude_on_path  # MCP-FREE actions (classify/draft) don't need MCP
    return {
        "claudeOnPath": claude_on_path,
        "mcpConfigured": mcp_configured,
        "graphNodeReady": node["graphNodeReady"],
        "graphNodeReason": node["graphNodeReason"],
        "ready": ready,
    }


def _mute_key_for(item: dict) -> str:
    """Derive a stable Graph-native mute key from an item.

    Keys on the Graph messageId (the conversationId is an alias of it, see D-051),
    so a mute survives the conversationId->messageId re-key. Falls back to
    conversationId then the queue id so an older item never produces an empty key.
    """
    msg_id = str(item.get("messageId", "") or "").strip()
    if msg_id:
        return msg_id
    conv_id = str(item.get("conversationId", "") or "").strip()
    return conv_id or str(item.get("id", ""))


def _prune_muted(muted: dict, now_s: float) -> dict:
    """Remove mute entries older than _MUTE_TTL_S (30 days)."""
    return {k: v for k, v in muted.items()
            if now_s - float(v) < _MUTE_TTL_S}


def _unwrap_list(payload, *keys) -> list:
    """Best-effort: pull a list from an MCP tool payload (searches 2 levels deep)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Level 1: check named keys at top
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return val
        # Level 2: check named keys one level down (e.g. content.emails)
        for v in payload.values():
            if isinstance(v, dict):
                for key in keys:
                    val = v.get(key)
                    if isinstance(val, list):
                        return val
        # Fallback: first list value at top level
        for val in payload.values():
            if isinstance(val, list):
                return val
    return []


_MY_EMAIL = cfg.my_email


def _detail_recipients(payload, key: str) -> list:
    """Pull the ``to``/``cc`` recipient list ({name,email} dicts) from a Graph
    get_email DETAIL payload, tolerating the ``{email:{...}}`` nesting.

    The inbox-LIST call (get_emails) carries NO recipients, so this reads the
    detail payload only. Returns [] (never None) for a missing key / wrong shape
    so callers never fabricate. ``key`` is "to" or "cc".
    """
    if not isinstance(payload, dict):
        return []
    src = payload.get("email") if isinstance(payload.get("email"), dict) else payload
    val = src.get(key) if isinstance(src, dict) else None
    if not isinstance(val, list):
        return []
    return [e for e in val if isinstance(e, dict)]


def _capture_recipients(target: dict, body_payload) -> None:
    """Persist the detail payload's ``to``/``cc`` onto ``target`` as
    ``toRecipients``/``ccRecipients`` (lists of {name,email}).

    Writes ONLY when the detail carries a non-empty list for a key, so a body-less
    retry / a payload missing recipients never blanks a value already captured
    (never fabricates). Also re-derives ``recipientType`` from the freshly captured
    lists so a previously-"unknown" item gets correctly classified. ZERO extra MCP
    cost — reads the SAME get_email payload the body/thread came from.
    """
    if not isinstance(target, dict):
        return
    to_list = _detail_recipients(body_payload, "to")
    cc_list = _detail_recipients(body_payload, "cc")
    if to_list:
        target["toRecipients"] = [
            {"name": str(e.get("name") or "").strip(),
             "email": str(e.get("email") or "").strip()}
            for e in to_list
        ]
    if cc_list:
        target["ccRecipients"] = [
            {"name": str(e.get("name") or "").strip(),
             "email": str(e.get("email") or "").strip()}
            for e in cc_list
        ]
    if to_list or cc_list:
        target["recipientType"] = _recipient_type({"to": to_list, "cc": cc_list})


def _lift_owa_sender(target: dict, body_payload) -> None:
    """Lift the head message's sender identity ({name,email}) onto ``target``.

    The OWA folder scan (D-058) builds its scan candidate from the cheap
    ``email_folders`` listing, which carries only a NAME list (``senders``) — no
    ``from`` dict and no address. So _flatten_from fell to its legacy ``senders[]``
    branch and HARD-CODED senderEmail "" (0/200 folder items captured a sender, the
    D-068 bug). After the body pre-fetch builds ``body_payload`` via
    _owa_conversation_to_payload, this reads the head message's ``from``/``sender``
    ({name,email}) and sets ``target['from'] = {name,email}`` (so a later
    _flatten_from takes its dict branch unchanged) AND ``target['senderEmail']``
    directly. FILL-ONLY on senderEmail: a blank/missing address never overwrites an
    address already present (anti SELF-HEAL-CLOBBERS-GOOD-DATA). NEVER fabricates: a
    payload without a head sender email is a no-op. Mirrors how the inbox/Graph path
    already populates senderEmail via from.email — the inbox path is untouched.
    """
    if not isinstance(target, dict) or not isinstance(body_payload, dict):
        return
    head = body_payload.get("email")
    if not isinstance(head, dict):
        return
    frm = head.get("from") or head.get("sender")
    if not isinstance(frm, dict):
        return
    name = str(frm.get("name", "") or "").strip()
    email = str(frm.get("email", "") or "").strip()
    if not email:
        return  # never fabricate / blank a populated address
    target["from"] = {"name": name, "email": email}
    if not str(target.get("senderEmail") or "").strip():
        target["senderEmail"] = email


def _recipient_type(raw: dict) -> str:
    """Determine whether the user is in To (only), To (shared), or CC.

    Returns: 'to-only' | 'to' | 'cc' | 'dl' | 'unknown'.

    Reads the DETAIL payload's ``to``/``cc`` keys (each a {name,email} list) — the
    ONLY place Graph exposes recipients (the inbox-list call has none, so the old
    ``toRecipients``/``ccRecipients`` raw read was always None and returned
    "unknown" for every item, D-056 #3). Also accepts an already-captured item
    carrying ``toRecipients``/``ccRecipients`` so the persisted item re-classifies
    correctly. Since the email reached my inbox I'm a recipient: match my personal
    email in To (length picks to-only vs shared) or CC; not in either == a
    distribution list ('dl'); both empty/absent == 'unknown'.
    """
    to_list = raw.get("to") or raw.get("toRecipients") or []
    cc_list = raw.get("cc") or raw.get("ccRecipients") or []

    # If neither list is populated, we can't determine
    if not to_list and not cc_list:
        return "unknown"

    def _contains_me(lst):
        """True only if MY personal email is literally in the list."""
        if not _MY_EMAIL:
            return False
        for entry in lst:
            if isinstance(entry, dict):
                email = str(entry.get("email", "") or "").lower()
            else:
                email = str(entry).lower()
            if email == _MY_EMAIL:
                return True
        return False

    in_to = _contains_me(to_list)
    in_cc = _contains_me(cc_list)

    if in_to:
        if len(to_list) == 1:
            return "to-only"
        return "to"
    if in_cc:
        return "cc"
    # I'm not explicitly in To or CC — reached me via a distribution list
    return "dl"


_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _recipient_email(entry) -> str:
    """Pull the EMAIL address from a recipient entry ({name,email} dict or string).

    Mirrors _flatten_recipient but yields the ADDRESS, not the display name, since
    the reply-all default + the email_draft create call key on addresses. Returns
    "" for anything without a usable address (never a fabricated value).
    """
    if isinstance(entry, dict):
        return str(entry.get("email") or "").strip()
    return str(entry or "").strip()


def _valid_email(addr: str) -> bool:
    """Basic shape check for a recipient address (user-data; never fabricate)."""
    return bool(_RE_EMAIL.match(str(addr or "").strip()))


def _dedupe_emails_ci(addrs) -> list[str]:
    """De-dupe a list of email addresses case-insensitively, first-seen wins.

    Drops empties/duplicates (key = lowercased) while preserving order and the
    first-seen display form. Used by the reply-all resolver and save_draft persist.
    """
    out: list[str] = []
    seen: set[str] = set()
    for a in addrs or []:
        s = str(a or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _reply_all_recipients(item: dict) -> tuple[list[str], list[str]]:
    """Resolve the reply-all To/CC default for an item (PURE, no I/O).

    To  = original sender (senderEmail) + the original ``to`` recipients, with the
          user's own address (_MY_EMAIL) removed.
    CC  = the original ``cc`` recipients with _MY_EMAIL removed, then _MY_EMAIL
          ALWAYS appended (so the user stays on their own thread's CC).
    Both are de-duped case-insensitively. When the item carries NO captured to/cc
    (older item / data absent) it degrades to To=[sender], CC=[_MY_EMAIL] — NEVER
    a fabricated address. This MUST mirror the frontend replyAllRecipients rule so a
    never-opened editor and the persisted edit resolve identically. Reads
    ``toRecipients``/``ccRecipients`` (the captured {name,email} lists) via
    _recipient_email.
    """
    me = _MY_EMAIL.lower()
    sender = str(item.get("senderEmail", "") or "").strip()

    orig_to = item.get("toRecipients") or []
    orig_cc = item.get("ccRecipients") or []

    # When _MY_EMAIL is empty (env var not set), skip me-filtering entirely —
    # return all original recipients unchanged, append nothing to CC.
    if not me:
        to_addrs = ([sender] if sender else []) + [
            a for a in (_recipient_email(e) for e in orig_to) if a
        ]
        to = _dedupe_emails_ci([a for a in to_addrs if _valid_email(a)])
        cc = _dedupe_emails_ci([
            a for a in (_recipient_email(e) for e in orig_cc) if a and _valid_email(a)
        ])
        if not to and _valid_email(sender):
            to = [sender]
        return to, cc

    # Validate + drop my own address; dedupe CI. Mirrors the frontend
    # replyAllRecipients (which validates via _dedupeByEmailCI) so a malformed
    # captured address is dropped identically on both sides.
    to_addrs = ([sender] if sender else []) + [
        a for a in (_recipient_email(e) for e in orig_to) if a and a.lower() != me
    ]
    to = _dedupe_emails_ci([a for a in to_addrs if _valid_email(a)])

    cc_addrs = [
        a for a in (_recipient_email(e) for e in orig_cc) if a and a.lower() != me
    ]
    cc_addrs.append(_MY_EMAIL)
    cc = _dedupe_emails_ci([a for a in cc_addrs if _valid_email(a)])

    # Degrade for an item with no captured to/cc: To=[sender], CC=[me].
    if not to and _valid_email(sender):
        to = [sender]
    return to, cc


def _esc(value) -> str:
    """HTML-escape any external value (None-safe → '') for the draft body.

    Every email-derived field (sender / timestamp / recipients / subject / body)
    passes through here BEFORE it reaches the HTML so a hostile thread can never
    inject live markup into the saved Outlook draft. A missing field becomes ''
    (never the literal 'None'/'undefined').
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _build_reply_all_body(reply_text: str, item: dict) -> str:
    """Build the Outlook reply-all HTML draft body (PURE — no I/O, deterministic).

    Layout: the user's ``reply_text`` on top (with the existing ``\\n`` → ``<br>``
    transform preserved), then an Outlook-style quoted block reproducing the
    ENTIRE thread — EVERY turn in ``item['threadHistory']`` in the array's existing
    oldest→newest order, never re-sorted / de-duped / summarized / skipped. Each
    quoted message carries a From/Sent/To/Subject header block above its body.

    ALL external text (sender / timestamp / recipients / subject / each turn body)
    is ``html.escape``-d FIRST, then the per-turn body's ``\\n`` → ``<br>`` is
    applied AFTER escaping (escape-first-then-inject-<br>) so a hostile turn body
    can't smuggle markup. Reads only ``threadHistory`` + ``subject`` already on the
    item — makes NO MCP / network call (the thread data is already captured). A
    turn missing a field renders that header line omitted, never a literal
    None/undefined. The send-safety boundary is untouched: this only shapes the
    DRAFT body; approve still calls email_draft create only.
    """
    reply_html = _esc(reply_text).replace("\n", "<br>")
    subject = str(item.get("subject", "") or "")

    thread_history = item.get("threadHistory")
    if not isinstance(thread_history, list):
        thread_history = []

    quoted_blocks = []
    # Iterate the array AS-IS (oldest→newest) — never re-order or skip a turn.
    for turn in thread_history:
        if not isinstance(turn, dict):
            continue
        sender = _esc(turn.get("sender"))
        timestamp = _esc(turn.get("timestamp"))
        recipients = _esc(turn.get("recipients"))
        # Escape the body FIRST, then convert its newlines to <br> (escape can't
        # touch the <br> tags this way, and the body text stays inert).
        body_html = _esc(turn.get("body")).replace("\n", "<br>")

        header_lines = []
        if sender:
            header_lines.append(f"<b>From:</b> {sender}")
        if timestamp:
            header_lines.append(f"<b>Sent:</b> {timestamp}")
        if recipients:
            header_lines.append(f"<b>To:</b> {recipients}")
        if subject:
            header_lines.append(f"<b>Subject:</b> {_esc(subject)}")
        header_html = "<br>".join(header_lines)

        quoted_blocks.append(
            f'<div style="margin-top:12px">{header_html}'
            f'<div style="margin-top:6px">{body_html}</div></div>'
        )

    if not quoted_blocks:
        return reply_html

    # Outlook separates the reply from the quoted thread with a horizontal rule,
    # and indents the quoted conversation as a blockquote.
    quoted_html = "<hr>".join(quoted_blocks)
    return (
        f"{reply_html}"
        '<br><br><hr>'
        '<blockquote style="margin:0 0 0 8px;padding-left:8px;'
        'border-left:1px solid #ccc">'
        f"{quoted_html}"
        "</blockquote>"
    )


def _flatten_from(raw: dict) -> tuple[str, str]:
    """Flatten a Graph ``from`` field into (display_name, email).

    Graph delivers from as {name, email}. The display name falls back to the email
    when name is blank (never a leaked dict repr, never an empty sender). Tolerates
    a legacy senders[]/sender/from-string shape so older callers keep working.
    """
    frm = raw.get("from")
    if isinstance(frm, dict):
        name = str(frm.get("name", "") or "").strip()
        email = str(frm.get("email", "") or "").strip()
        return (name or email, email)
    # Legacy aws-outlook shapes: senders[] array or a plain string.
    senders = raw.get("senders") or []
    if isinstance(senders, list) and senders:
        return (str(senders[0]), "")
    name = str(raw.get("sender", "") or (frm if isinstance(frm, str) else "") or "")
    return (name, str(raw.get("senderEmail", "") or ""))


def _graph_ts_ms(raw: dict) -> int:
    """Parse the Graph ``received`` (ISO8601) into epoch ms; fall back to now().

    A missing/unparseable timestamp falls back to the scan time so the row never
    shows a garbage 'When'. Tolerates legacy receivedDateTime too.
    """
    received = raw.get("received") or raw.get("receivedDateTime") or ""
    dt = _parse_ts(str(received))
    if dt is not None:
        try:
            return int(dt.timestamp() * 1000)
        except (OverflowError, OSError, ValueError):
            pass
    return int(time.time() * 1000)


def _skeleton_for(raw: dict) -> dict:
    """Build a needs-classify skeleton from a Microsoft Graph inbox message.

    Graph (manager-outlook-mcp get_emails) shape: id (the Graph message_id),
    subject, from{name,email}, received (ISO8601), is_read, has_attachments,
    preview. The item keys NATIVELY on the Graph message_id (``messageId``) and
    aliases ``conversationId`` to it (Graph exposes no conversation id, D-051) so
    the existing frontend/mute logic keeps working. ``senderEmail`` is NOW
    populated from from.email (the old aws-outlook path left it blank, AC-3).
    """
    message_id = str(raw.get("id", "") or raw.get("messageId", "")
                     or raw.get("conversationId", ""))
    sender, sender_email = _flatten_from(raw)
    subject = str(raw.get("subject", "") or raw.get("topic", ""))
    snippet = str(raw.get("preview", "") or raw.get("snippet", "") or raw.get("bodyPreview", ""))
    recipients = raw.get("recipients") or []
    return {
        "id": secrets.token_hex(4),
        "messageId": message_id,
        # Alias: Graph exposes no conversation id, so conversationId == messageId
        # keeps the frontend mute-label + the mute-key derivation correct (D-051).
        "conversationId": message_id,
        "sender": _scrub(_one_line(sender, 200)),
        "senderEmail": _scrub(_one_line(sender_email, 200)),
        "subject": _scrub(_one_line(subject, 300)),
        "snippet": _scrub(snippet[:_SNIPPET_CAP]),
        "recipients": recipients if isinstance(recipients, list) else [],
        "recipientType": _recipient_type(raw),
        "hasAttachments": bool(raw.get("has_attachments") or raw.get("hasAttachments")),
        "messageCount": raw.get("messageCount") or raw.get("unreadCount") or 1,
        "status": "needs-classify",
        "ts": _graph_ts_ms(raw),
    }


# Inline-image marker glyph (framed picture). The marker is the ONLY trace of a
# stripped <img> that survives _html_to_text, rendered downstream as plain prose.
_IMG_MARKER_GLYPH = "\U0001f5bc"  # 🖼
_RE_IMG_ALT = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_RE_IMG_SRC = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)


def _img_src_basename(src: str) -> str:
    """Derive a filename from an <img> src.

    cid:image001.gif@01D8.host -> image001.gif (drop the cid: scheme and @-suffix);
    https://cdn/assets/logo.png?v=3 -> logo.png (drop path + query). A data: URI or
    an empty src yields "" (no usable filename -> generic marker upstream).
    """
    src = (src or "").strip()
    if not src or src.lower().startswith("data:"):
        return ""
    # cid references: "cid:image001.gif@host" -> "image001.gif@host" -> "image001.gif"
    if src.lower().startswith("cid:"):
        src = src[4:]
        src = src.split("@", 1)[0]
    # Drop a query string / fragment, then take the last path segment.
    src = src.split("?", 1)[0].split("#", 1)[0]
    src = src.rstrip("/")
    base = src.rsplit("/", 1)[-1]
    return base.strip()


def _img_marker_from_tag(match: "re.Match") -> str:
    """re.sub replacement: turn one <img ...> tag into a 🖼 [image: <name>] marker.

    Name preference: alt attr, else the src basename, else a generic "🖼 [image]"
    (no filename, no ': ' separator). Pure + content-additive: it ADDS a marker
    where the catch-all tag-strip previously DELETED the tag; it never drops prose.
    The emitted text has no '<img>', so a second _html_to_text pass is a no-op.
    """
    tag = match.group(0)
    alt_m = _RE_IMG_ALT.search(tag)
    name = ""
    if alt_m:
        name = (alt_m.group(1) or alt_m.group(2) or alt_m.group(3) or "").strip()
    if not name:
        src_m = _RE_IMG_SRC.search(tag)
        if src_m:
            src = src_m.group(1) or src_m.group(2) or src_m.group(3) or ""
            name = _img_src_basename(src)
    if name:
        return f"{_IMG_MARKER_GLYPH} [image: {name}]"
    return f"{_IMG_MARKER_GLYPH} [image]"


_RE_TABLE_TAG = re.compile(r"</?table[^>]*>", re.IGNORECASE)

# D-065: capture colspan/rowspan off a <td>/<th> OPEN tag BEFORE the attributes are
# stripped. A bare/absent/invalid span defaults to 1 (HTML semantics). The value is
# the first run of digits (handles colspan=3, colspan="3", colspan='3 '); a 0 or a
# non-numeric value clamps to 1 so the grid never collapses or explodes.
_RE_CELL_OPEN = re.compile(r"<t([hd])\b([^>]*)>", re.IGNORECASE)
_RE_SPAN_ATTR = re.compile(r"\b(colspan|rowspan)\s*=\s*[\"']?\s*(\d+)", re.IGNORECASE)


def _cell_span(attrs: str, which: str) -> int:
    """Parse colspan/rowspan (``which``) from a cell tag's raw attribute string.

    Defaults to 1 for absent/zero/garbage values (HTML semantics) so a normal
    span-free cell expands to exactly itself — the strict-superset guarantee.
    """
    for name, val in _RE_SPAN_ATTR.findall(attrs):
        if name.lower() == which:
            try:
                n = int(val)
            except ValueError:
                return 1
            return n if n >= 1 else 1
    return 1


def _escape_pipes_in_cell(cell: str) -> str:
    """Escape any literal ``|`` inside ONE table cell's text as ``\\|`` (GFM rule).

    THE h3/s2 / h2/s1 LIVE-MISMATCH ROOT CAUSE (D-063 follow-up): the converter
    sizes the separator + pads every row from the cell COUNT it splits on the cell
    sentinel, so by construction header_cols == separator_cols == data_width. But it
    emitted each cell's TEXT verbatim — and a cell whose text contains a literal
    ``|`` (an Outlook status cell like ``A | B``, a path, a "key|value" pair) makes
    remark-gfm (and the live probe) RE-PARSE that one cell as TWO columns. The
    header row then re-parses WIDER than the separator (which only ever held
    ``---``), so header_cols > separator_cols and remark-gfm REFUSES the table,
    rendering raw ``| a | b |`` as a literal paragraph — the exact h3/s2 (96x),
    h2/s1 (34x), … mismatches the AC-1 probe still saw after the MAX-width fix.

    The GFM remedy is to backslash-escape every literal pipe inside the cell so it
    stays INSIDE the cell on re-parse (a column boundary is an UN-escaped ``|``).
    Applied to EVERY cell (header AND data) identically so the per-row column count
    is exactly the sentinel-split count again. Pure/deterministic; no word loss (the
    pipe character is preserved, only escaped). The escaped ``\\|`` carries no cell
    sentinel and no ``<table>``, so a second _html_to_text pass is a no-op.
    """
    return cell.replace("|", "\\|")


# D-065 span sentinels (module level so the parser/expander helpers and the
# _html_to_text cell-substitution share ONE definition).
_SPAN_OPEN = "\x04"
_SPAN_CLOSE = "\x05"
_RE_CELL_SPAN_PREFIX = re.compile(
    re.escape(_SPAN_OPEN) + r"(\d+),(\d+)" + re.escape(_SPAN_CLOSE)
)


def _parse_cell_span_prefix(piece: str) -> "tuple[str, int, int]":
    """Strip the encoded "<SPAN_OPEN>colspan,rowspan<SPAN_CLOSE>" prefix off ONE
    cell piece (the text between two cell sentinels) and return
    ``(text, colspan, rowspan)``.

    A piece with no prefix (legacy / a second idempotent pass over pipe output)
    yields (text, 1, 1) — so the expander degrades to the pre-D-065 positional
    behavior on any non-span input.
    """
    m = _RE_CELL_SPAN_PREFIX.match(piece)
    if not m:
        return piece, 1, 1
    colspan = max(1, int(m.group(1)))
    rowspan = max(1, int(m.group(2)))
    return piece[m.end():], colspan, rowspan


def _expand_row_with_spans(
    parsed_cells: "list[tuple[str, int, int]]",
    pending: "dict[int, int]",
) -> "list[str]":
    """Expand ONE source row's ``(text, colspan, rowspan)`` cells into a flat grid
    row, honoring colspan (text-in-FIRST + N-1 trailing EMPTY columns) and the
    rowspans HELD from prior rows (an EMPTY cell reserved at that exact column
    index), per the D-065 user decisions.

    ``pending`` is the per-column carry of rowspan continuations WITHIN the current
    table block (keyed by grid-column index → remaining continuation rows). It is
    mutated in place: this row CONSUMES one held continuation per occupied column,
    and REGISTERS new continuations for any cell whose rowspan > 1.

    No cell text is ever duplicated or dropped — colspan/rowspan only ADD empty
    cells (hard no-word-loss). Alignment is POSITIONAL: a held column keeps the
    same index so the row below never shifts left.
    """
    row: "list[str]" = []
    c = 0  # current grid column index
    src = list(parsed_cells)
    si = 0
    # Walk grid columns left-to-right. A column with a pending rowspan is filled
    # with an EMPTY held cell (consuming one continuation); otherwise the next
    # source cell lands there and claims colspan columns + registers its rowspan.
    while si < len(src) or any(rem > 0 for rem in pending.values()):
        held = pending.get(c, 0)
        if held > 0:
            row.append("")
            if held - 1 <= 0:
                pending.pop(c, None)
            else:
                pending[c] = held - 1
            c += 1
            continue
        if si >= len(src):
            # No more source cells AND no held continuation at THIS column — the
            # row is naturally complete (trailing held columns beyond the row's
            # own cells are not emitted; MAX-width padding reconciles ragged rows).
            break
        text, colspan, rowspan = src[si]
        si += 1
        # The cell's text goes in its FIRST column; the remaining colspan-1 grid
        # columns are EMPTY (never the duplicated text).
        for k in range(colspan):
            row.append(text if k == 0 else "")
            if rowspan > 1:
                # Reserve THIS column on the next rowspan-1 rows (positional).
                pending[c] = rowspan - 1
            c += 1
    return row


def _mark_top_level_tables(text: str, sep: str) -> str:
    """Replace only OUTERMOST <table>/</table> boundaries with the table sentinel.

    A nested <table> (depth > 1) is DROPPED so it does not split a parent logical
    row into ragged fragments (D-063 §4); its rows/cells flow on as their own GFM
    rows in the same block. Adjacent TOP-LEVEL tables still get a boundary sentinel
    each, so the per-<table> flush (D-061 §2) is unchanged. Pure/deterministic — a
    single left-to-right depth walk; an unbalanced </table> (depth already 0) is
    treated as a top-level close so a malformed body never under-counts a boundary.
    """
    out = []
    last = 0
    depth = 0
    for m in _RE_TABLE_TAG.finditer(text):
        out.append(text[last:m.start()])
        tag = m.group(0)
        is_open = not tag.lstrip("<").startswith("/")
        if is_open:
            # An outermost <table> (depth 0 → 1) marks a boundary; deeper opens drop.
            if depth == 0:
                out.append(sep)
            depth += 1
        else:
            depth = max(0, depth - 1)
            # An outermost </table> (back to depth 0) marks a boundary; deeper drop.
            if depth == 0:
                out.append(sep)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _html_to_text(html: str) -> str:
    """Strip HTML to readable plain text. Handles email HTML from Graph/OWA.

    Tables are converted to pipe-separated markdown-style rows so columnar
    data (status updates, metrics) remains scannable rather than collapsing
    into an unstructured line dump.
    """
    if not html or "<" not in html:
        return html
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Tables: convert to pipe-separated rows. Strategy: mark cell boundaries
    # with sentinel characters, strip all other tags, then replace sentinels.
    _CELL_SEP = "\x01"
    _ROW_SEP = "\x02"
    _TABLE_SEP = "\x03"
    # D-065 span markers (module-level _SPAN_OPEN/_SPAN_CLOSE): each cell sentinel is
    # IMMEDIATELY followed by an encoded "<SPAN_OPEN>colspan,rowspan<SPAN_CLOSE>"
    # prefix captured from the <td>/<th> tag's colspan/rowspan attributes BEFORE they
    # are stripped, so the downstream row builder knows how many grid columns/rows
    # each source cell occupies. The prefix is parsed off (never emitted), so the
    # converter stays idempotent — a second pass over the pipe output sees no markers.
    # Each TOP-LEVEL table (the outermost <table>/</table> element) is a sentinel-
    # delimited block so the row buffer flushes at every top-level table boundary.
    # Without this, two adjacent Outlook tables fused into ONE block (one separator,
    # the first table's header demoted to a caption) and any heading wedged between
    # them was swallowed into the prior cell. thead/tbody/tfoot/colgroup/col/caption
    # are SUB-divisions WITHIN one table, so they stay a bare '\n' (intra-table) —
    # splitting on them would tear a single thead+tbody table into two.
    #
    # NESTED-TABLE handling (D-063 §4): an Outlook nested <table> (a table inside a
    # parent <td>, e.g. a Zoom-invite block embedded in an agenda cell) used to emit
    # a _TABLE_SEP in the MIDDLE of a parent row, which forced a flush there and TORE
    # the one logical parent row into ragged half-tables (the parent cell after the
    # nested table got orphaned as a lone '| Devon |' width-1 row / a stray caption).
    # We make ONLY the OUTERMOST <table>/</table> boundaries a _TABLE_SEP (depth 0↔1)
    # and DROP inner ones, so a nested table no longer forces a mid-parent-row flush:
    # its <tr>/<td> still emit _ROW_SEP/_CELL_SEP and surface as their OWN padded GFM
    # rows in the same contiguous block (valid GFM), while the top-level flush that
    # keeps adjacent tables separate (D-061 §2) is unchanged. Deterministic: a single
    # left-to-right depth walk over the <table>/</table> tokens.
    text = _mark_top_level_tables(text, _TABLE_SEP)
    text = re.sub(r"</?(?:thead|tbody|tfoot|colgroup|col|caption)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<tr[^>]*>", _ROW_SEP, text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "", text, flags=re.IGNORECASE)
    # D-065: turn each cell OPEN tag into the cell sentinel PLUS an encoded span
    # prefix (colspan,rowspan) captured from its attributes BEFORE they are stripped
    # by the catch-all tag-strip below. Previously this discarded colspan/rowspan, so
    # a <td colspan=3> became ONE grid cell and a <td rowspan=2> left the row below
    # short — the converter could not reconcile widths or alignment (the last 9
    # malformed Outlook tables). The span counts now survive to the row builder.
    def _cell_sentinel(m: "re.Match[str]") -> str:
        attrs = m.group(2) or ""
        colspan = _cell_span(attrs, "colspan")
        rowspan = _cell_span(attrs, "rowspan")
        return f"{_CELL_SEP}{_SPAN_OPEN}{colspan},{rowspan}{_SPAN_CLOSE}"
    text = _RE_CELL_OPEN.sub(_cell_sentinel, text)
    text = re.sub(r"</t[hd]>", "", text, flags=re.IGNORECASE)
    # List items get a GFM unordered-list marker. It MUST be line-anchored ("\n- "
    # → "- " at column 0 after the whitespace collapse below) so ReactMarkdown+
    # remarkGfm renders a real <ul><li>. A leading-space "  • " bullet (the old
    # form) is NOT a list to remarkGfm — it renders as indented plain text.
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    # Block-level breaks
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "\n", text, flags=re.IGNORECASE)
    # Inline images: replace each <img ...> with a tidy plain-text marker BEFORE the
    # catch-all tag-strip below (which would otherwise DELETE the tag, losing the
    # image's identity — this is the only place the alt/src still exists). The marker
    # is PLAIN TEXT (no <img>, no cid/remote src) so the downstream tag-strip,
    # entity-decode, and table/list passes treat it as ordinary prose, and it flows
    # automatically into both _extract_email_body and every _extract_thread_history
    # turn (both call _html_to_text). Name = alt attr, else the src basename
    # (cid:image001.gif@host -> image001.gif; https://.../logo.png?v=3 -> logo.png),
    # else the generic "🖼 [image]". The emitted marker has no '<img>', so a second
    # _html_to_text pass is a no-op (idempotent).
    #
    # An HTML body that carries <img> tags surfaces a filenamed marker. BOTH sources
    # now feed HTML: the Inbox/Graph get_email path AND (since D-062) the OWA folder
    # email_read path, which fetches format="html" — so folder items now carry <img>
    # tags and get a marker here too (the prior format="markdown" body had no <img>
    # trace and could not). The marker is plain prose, so it flows uniformly into
    # _extract_email_body and every _extract_thread_history turn regardless of source.
    text = re.sub(r"<img\b[^>]*>", _img_marker_from_tag, text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')
    # Strip angle brackets around email addresses (e.g. <user@corp.example.com> → user@corp.example.com)
    # so ReactMarkdown doesn't treat them as HTML tags and silently drop following content.
    text = re.sub(r"<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>",
                  r"\1", text)
    # Collapse whitespace (preserve newlines and sentinels)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    # Convert table sentinels to GFM markdown tables. Each ROW_SEP starts a new
    # row; each CELL_SEP within a row is a column boundary. remarkGfm needs the
    # FULL GFM form to render an HTML <table>: every row wrapped in leading/
    # trailing '|', AND a '| --- | --- | ... |' separator row whose column count
    # MATCHES the table's real data columns. The separator is load-bearing —
    # pipe-wrapped rows WITHOUT it (or with a mismatched width) render as literal
    # '|' text / a malformed table in remarkGfm.
    #
    # MERGED-TITLE handling (D-061 §1): Outlook status/report tables almost always
    # LEAD with a merged single-cell TITLE row spanning the table (a colspan over
    # the N-column body). Sizing the separator from that 1-cell first row produced
    # MALFORMED GFM (1-col separator over 4-col data). Instead we BUFFER each
    # contiguous table block and lift a narrower-than-the-body FIRST row OUT as a
    # bold caption line ABOVE the table (never row 1, never dropped).
    #
    # WIDTH POLICY (D-063 §1, refines D-061): the grid width is the MAXIMUM cell
    # count across the (post-title-lift) rows, the separator is emitted at exactly
    # that width, and EVERY row — header AND data — is padded to it with empty
    # trailing cells. remark-gfm derives the column count from the HEADER + delimiter
    # and REQUIRES header_cols == separator_cols; the old MODAL width left a HEADER
    # wider than the modal mismatched (h3/s2 live) → remark-gfm rejected the table
    # and rendered raw '| a | b |' as a literal paragraph. MAX-width makes the widest
    # row define the grid so nothing exceeds the separator. No cell text is ever
    # dropped, merged, reordered, or truncated (hard no-word-loss).
    #
    # BACKWARD-COMPAT: this fires ONLY for rows that actually contain a cell
    # sentinel (a real <td>/<th>). A non-table row (ordinary prose, or a literal
    # '|' typed in text) carries no sentinel, so it passes through untouched. MAX is
    # a strict SUPERSET of MODAL: a table whose rows already share a width has
    # max == modal and gains NO padding, so a normal table gets NO caption and the
    # same separator as before — byte-identical output (the 17 clean reports stay).
    #
    # IDEMPOTENT: the emitted caption is a plain bold-markdown PROSE line (no cell
    # sentinel) and the rows are pipe rows (no <table>/sentinel), so a second
    # _html_to_text pass sees no sentinels and re-emits them unchanged.
    lines = text.split(_ROW_SEP)
    out_lines = []
    table_block = []  # buffered cell-row lists for the current contiguous block
    # D-065: per-block carry of rowspan continuations (grid-column index → remaining
    # rows). Mutated in place by _expand_row_with_spans; CLEARED whenever the block
    # resets (each _TABLE_SEP boundary / prose flush) so a rowspan never leaks across
    # a <table> boundary. A dict (not reassigned) so the expander mutates the SAME
    # object the loop holds.
    table_pending: "dict[int, int]" = {}

    def _flush_table_block():
        """Emit the buffered contiguous table block as valid GFM (caption +
        max-sized separator + every row padded to that width), then clear the buffer.

        D-063 §1: the grid width is the MAXIMUM cell count across the block's rows
        (computed AFTER the leading-title lift). remark-gfm derives the column count
        from the HEADER row + the delimiter row and REQUIRES header_cols ==
        separator_cols; the prior MODAL width emitted the separator at the modal but
        each row at its OWN width, so a HEADER (or any row) WIDER than the modal
        (h3/s2, h9/s8, h14/s13 live) left header_cols > separator_cols → remark-gfm
        rejected the table and rendered raw '| a | b |' as a literal paragraph. Sizing
        to the MAX (the widest row defines the grid) + padding EVERY row to it makes
        header_cols == separator_cols == every data-row width — zero pipe-junk. This
        is a strict SUPERSET of the modal policy: a table whose rows already share a
        width has max == modal and gains no padding, so matched tables stay
        byte-identical (D-061 §2 / the 17 clean reports preserved)."""
        if not table_block:
            return
        rows = list(table_block)
        # A leading row NARROWER than every following row is a merged/colspan TITLE
        # (a colspan over the N-column body): lift it out as a bold caption above the
        # table (never row 1 of the grid, never dropped). Compute against the MAX of
        # the REMAINING rows so the title test mirrors the final grid width.
        if len(rows) >= 2:
            body_width = max(len(r) for r in rows[1:])
            if len(rows[0]) < body_width:
                title = " ".join(rows[0]).strip()
                if title:
                    out_lines.append("**" + title + "**")
                    out_lines.append("")  # blank line so the caption reads as prose
                rows = rows[1:]
        if not rows:
            return
        # DEGENERATE single-cell block (a lone section-title <table> with no body, or
        # a one-row one-cell remnant): emit it as a bold caption + blank line, NOT a
        # 1-col GFM table. THE D-064 ADJACENCY DEFECT: a single-cell title table
        # emitted as '| Title |\n| --- |' and then immediately followed (no blank
        # line) by a WIDER grid made remark-gfm parse both as ONE 1-col table (it
        # fixes width from the header) and DROP the grid's data columns. A 1-row/1-col
        # block carries no columnar data to preserve, so lifting it to a caption (the
        # same form as the merged-title lift above) is loss-free AND the trailing
        # blank line keeps the next table its own block. Idempotent: '**...**' carries
        # no cell sentinel, so a re-run re-emits it unchanged.
        if len(rows) == 1 and len(rows[0]) == 1:
            title = rows[0][0].strip()
            if title:
                out_lines.append("**" + title + "**")
                out_lines.append("")
            return
        # MAX cell count across the (post-lift) rows — the widest row defines the
        # grid so nothing exceeds the separator. Deterministic for a given input.
        data_width = max(len(r) for r in rows)
        for i, cells in enumerate(rows):
            # PAD every row (header row 0 INCLUDED) to the grid width with empty
            # trailing cells so header/separator/data all share a column count —
            # never drop, merge, reorder, or truncate a cell (hard no-word-loss).
            padded = list(cells)
            if len(padded) < data_width:
                padded += [""] * (data_width - len(padded))
            out_lines.append("| " + " | ".join(padded) + " |")
            if i == 0:
                out_lines.append("| " + " | ".join(["---"] * data_width) + " |")
        # THE ADJACENT-BLOCK FUSION DEFECT (D-063 AC-1 live tail): when this full
        # table is IMMEDIATELY followed by ANOTHER table block (two stacked Outlook
        # tables of DIFFERENT widths, or a width-1 flowchart connector table after a
        # 2-col header) with NO blank line between them, remark-gfm reads the next
        # block's rows as MORE data rows of THIS table — fixing the column count from
        # THIS header — so a width-3 (or width-1) follow-on row mismatches this width-2
        # separator and remark-gfm refuses the fused table, rendering raw pipe text.
        # That is exactly the 'data-row width 3 != separator 2' (Hydra-Kirin ragged
        # nested layout) and 'data-row width 1 != separator 2' (MAP Automation ▼/EPR
        # connector) live mismatches. A GFM table ENDS at a blank line, so we terminate
        # the block with one trailing blank line — the SAME guard the caption /
        # degenerate-single-cell paths already use. The trailing-blank run is collapsed
        # by the later `\n{3,}` squeeze, so a table with prose after it is unchanged
        # byte-for-byte (a single blank line was already there); only the
        # table-immediately-after-table case gains the separating blank line. Idempotent
        # (a blank line carries no sentinel) and loss-free (adds whitespace only).
        out_lines.append("")

    for raw_line in lines:
        # A logical <table>/</table> boundary (the _TABLE_SEP sentinel) ends the
        # current table block: split the row on it so two adjacent Outlook tables —
        # and any heading wedged between them — never fuse into one block. Each
        # segment is processed independently; the boundary itself forces a flush so
        # the next table starts its OWN header + separator.
        segments = raw_line.split(_TABLE_SEP)
        for seg_idx, line in enumerate(segments):
            if seg_idx > 0:
                # Crossed a <table>/</table> boundary — end the prior table here.
                _flush_table_block()
                table_block = []
                table_pending.clear()  # rowspans never leak across a <table> boundary
            if _CELL_SEP in line:
                # Any text BEFORE the first cell sentinel is prose (a real cell always
                # opens with _CELL_SEP), so flush any open block, emit the prose on its
                # own line, and start fresh — it must not be folded into the first cell.
                prefix, _, rest = line.partition(_CELL_SEP)
                prefix = prefix.strip()
                if prefix:
                    _flush_table_block()
                    table_block = []
                    table_pending.clear()
                    out_lines.append(prefix)
                # Split into cells POSITIONALLY. The old code dropped every empty cell
                # (`[c for c in cells if c]`), which shifted later cells LEFT and broke
                # column alignment (the live 3-col-header-over-1-col-separator and
                # 14-vs-13 mismatch). We KEEP empty cells in place so cell N stays in
                # column N; a row whose cells are ALL empty (an Outlook spacer/layout
                # row) is the ONLY case dropped — it is not data and must not widen the
                # grid or emit a blank pipe row.
                # Escape any literal '|' INSIDE a cell's text (_escape_pipes_in_cell)
                # so remark-gfm re-parses the cell as ONE column. An un-escaped pipe
                # in cell text was the live h3/s2 / h2/s1 mismatch: it inflated the
                # header's re-parsed column count past the separator width, so
                # remark-gfm refused the table and rendered raw pipe text.
                #
                # D-065: each piece begins with the encoded "<SPAN_OPEN>colspan,
                # rowspan<SPAN_CLOSE>" prefix captured from the <td>/<th> tag. Parse
                # it off, then EXPAND: colspan=N → text-in-FIRST + N-1 trailing EMPTY
                # cells; a rowspan held from a prior row reserves its column with an
                # EMPTY cell (positional). The span counts (not the raw text) drive
                # the grid width so a <td colspan=3> occupies 3 columns and a
                # <td rowspan=2> keeps the row below aligned — the last 9 malformed
                # Outlook tables. A span-free cell has (.,1,1) and expands to itself,
                # so a normal table is byte-identical (strict superset).
                parsed_cells = []
                for piece in rest.split(_CELL_SEP):
                    cell_text, colspan, rowspan = _parse_cell_span_prefix(piece)
                    cell_text = _escape_pipes_in_cell(cell_text.replace("\n", " ").strip())
                    parsed_cells.append((cell_text, colspan, rowspan))
                if not any(text for text, _cs, _rs in parsed_cells) and not any(
                    rem > 0 for rem in table_pending.values()
                ):
                    # An all-empty Outlook SPACER/layout row (every cell blank text)
                    # with NO rowspan continuation pending. It is NOT data and must not
                    # emit a pipe row or widen the grid — but it must ALSO NOT flush /
                    # break the contiguous table block (the D-064 SPLIT-BLOCK DEFECT:
                    # flushing here glued a 1-col section-title header to a wider grid,
                    # so remark-gfm fixed the width from the 1-col header and DROPPED
                    # the data columns). We DROP the spacer and KEEP buffering so the
                    # title + grid stay ONE MAX-width block. A spacer carries no
                    # surviving cell text, so dropping it loses no word. (When a
                    # rowspan IS pending, an empty source row is a real continuation
                    # row — it must still be expanded so the held column advances.)
                    continue
                cells = _expand_row_with_spans(parsed_cells, table_pending)
                if not any(cells):
                    # Span expansion produced an all-empty grid row (e.g. a stray
                    # empty continuation with nothing held): nothing to render, but the
                    # pending map was already advanced inside the expander. Drop it.
                    continue
                table_block.append(cells)
            else:
                # Plain prose (or a heading lifted out from between two tables). Flush
                # the open block and emit the text on its own line. An empty segment
                # (a bare table boundary with nothing else) just flushes — it adds no
                # blank line of its own.
                _flush_table_block()
                table_block = []
                table_pending.clear()
                if line.strip():
                    out_lines.append(line)
    _flush_table_block()
    table_block = []
    table_pending.clear()
    text = "\n".join(out_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _msg_sender_name(msg: dict) -> str:
    """Flatten a message's sender/from into a display name (never a dict repr)."""
    sender = msg.get("from")
    if sender is None:
        sender = msg.get("sender") or ""
    if isinstance(sender, dict):
        return str(sender.get("name") or sender.get("email") or "")
    return str(sender or "")


def _flatten_recipient(entry) -> str:
    """Flatten ONE recipient entry into a display string (name else email).

    Mirrors _msg_sender_name/_flatten_from: a {name,email} dict yields the name
    (falling back to the email), a plain string passes through, anything else
    yields "" — NEVER a leaked dict repr / '[object Object]'.
    """
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("email") or "").strip()
    return str(entry or "").strip()


def _msg_recipients_str(msg: dict) -> str:
    """Build a human-readable 'To:' string for one message turn.

    Reads the message's toRecipients/recipients/to field, flattens each entry via
    _flatten_recipient (name else email, never a dict repr), drops empties, and
    joins with ', '. Returns "" when the message carries no recipient info so the
    turn never fabricates a recipients line.
    """
    if not isinstance(msg, dict):
        return ""
    raw = msg.get("toRecipients")
    if not isinstance(raw, list) or not raw:
        raw = msg.get("recipients")
    if not isinstance(raw, list) or not raw:
        raw = msg.get("to")
    if isinstance(raw, str):
        return _one_line(raw, 200)
    if not isinstance(raw, list):
        return ""
    names = [n for n in (_flatten_recipient(e) for e in raw) if n]
    return _one_line(", ".join(names), 200)


def _resolve_scan_subfolders(list_folders_payload) -> list[tuple[str, str]]:
    """Resolve EMAIL_SCAN_SUBFOLDERS display names -> current folder ids (D-058).

    PURE/testable: takes an ``email_list_folders`` payload
    ``{content:{folders:[{name,id,unreadCount,children:[...]}]}}``, finds the
    top-level folder named "Inbox", walks its ``children``, and matches each child's
    ``name`` against the allowlist. Returns ``[(display_name, folder_id), ...]`` for
    allowlisted children that have ``unreadCount > 0`` (the cheap optimization --
    skip folders with no unread to ingest this tick).

    FAIL-LOUD-BUT-CONTINUE (AC-11): an allowlist name with NO matching Inbox child is
    SKIPPED with a logger.info -- NEVER resolved to a fabricated id. A missing/
    malformed payload (no Inbox, wrong shape) degrades to [] without raising.
    """
    if not isinstance(list_folders_payload, dict):
        return []
    content = list_folders_payload.get("content")
    folders = content.get("folders") if isinstance(content, dict) else None
    if not isinstance(folders, list):
        folders = list_folders_payload.get("folders")
    if not isinstance(folders, list):
        return []

    inbox = None
    for f in folders:
        if isinstance(f, dict) and str(f.get("name", "")).strip().lower() == "inbox":
            inbox = f
            break
    if inbox is None:
        return []

    children = inbox.get("children")
    if not isinstance(children, list):
        children = []
    # Index the Inbox children by display name (current ids).
    by_name = {
        str(c.get("name", "")): c
        for c in children
        if isinstance(c, dict) and c.get("name")
    }

    resolved: list[tuple[str, str]] = []
    for name in EMAIL_SCAN_SUBFOLDERS:
        child = by_name.get(name)
        if child is None:
            # Fail-loud-but-continue: surface the miss, never fabricate an id.
            logger.info(
                "Email scan: allowlisted subfolder %r not found under Inbox "
                "(skipping this folder this cycle).", name,
            )
            continue
        fid = str(child.get("id", "") or "")
        if not fid:
            logger.info(
                "Email scan: allowlisted subfolder %r resolved to an empty id "
                "(skipping).", name,
            )
            continue
        # Cheap optimization: nothing unread to ingest this tick.
        unread = child.get("unreadCount")
        if isinstance(unread, (int, float)) and not isinstance(unread, bool) and unread <= 0:
            continue
        resolved.append((name, fid))
    return resolved


def _owa_conversation_to_payload(owa_emails) -> dict:
    """Normalize an OWA ``email_read`` message list onto the canonical body shape.

    The aws-outlook-mcp ``email_read`` response (fetched format="html" since D-062 so
    the body carries real <table>/<img>/<b> markup for _html_to_text) is
    ``{content:{emails:[<message>]}}`` where each <message> uses DIFFERENT field
    names than the Graph ``get_email`` path: message id is ``itemId`` (NOT id/
    messageId), To recipients are ``recipients`` (NOT toRecipients), CC is
    ``ccRecipients``, sender is ``sender``/``from``, received ts is
    ``dateTimeSent``/``recievedAt`` (sic -- misspelled in the API), read flag is
    ``isRead``. The ``body`` is passed onto messages[].body UNCHANGED (no pre-strip);
    _extract_email_body / _extract_thread_history run _html_to_text on it EXACTLY once.

    We REMAP onto the {email:{..., messages:[...]}} shape the EXISTING helpers
    already consume (_email_messages_from_payload / _extract_email_body /
    _extract_thread_history / _detail_recipients / _recipient_type) so we do NOT
    fork a parallel item builder. The conversation's FULL message list IS the thread
    history (AC-4 -- N per-turn cards, never a summary). Crucially we map
    ``recipients`` -> ``to`` and ``ccRecipients`` -> ``cc`` so _capture_recipients/
    _recipient_type (which read ``to``/``cc``) classify correctly (AC-12 -- avoids
    all-"Other" grouping). Returns {} for an empty/wrong-shaped input (never
    fabricates).
    """
    if not isinstance(owa_emails, list):
        return {}
    messages: list[dict] = []
    for m in owa_emails:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("itemId", "") or m.get("id", "") or m.get("messageId", ""))
        recipients = m.get("recipients")
        cc = m.get("ccRecipients")
        received = m.get("recievedAt") or m.get("dateTimeSent") or m.get("received") or ""
        messages.append({
            "id": mid,
            "messageId": mid,
            "from": m.get("from") or m.get("sender"),
            "sender": m.get("sender") or m.get("from"),
            # Map onto to/cc (the keys _detail_recipients / _recipient_type read).
            "to": recipients if isinstance(recipients, list) else [],
            "cc": cc if isinstance(cc, list) else [],
            "subject": m.get("subject", ""),
            "body": m.get("body", ""),
            "received": received,
            "isRead": m.get("isRead"),
        })
    if not messages:
        return {}
    # The latest message (newest-first by convention) seeds the top-level
    # email.to/cc so _detail_recipients/_capture_recipients see the recipients of
    # the message addressed to me; the full list rides under messages[].
    head = dict(messages[0])
    head["messages"] = messages
    return {"email": head}


def _email_messages_from_payload(payload) -> list[dict]:
    """Normalize an MCP body response into a list of message dicts.

    Supports BOTH the migrated Graph shape (get_email -> {email:{...}} with an
    optional messages[] thread array, else the single email IS the message) AND
    the legacy aws-outlook shape ({content:{emails:[...]}}). Returns [] when no
    message dict is present so callers never fabricate content.
    """
    if not isinstance(payload, dict):
        return []
    # Graph: {email: {... , messages?: [...]}}
    email = payload.get("email")
    if isinstance(email, dict):
        msgs = email.get("messages") or email.get("emails")
        if isinstance(msgs, list) and msgs:
            return [m for m in msgs if isinstance(m, dict)]
        return [email]
    # Legacy aws-outlook: {content: {emails|messages: [...]}}
    content = payload.get("content")
    if isinstance(content, dict):
        emails = content.get("emails") or content.get("messages")
        if isinstance(emails, list) and emails:
            return [m for m in emails if isinstance(m, dict)]
    return []


def _extract_email_body(payload) -> str:
    """Extract the email body text from a body MCP response.

    Handles the Graph {email:{...}} shape AND the legacy {content:{emails:[...]}}
    shape (via _email_messages_from_payload). We concatenate all message bodies
    into a single string, flattening from{name,email} so the header reads
    "From: Jane Doe", NEVER a leaked Python dict repr.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    msgs = _email_messages_from_payload(payload)
    if msgs:
        parts = []
        for msg in msgs:
            body = _html_to_text(str(msg.get("body", "") or ""))
            sender = _msg_sender_name(msg)
            subject = msg.get("subject", "")
            if body:
                header = f"From: {sender}\nSubject: {subject}\n\n" if sender else ""
                parts.append(header + body)
        if parts:
            return "\n---\n".join(parts)
    # Fallback: top-level body or content as string
    body = payload.get("body", "") or payload.get("content", "")
    return _html_to_text(str(body)) if body else ""


# Quoted-reply header boundaries. These match the multi-line header SHAPE, never a
# single stray keyword in prose (AC-9): an Outlook ``From:`` line is only a
# boundary if a ``Sent:``/``Date:`` AND a ``Subject:`` line follow within a few
# lines (confirmed in _split_quoted_thread); the Gmail ``On ... wrote:`` and the
# ``-----Original <word>-----`` separator stand alone.
#
# Since D-062 the OWA ``email_read`` path fetches ``format="html"``, so the folder
# body arrives as raw ``<b>From:</b>``/``<strong>Sent:</strong>`` markup that
# _html_to_text's tag-strip reduces to BARE labels (``From:``) — exactly like the
# Inbox/Graph HTML path. So the SHAPE-based boundary regexes match on bare labels on
# both paths. The D-060 emphasis tolerance below (legacy ``format=markdown`` bolded
# the labels: ``**From:``, ``**From:** Shadeck, Gal``, ``**Date: **...``, ``__Sent:__
# ...``, ``*Subject:* ...``) is now a harmless back-compat STRICT-SUPERSET, no longer
# the folder path's primary boundary. We tolerate an OPTIONAL leading emphasis run
# (``**``/``__``/single ``*``/``_``) plus whitespace before the label, and emphasis
# between the label's colon and the value — when NO emphasis is present the value
# capture is byte-identical to the pre-D-060 pattern (D-053 backward compat). The
# captured VALUE may still carry trailing emphasis (e.g. ``Shadeck, Gal **``), which
# is stripped by ``_strip_emphasis`` BEFORE the value is flattened into a field.
_EMPH = r"(?:\*{1,2}|_{1,2})"  # one markdown emphasis run: ** __ * _
# Optional leading emphasis + ws, label + ':', optional emphasis/ws, then value.
# The value group is OPTIONAL ((.*)) so a real OWA body that wraps the value onto
# the FOLLOWING line (``**From:`` / ``**From: `` alone, value ``**"Shadeck, Gal"``
# below) still matches the label; the on-next-line value is recovered by
# ``_header_value`` (D-060 rework). The bare/inline form is unchanged: a non-empty
# value is still captured in group(1) byte-identically.
_RE_FROM_LINE = re.compile(rf"^\s*{_EMPH}?\s*From:\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
_RE_DATE_LINE = re.compile(rf"^\s*{_EMPH}?\s*(?:Date|Sent):\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
_RE_TO_LINE = re.compile(rf"^\s*{_EMPH}?\s*To:\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
_RE_CC_LINE = re.compile(rf"^\s*{_EMPH}?\s*Cc:\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
_RE_SUBJECT_LINE = re.compile(rf"^\s*{_EMPH}?\s*Subject:\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
# A meeting-invite ``**When:`` (date/time) and ``**Where:`` (location) line.
# ``When:`` is a date CONFIRMATION for the shape gate and a FALLBACK timestamp (used
# only when no Date:/Sent: line is present); ``Where:`` is recognized solely so the
# parser drops it from the body (D-060 rework: real Original-Appointment blocks).
_RE_WHEN_LINE = re.compile(rf"^\s*{_EMPH}?\s*When:\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
_RE_WHERE_LINE = re.compile(rf"^\s*{_EMPH}?\s*Where:\s*{_EMPH}?\s*(.*?)\s*$", re.IGNORECASE)
_RE_GMAIL_WROTE = re.compile(r"^\s*On\s+.+\bwrote:\s*$", re.IGNORECASE)
# Full-line dashed separator: ``-----Original Message-----`` AND
# ``-----Original Appointment-----`` (any single-word variant). Stays dash-anchored
# (a ``\w+`` word, never free prose) so a sentence mentioning "the original
# message" never false-splits (D-060, AC-7).
_RE_ORIGINAL_SEP = re.compile(r"^\s*-{2,}\s*Original\s+\w+\s*-{2,}\s*$", re.IGNORECASE)
# Leading/trailing markdown emphasis runs to peel off a captured header VALUE.
_RE_EMPH_EDGES = re.compile(r"^(?:\*{1,2}|_{1,2})\s*|\s*(?:\*{1,2}|_{1,2})$")


def _strip_emphasis(value: str) -> str:
    """Peel leading/trailing markdown emphasis runs (``**``/``__``/``*``/``_``) and
    surrounding whitespace off a captured header value.

    A bolded label line like ``**From:** Shadeck, Gal **`` yields the raw value
    ``Shadeck, Gal **``; this returns ``Shadeck, Gal`` so the flattened sender is
    clean, never carrying emphasis noise (D-060 AC-3). A value with NO emphasis is
    returned unchanged (strict superset / backward compat). Idempotent: re-applying
    on the cleaned value is a no-op.
    """
    v = (value or "").strip()
    prev = None
    # Peel one run at a time from each edge until stable (handles ``** value **``).
    while v != prev:
        prev = v
        v = _RE_EMPH_EDGES.sub("", v).strip()
    return v
# Max NON-BLANK lines a single quoted Outlook header block may span (From: through
# Subject:, blank lines NOT counted). Real OWA blocks interleave blank lines between
# every label AND wrap long To:/Cc: recipient lists across MANY continuation lines —
# the largest genuine block observed in the stored corpus is 27 non-blank lines (a
# big Cc list). 40 comfortably clears that while still bounding a pathological scan
# (a lone ``**From:`` in prose can't wander hundreds of lines hunting an unrelated
# Subject:). The real over-match guard is the SHAPE gate (From: + date + Subject: in
# order, terminating at Subject:); this cap is the belt-and-suspenders (D-060 rework).
_OUTLOOK_HEADER_MAX_NONBLANK = 40
# A 'Name <email>' / 'Name email@x' header value -> display name (drop the addr).
_RE_ADDR_ANGLE = re.compile(r"\s*<[^>]+>\s*$")
_RE_ADDR_BARE = re.compile(r"\s+\S+@\S+\s*$")
# Split a header recipient line between entries. Outlook joins recipients with
# ';' and names are 'Last, First' (commas INSIDE a name), so prefer ';'; only fall
# back to ',' when no ';' is present (a Gmail-style comma-joined list).
_RE_RECIP_SPLIT_SEMI = re.compile(r"\s*;\s*")
_RE_RECIP_SPLIT_COMMA = re.compile(r"\s*,\s*")


def _flatten_header_name(value: str) -> str:
    """Flatten ONE header 'Name <email>' / 'Name email@x' value to a display name.

    Drops a trailing ``<addr>`` or bare ``addr@host`` and any surrounding double
    quotes (OWA wraps display names as ``"Last, First"``); if only an email is
    present the email is kept (better than ""). Mirrors the name-else-email intent
    of _flatten_recipient for parsed-header text.
    """
    v = (value or "").strip().strip('"').strip()
    if not v:
        return ""
    stripped = _RE_ADDR_ANGLE.sub("", v).strip()
    if not stripped:
        # Was just '<addr>' — unwrap the angle brackets.
        return v.strip("<> ").strip()
    bare = _RE_ADDR_BARE.sub("", stripped).strip()
    return (bare or stripped).strip().strip('"').strip()


# A double-quoted display name (``"Last, First"``), used to split a recipient list
# whose names carry internal commas. The quotes are the real delimiter, so when any
# are present we extract the quoted spans rather than naively splitting on ','/';'.
_RE_QUOTED_NAME = re.compile(r'"([^"]*)"')


def _flatten_header_recipients(value: str) -> str:
    """Flatten a 'To:'/'Cc:' header value (a list of recipients) to display names.

    OWA renders each recipient as a double-quoted ``"Last, First"`` separated by
    ``,`` or ``;`` — so a naive comma split would shatter ``"Rui, Ricardo"`` into two
    bogus names. When quoted spans are present we extract THOSE as the names;
    otherwise (a bare ``Last, First; Last, First`` list) we prefer ``;`` and only
    fall back to ``,`` when no ``;`` is present (D-060 rework).
    """
    v = (value or "").strip()
    if not v:
        return ""
    quoted = _RE_QUOTED_NAME.findall(v)
    if quoted:
        parts = quoted
    else:
        splitter = _RE_RECIP_SPLIT_SEMI if ";" in v else _RE_RECIP_SPLIT_COMMA
        parts = splitter.split(v)
    names = [n for n in (_flatten_header_name(p) for p in parts) if n]
    return "; ".join(names)


# Pull an email address out of ONE recipient entry: an angle-bracketed ``<addr>`` or
# a bare ``addr@host`` token. Reuses the same address SHAPE as the angle-bracket
# strip in _html_to_text (~2359); the captured value is still gated through
# _valid_email so only a well-formed address is ever stored (NEVER fabricated).
_RE_ENTRY_ANGLE_ADDR = re.compile(r"<\s*([^<>@\s]+@[^<>@\s]+)\s*>")
_RE_ENTRY_BARE_ADDR = re.compile(r"([^\s<>;,]+@[^\s<>;,]+)")


# One ``Name <addr@host>`` recipient entry — a (lazy) prefix up to and including the
# angle-bracketed address. Used to split an unquoted ``Last, First <a>; ... `` /
# ``Last, First <a> Last2, First2 <b>`` list on the ADDRESS boundary so the inner
# ``Last, First`` comma is never mistaken for a delimiter (D-068).
_RE_NAME_ANGLE_ENTRY = re.compile(r".*?<\s*[^<>@\s]+@[^<>@\s]+\s*>")
# Same idea for a BARE address (no angle brackets): _html_to_text strips the
# ``<addr>`` wrapper (~2359) so the folder body header reads ``Last, First addr@host``.
# A lazy prefix up to and including a bare ``addr@host`` token closes one entry, so
# ``Carol, Lee carol@host`` is ONE entry (name "Carol, Lee"), not shattered on the
# inner comma. The trailing ``[^\s;,]*`` swallows any glued trailing punctuation.
_RE_NAME_BARE_ENTRY = re.compile(r"[^@]*?[^\s<>;,]+@[^\s<>;,]+")


def _split_header_entries(value: str) -> list[str]:
    """Split a 'To:'/'Cc:' header VALUE into per-recipient entry strings.

    Each entry keeps its ``Name <addr>`` shape intact (the address-extracting
    counterpart needs the addr, unlike _flatten_header_recipients which only wants
    the name). Splitting precedence (Outlook ``Last, First`` names carry INNER
    commas, so a naive comma split shatters them):

    1. ``;`` present  -> split on ``;`` (Outlook's canonical recipient delimiter).
    2. no ``;`` but angle-bracketed addresses present -> split on the ADDRESS
       boundary (each ``<addr>`` closes one entry), so an unquoted
       ``Last, First <a@x> Other, Name <b@x>`` is two entries, not shattered on the
       name commas.
    3. quoted ``"Last, First"`` span present -> keep as ONE entry (the comma is a
       name comma; we never know the bare-list delimiter safely).
    4. otherwise -> fall back to a comma split (a Gmail-style ``a@x, b@x`` list).

    Empty/whitespace entries are dropped.
    """
    v = (value or "").strip()
    if not v:
        return []
    if ";" in v:
        parts = _RE_RECIP_SPLIT_SEMI.split(v)
    elif _RE_ENTRY_ANGLE_ADDR.search(v):
        # Unquoted angle-bracket address list: split on the address boundary so an
        # inner ``Last, First`` comma is never a delimiter. Trailing text after the
        # last address (rare) is appended as its own entry.
        parts = _RE_NAME_ANGLE_ENTRY.findall(v)
        consumed = "".join(parts)
        tail = v[len(consumed):].strip(" ,;")
        if tail:
            parts.append(tail)
    elif _RE_ENTRY_BARE_ADDR.search(v):
        # Bare-address list (the folder path: _html_to_text strips ``<addr>`` to a
        # bare ``addr@host``). Split on the bare-address boundary so ``Carol, Lee
        # carol@host`` stays ONE entry, not shattered on the inner comma.
        parts = _RE_NAME_BARE_ENTRY.findall(v)
        consumed = "".join(parts)
        tail = v[len(consumed):].strip(" ,;")
        if tail:
            parts.append(tail)
    elif _RE_QUOTED_NAME.search(v):
        # A quoted "Last, First" with no ';' — the comma is inside the name, so do
        # NOT comma-split (would shatter the name); treat the whole value as one entry.
        parts = [v]
    else:
        parts = _RE_RECIP_SPLIT_COMMA.split(v)
    return [p.strip(" ,;") for p in parts if p.strip(" ,;")]


def _entry_address(entry: str) -> str:
    """Extract a VALID email address from one ``Name <addr>`` / bare ``addr`` entry.

    Prefers an angle-bracketed ``<addr>``; falls back to a bare ``addr@host`` token.
    Returns "" when no well-formed address is present (gated through _valid_email so
    a malformed token is dropped, never stored). PURE — the address-extracting
    counterpart to _flatten_header_name (which DROPS the address to keep the name).
    """
    s = (entry or "").strip()
    if not s:
        return ""
    m = _RE_ENTRY_ANGLE_ADDR.search(s)
    cand = m.group(1).strip() if m else ""
    if not cand:
        # No angle brackets — strip any quoted name span first so a bare list like
        # '"Last, First" addr@host' doesn't match inside the quotes, then look for a
        # bare address token.
        unquoted = _RE_QUOTED_NAME.sub(" ", s)
        bm = _RE_ENTRY_BARE_ADDR.search(unquoted)
        cand = bm.group(1).strip() if bm else ""
    return cand if _valid_email(cand) else ""


def _extract_header_addrs(value: str) -> list[tuple[str, str]]:
    """Extract (display_name, email) pairs from a captured To:/Cc: header VALUE.

    The address-extracting counterpart to _flatten_header_name: where that helper
    DROPS the trailing ``<addr>`` to keep only the name, this keeps BOTH. Splits the
    value into per-recipient entries (``_split_header_entries`` — quoted-name / ';'
    preferred over ','), then for each entry pairs the flattened display name with the
    extracted, _valid_email-gated address. An entry with no parseable address yields
    NO pair (never a fabricated address). Used to build the body name->addr map that
    fills the name-only OWA recipients (D-068). Returns [] for an empty/None value.
    """
    pairs: list[tuple[str, str]] = []
    for entry in _split_header_entries(value):
        addr = _entry_address(entry)
        if not addr:
            continue  # no parseable address — never fabricate one
        name = _flatten_header_name(entry)
        pairs.append((name, addr))
    return pairs


def _normalize_recipient_name(name: str) -> str:
    """Normalize a recipient display name for CASE-INSENSITIVE matching.

    Lowercases, strips, collapses internal whitespace and drops surrounding double
    quotes so an OWA ``"Last, First"`` matches a body header's ``Last, First`` form.
    Returns "" for an empty/None name.
    """
    n = str(name or "").strip().strip('"').strip()
    return re.sub(r"\s+", " ", n).lower()


def _recipient_addr_map_from_body(body: str) -> dict[str, str]:
    """Build a {normalized_name: email} map from a quoted-body's header lines.

    Walks the body with the EXISTING quoted-header machinery — for every Outlook
    header block (`_parse_outlook_header_block`) found anywhere in the body it reads
    the raw (still-address-bearing) ``to``/``cc`` values and pairs each name with its
    address via `_extract_header_addrs`. Builds the map across ALL turns. FIRST-SEEN
    wins (so an earlier turn's address is not silently overwritten by a later turn).
    A name with no parseable address is simply absent (never mapped to "").
    """
    out: dict[str, str] = {}
    if not body:
        return out
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        block = _parse_outlook_header_block(lines, i)
        if block is None:
            i += 1
            continue
        fields, body_start = block
        for key in ("to", "cc"):
            raw = _strip_emphasis(fields.get(key, ""))
            for name, addr in _extract_header_addrs(raw):
                norm = _normalize_recipient_name(name)
                if norm and norm not in out:
                    out[norm] = addr
        i = max(body_start, i + 1)
    return out


def _recipient_emails_snapshot(item: dict) -> dict[str, str]:
    """Snapshot the CURRENT {normalized_name: email} of an item's captured recipients.

    Used to PRESERVE prior-captured addresses across a backfill re-fetch: the OWA
    re-fetch's structured recipients[] are NAME-ONLY, so _capture_recipients would
    overwrite a previously-recovered email with "" (the SELF-HEAL-CLOBBERS-GOOD-DATA
    trap). Snapshotting before _capture_recipients and re-filling after restores them.
    Only non-empty emails are captured.
    """
    snap: dict[str, str] = {}
    if not isinstance(item, dict):
        return snap
    for key in ("toRecipients", "ccRecipients"):
        for entry in item.get(key) or []:
            if not isinstance(entry, dict):
                continue
            email = str(entry.get("email") or "").strip()
            norm = _normalize_recipient_name(entry.get("name", ""))
            if email and norm and norm not in snap:
                snap[norm] = email
    return snap


def _fill_recipient_emails_from_body(item: dict, body: str,
                                     prior: "dict[str, str] | None" = None) -> None:
    """Fill the EMPTY ``email`` of name-only toRecipients/ccRecipients.

    The OWA structured recipients[] are NAME-ONLY (a genuine OWA limitation); the real
    addresses live in the quoted-body header lines. This builds a name->addr map from
    the body (`_recipient_addr_map_from_body`) — preferred — and falls back to the
    optional ``prior`` map (a pre-_capture_recipients snapshot, so an address already
    recovered on an earlier scan is restored after a name-only re-fetch overwrote the
    list). Fills each recipient entry whose email is currently empty by a
    CASE-INSENSITIVE name match (tolerant of Outlook ``"Last, First"`` forms).
    FILL-ONLY: an already-populated email is NEVER overwritten/cleared (anti
    SELF-HEAL-CLOBBERS-GOOD-DATA). NEVER fabricates: a name with no parseable/matchable
    address stays email:'' (no guess, no cross-assignment).
    """
    if not isinstance(item, dict):
        return
    name_to_addr = _recipient_addr_map_from_body(body) if body else {}
    if prior:
        # Body wins; prior fills only the names the body didn't recover.
        for norm, addr in prior.items():
            name_to_addr.setdefault(norm, addr)
    if not name_to_addr:
        return
    for key in ("toRecipients", "ccRecipients"):
        entries = item.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("email") or "").strip():
                continue  # fill-only — never overwrite a populated address
            norm = _normalize_recipient_name(entry.get("name", ""))
            addr = name_to_addr.get(norm)
            if addr:
                entry["email"] = addr


# label -> regex ; built once, used by the block walker. Each regex anchors a
# DISTINCT label word so the iteration order does not matter (no shadowing).
_OUTLOOK_HEADER_LABELS = (
    ("from", _RE_FROM_LINE),
    ("date", _RE_DATE_LINE),
    ("when", _RE_WHEN_LINE),
    ("to", _RE_TO_LINE),
    ("cc", _RE_CC_LINE),
    ("subject", _RE_SUBJECT_LINE),
    ("where", _RE_WHERE_LINE),
)
# Labels that carry a single (possibly wrapped-onto-next-line) value.
_SINGLE_VALUE_LABELS = ("from", "date", "when", "subject", "where")


def _match_header_label(line: str):
    """If ``line`` is a quoted-header LABEL line, return (label, inline_value); else
    None. ``inline_value`` is the (possibly empty) value captured after the colon —
    empty means the real value wraps onto the following line(s) (D-060 rework).
    """
    for label, rx in _OUTLOOK_HEADER_LABELS:
        m = rx.match(line)
        if m:
            return label, m.group(1)
    return None


def _parse_outlook_header_block(lines: list[str], start: int):
    """Walk a quoted Outlook header block beginning at the ``From:`` line ``start``.

    Since D-062 the OWA path fetches ``format="html"``, so the folder body's header
    labels arrive BARE (``<b>From:</b>`` -> ``From:`` after _html_to_text), like the
    Inbox/Graph path. Legacy/markdown bodies could additionally (1) bold the labels
    (``**From:``), (2) wrap a value onto the FOLLOWING line(s) when the label line has
    no inline value, (3) interleave a blank line between every label, and (4) wrap a
    long ``To:``/``Cc:`` list across continuation lines. This walker tolerates all of
    them: it skips blank lines inside the block, attaches a label's value from the
    next non-blank line(s) when the inline value is empty, and folds recipient
    continuation lines into the preceding ``To:``/``Cc:`` value.

    Returns ``(fields, body_start)`` where ``fields`` is
    ``{from,date,when,to,cc,subject,where}`` (raw captured values, emphasis NOT yet
    stripped) and ``body_start`` is the index of the first body line AFTER the
    block — but ONLY when the block has the required multi-line SHAPE (``From:`` + a
    date label [Date/Sent/When] + ``Subject:``). Returns ``None`` when the shape is
    absent (a lone ``**From:`` in prose, the top FW wrapper with no date, etc.) so
    the caller never false-splits.

    Header model: in a real OWA quoted block the labels appear in order and
    ``Subject:`` (sometimes followed by a meeting invite's ``Where:``) is LAST. So
    BEFORE the subject is seen, any non-label non-blank line is a wrapped value
    continuation of the current label (date/recipient lists routinely wrap onto two
    or three lines); the FIRST non-label line AFTER the subject (+ optional Where)
    is the body. Bounded to _OUTLOOK_HEADER_MAX_NONBLANK non-blank lines as the
    over-match guard.
    """
    if start >= len(lines) or not _RE_FROM_LINE.match(lines[start]):
        return None
    fields = {"from": "", "date": "", "when": "", "to": "", "cc": "",
              "subject": "", "where": ""}
    cur_label = None  # the label whose value the next continuation line extends
    subject_seen = False  # Subject: (and any trailing Where:) terminate the block
    nonblank = 0
    last_consumed = start - 1
    j = start
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "":
            # Blank lines interleave the labels — never end the block on their own.
            j += 1
            continue
        nonblank += 1
        if nonblank > _OUTLOOK_HEADER_MAX_NONBLANK:
            break
        matched = _match_header_label(ln)
        if matched:
            label, value = matched
            # Subject: is the last addressing header; only a meeting invite's trailing
            # When:/Where: may follow it. ANY other label after the subject (a new
            # From:/Date:/Sent:/To:/Cc:/Subject:) is the START of the NEXT quoted
            # message — terminate THIS block here so its fields are not overwritten by
            # the next header (and the next From: stays available as its own boundary).
            if subject_seen and label not in ("when", "where"):
                break
            cur_label = label
            fields[label] = value.strip()
            if label == "subject":
                subject_seen = True
            last_consumed = j
            j += 1
            continue
        # Not a label line. BEFORE the subject it is a wrapped value continuation of
        # the current label (a value-on-next-line, or a wrapped date/recipient list);
        # AFTER the subject it is the first body line.
        if not subject_seen and cur_label is not None:
            sep = " " if fields[cur_label] else ""
            fields[cur_label] = (fields[cur_label] + sep + ln.strip()).strip()
            last_consumed = j
            j += 1
            continue
        break  # first real body line
    # Date preference: an explicit Date:/Sent: wins; a meeting invite's When: is the
    # fallback timestamp (and confirms the date-bearing shape on invites).
    if not fields["date"] and fields["when"]:
        fields["date"] = fields["when"]
    have_date = bool(fields["date"] or fields["when"])
    if not (fields["from"] and have_date and fields["subject"]):
        return None
    return fields, last_consumed + 1


def _is_outlook_boundary(lines: list[str], i: int) -> bool:
    """True iff line ``i`` opens an Outlook header BLOCK (From: + Date/Sent/When: +
    Subject:) — the multi-line SHAPE, not a keyword. Delegates to the same
    blank-line-tolerant walker the consumption loop uses, so detection and stripping
    can't drift (D-060 rework)."""
    return _parse_outlook_header_block(lines, i) is not None


def _strip_leading_fw_wrapper(seg_lines: list[str]) -> list[str]:
    """Drop an OWA forward-wrapper header at the TOP of the latest-reply segment.

    A forwarded OWA body opens with a bare ``From: <name>`` / ``Subject: FW: ...``
    (sometimes Sent:/To:/Cc:) wrapper ABOVE the actual reply text — it is the top
    message's own forward header, not a quoted older turn (no Date: → never a
    boundary). Left in place it leaks ``From:``/``Subject:`` literals into the latest
    turn's body. We strip a leading run of header-label lines ONLY when the segment's
    FIRST non-blank line is itself a header label (so a normal prose reply, or a
    reply that merely mentions "From:" mid-sentence, is untouched). Returns the
    segment unchanged when there is no such leading wrapper (no-loss). """
    # Find the first non-blank line; bail (keep everything) if it is not a label.
    first = 0
    while first < len(seg_lines) and seg_lines[first].strip() == "":
        first += 1
    if first >= len(seg_lines) or _match_header_label(seg_lines[first]) is None:
        return seg_lines
    # Consume the contiguous (blank-tolerant) run of header-label lines + their
    # wrapped continuations, stopping at the first non-blank non-label/continuation.
    cur_label = None
    k = first
    while k < len(seg_lines):
        ln = seg_lines[k]
        if ln.strip() == "":
            k += 1
            continue
        matched = _match_header_label(ln)
        if matched:
            cur_label = matched[0]
            k += 1
            continue
        if cur_label in ("to", "cc"):
            # recipient list continuation line — still part of the wrapper
            k += 1
            continue
        break
    return seg_lines[k:]


def _split_quoted_thread(body: str, top_msg: dict) -> list[dict]:
    """Split a SINGLE inline-quoted reply chain into per-message turns.

    ``body`` is the already-_html_to_text'd body of ``top_msg`` (the latest reply
    on top, each older quoted message below an Outlook/Gmail/Original-Message
    header boundary). Returns turns NEWEST-FIRST as ``{sender, timestamp, body,
    recipients}``:

    * the FIRST segment (text above the first boundary) is the latest reply — its
      sender/recipients/timestamp come from ``top_msg``;
    * each subsequent segment is one older quoted message — its From:/Date:(Sent:)/
      To:(+Cc:) header lines are PARSED into the structured fields and STRIPPED out
      of the body (killing the run-on blob), Subject: is dropped.

    Returns [] when there is NO detectable boundary (the caller then falls back to
    the single whole-body turn — byte-identical backward compat) or when the split
    would be empty/garbage. No substantive body text is ever lost: the body is
    partitioned at boundaries, never truncated.
    """
    if not body:
        return []
    lines = body.split("\n")

    def _next_nonblank(idx: int) -> int:
        """Index of the next non-blank line at/after ``idx`` (len(lines) if none)."""
        while idx < len(lines) and lines[idx].strip() == "":
            idx += 1
        return idx

    # Find boundaries. Each boundary is (open_index, header_start) where
    # ``open_index`` is the line that opens the quoted message (a separator line or
    # the From: line itself) and ``header_start`` is the index of the From: line of
    # its Outlook header block (== open_index when the block is not separator-led, or
    # None for a bare Gmail/Original separator with no parseable Outlook block).
    boundaries: list[tuple[int, "int | None"]] = []
    i = 0
    while i < len(lines):
        block = _parse_outlook_header_block(lines, i)
        if block is not None:
            boundaries.append((i, i))
            i = block[1]  # jump past the parsed header block
            continue
        if _RE_GMAIL_WROTE.match(lines[i]) or _RE_ORIGINAL_SEP.match(lines[i]):
            # A separator (``-----Original Appointment-----``/Gmail ``wrote:``) is
            # routinely FOLLOWED by an Outlook header block. Fold the two into ONE
            # boundary so the block's headers are parsed/stripped, not left in body.
            nb = _next_nonblank(i + 1)
            follow = _parse_outlook_header_block(lines, nb)
            if follow is not None:
                boundaries.append((i, nb))
                i = follow[1]
            else:
                boundaries.append((i, None))
                i += 1
            continue
        i += 1
    if not boundaries:
        return []  # genuine single fresh email — caller keeps today's behavior

    # Segment span = [start, end). The first segment is the latest reply (text
    # above boundaries[0]); each later segment runs from its open line to the next.
    open_indices = [b[0] for b in boundaries]
    spans = [(0, open_indices[0], None)]
    for n, (open_idx, header_start) in enumerate(boundaries):
        end = open_indices[n + 1] if n + 1 < len(open_indices) else len(lines)
        spans.append((open_idx, end, header_start))

    turns: list[dict] = []
    for seg_idx, (start, end, header_start) in enumerate(spans):
        seg = lines[start:end]
        if seg_idx == 0:
            # Latest reply — fields from top_msg, whole segment is the body. Strip a
            # leading OWA forward-wrapper header (From:/Subject: FW:) so it does not
            # leak into the latest turn's body.
            seg_body = "\n".join(_strip_leading_fw_wrapper(seg))
            sender = _msg_sender_name(top_msg)
            timestamp = (
                top_msg.get("received") or top_msg.get("receivedDateTime")
                or top_msg.get("timestamp") or top_msg.get("sentDateTime")
                or top_msg.get("date") or ""
            )
            recipients = _msg_recipients_str(top_msg)
        else:
            # Quoted message. Prefer the structured header block parse (handles
            # value-on-next-line, blank-interleaving, wrapped recipient lists); fall
            # back to the bare per-line strip for a separator with no Outlook block.
            sender = timestamp = recipients = ""
            block = (
                _parse_outlook_header_block(lines, header_start)
                if header_start is not None else None
            )
            if block is not None:
                fields, body_start = block
                sender = _flatten_header_name(_strip_emphasis(fields["from"]))
                timestamp = _strip_emphasis(fields["date"])
                to_ = _flatten_header_recipients(_strip_emphasis(fields["to"]))
                cc = _flatten_header_recipients(_strip_emphasis(fields["cc"]))
                if cc:
                    recipients = (to_ + "; " + cc) if to_ else cc
                else:
                    recipients = to_
                # body_start is an absolute line index; the body is everything after
                # the header block within this segment.
                seg_body = "\n".join(lines[body_start:end])
            else:
                # Bare separator (Gmail/Original-Message) with no Outlook block —
                # strip the leading separator/blank lines, keep the rest as body.
                consumed = 0
                for ln in seg:
                    if (ln.strip() == "" or _RE_ORIGINAL_SEP.match(ln)
                            or _RE_GMAIL_WROTE.match(ln)):
                        consumed += 1
                    else:
                        break
                seg_body = "\n".join(seg[consumed:])

        seg_body = _scrub(seg_body)[:_THREAD_TURN_BODY_CAP].strip()
        if not seg_body:
            continue  # never emit a blank/ghost card
        ts_str = _one_line(str(timestamp), 100)
        turns.append({
            "sender": _scrub(_one_line(str(sender), 200)),
            "timestamp": ts_str,
            "body": seg_body,
            "recipients": _scrub(_one_line(str(recipients), 200)),
        })
    return turns


def _extract_thread_history(payload) -> list[dict]:
    """Build a structured threadHistory array from a body MCP response.

    Handles the Graph {email:{...}} shape AND the legacy {content:{emails:[...]}}
    shape (via _email_messages_from_payload). When the payload yields EXACTLY ONE
    message (the common Graph case — no messages[] array), the whole reply chain is
    quoted inline in that one body, so we route it through _split_quoted_thread to
    recover per-message turns. Otherwise (a real multi-message array) the MCP
    returns the conversation's messages newest-first, so we reverse to
    oldest->newest (or sort by a parseable timestamp when every turn carries one).
    Each turn is ``{sender, timestamp, body, recipients}`` with the body scrubbed
    and capped at _THREAD_TURN_BODY_CAP, and the array bounded to the
    _THREAD_HISTORY_MAX_TURNS newest turns. An email with no body is NOT emitted
    (we never fabricate a turn). Returns [] for any unexpected/empty payload so the
    caller can fall back to the snippet/body alone.
    """
    if not isinstance(payload, dict):
        return []
    emails = _email_messages_from_payload(payload)
    if not emails:
        return []

    # Single-message Graph case: the chain is quoted INLINE in the one body. Try to
    # split it into per-message turns; >=2 segments means we recovered the chain.
    # 0/1 segment (no boundary / garbage) falls through to the single whole-body
    # turn below — byte-identical to the pre-split behavior (backward compat).
    if len(emails) == 1 and isinstance(emails[0], dict):
        top_msg = emails[0]
        inline_body = _html_to_text(str(top_msg.get("body", "") or ""))
        split = _split_quoted_thread(inline_body, top_msg)
        if len(split) >= 2:
            # _split_quoted_thread returns newest-first; reverse to oldest->newest.
            split.reverse()
            return split[-_THREAD_HISTORY_MAX_TURNS:]

    turns: list[dict] = []
    for idx, msg in enumerate(emails):
        if not isinstance(msg, dict):
            continue
        body = _html_to_text(str(msg.get("body", "") or ""))
        body = _scrub(body)[:_THREAD_TURN_BODY_CAP].strip()
        if not body:
            continue  # never fabricate a turn for a body-less message
        sender = _msg_sender_name(msg)
        timestamp = (
            msg.get("received") or msg.get("receivedDateTime") or msg.get("timestamp")
            or msg.get("sentDateTime") or msg.get("date") or ""
        )
        ts_str = _one_line(str(timestamp), 100)
        turns.append({
            "_idx": idx,
            "_sort": _parse_ts(ts_str),
            "sender": _scrub(_one_line(str(sender), 200)),
            "timestamp": ts_str,
            "body": body,
            "recipients": _scrub(_msg_recipients_str(msg)),
        })

    # Order oldest -> newest. When EVERY turn has a parseable timestamp, sort by it
    # (authoritative, robust to whatever order the MCP returned). Otherwise the
    # email_read array is newest-first by convention, so reverse the delivered
    # order. Then cap to the most recent _THREAD_HISTORY_MAX_TURNS turns.
    if turns and all(t["_sort"] is not None for t in turns):
        turns.sort(key=lambda t: (t["_sort"], t["_idx"]))
    else:
        turns.reverse()
    ordered = [
        {"sender": t["sender"], "timestamp": t["timestamp"], "body": t["body"],
         "recipients": t["recipients"]}
        for t in turns
    ]
    return ordered[-_THREAD_HISTORY_MAX_TURNS:]


def _sender_list_from_history(history) -> list[str]:
    """Ordered, deduped list of participant display names across a threadHistory.

    Walks turns oldest->newest and appends each turn's ``sender`` the FIRST time it
    is seen (dedup key = lowercased+stripped; the STORED string is the first-seen
    display form). Empties are dropped and the result is bounded to _SENDER_LIST_MAX.

    FIRST-SEEN-WINS so no distinct participant is silently dropped and the order is
    preserved (Yibo -> Bingfeng -> Yibo => ['Yibo', 'Bingfeng']). This drives the
    From column's "+N" participant count, so a list that collapsed to only the
    newest name (or deduped by overwriting earlier entries) would undercount. A
    non-list / malformed input yields [] (never raises).
    """
    if not isinstance(history, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for turn in history:
        if not isinstance(turn, dict):
            continue
        name = str(turn.get("sender", "") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= _SENDER_LIST_MAX:
            break
    return out


def _apply_latest_sender(item: dict, history) -> None:
    """Overwrite item['sender']/['senderList'] from a non-empty threadHistory.

    Sets ``sender`` to the LATEST turn's sender (history[-1] — threadHistory is
    oldest->newest) and ``senderList`` to the ordered/deduped participant list.
    Shared by BOTH _scan_once write sites (new-item + backfill) so they can't
    drift. A no-op for an empty/malformed history so the seeded inbox sender stays
    untouched (never blanked).
    """
    if not isinstance(history, list) or not history:
        return
    last = history[-1]
    latest = ""
    if isinstance(last, dict):
        latest = str(last.get("sender", "") or "").strip()
    if latest:
        item["sender"] = _scrub(_one_line(latest, 200))
    sender_list = _sender_list_from_history(history)
    if sender_list:
        item["senderList"] = sender_list


def _normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fwd:/Fw: prefixes and lowercase, for thread matching.

    'Re: [Action needed] MAWS' and '[Action needed] MAWS' normalize to the same
    key so a standalone original and its reply thread are recognized as the same
    conversation.
    """
    s = str(subject or "").strip()
    while True:
        m = re.match(r"^\s*(re|fwd|fw)\s*:\s*", s, re.IGNORECASE)
        if not m:
            break
        s = s[m.end():]
    return s.strip().lower()


def _quoted_body_index(items) -> dict:
    """Build a lookup of {(normalized_subject, sender_lower): turn} from items that
    already carry a parsed multi-turn threadHistory.

    Some Graph message_ids fail ``get_email`` persistently while the SAME message
    is quoted inside a reply thread we DID fetch and split into turns. This index
    lets the backfill recover a body-less item's content from that already-parsed
    quoted copy instead of leaving the card stuck on the truncated snippet. We only
    index turns from multi-turn histories (a genuine quoted chain), never an item's
    own single self-turn, and keep the LONGEST body seen per key so a fuller quote
    wins. Never raises on malformed input.
    """
    index: dict = {}
    if not isinstance(items, list):
        return index
    for it in items:
        if not isinstance(it, dict):
            continue
        history = it.get("threadHistory")
        if not isinstance(history, list) or len(history) < 2:
            continue  # only a real quoted chain carries other people's messages
        subj_key = _normalize_subject(it.get("subject", ""))
        if not subj_key:
            continue
        for turn in history:
            if not isinstance(turn, dict):
                continue
            sender = str(turn.get("sender", "") or "").strip().lower()
            body = str(turn.get("body", "") or "")
            if not sender or not body:
                continue
            key = (subj_key, sender)
            prev = index.get(key)
            if prev is None or len(body) > len(prev.get("body", "")):
                index[key] = turn
    return index


def _parse_ts(value: str):
    """Best-effort parse of an email timestamp into a sortable datetime.

    Handles ISO-8601 (incl. a trailing 'Z'); returns None when unparseable so the
    caller falls back to the MCP's delivered (newest-first) ordering.
    """
    s = (value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _format_thread_history(thread_history) -> str:
    """Render a threadHistory array (oldest->newest) as readable lines.

    Tolerant of missing keys; returns "" for an empty/non-list input so the
    caller can fall back to the single body/snippet block.
    """
    if not isinstance(thread_history, list) or not thread_history:
        return ""
    lines = []
    for turn in thread_history[-_THREAD_HISTORY_MAX_TURNS:]:
        if not isinstance(turn, dict):
            continue
        sender = str(turn.get("sender", "") or "?")
        ts = str(turn.get("timestamp", "") or "")
        body = _one_line(str(turn.get("body", "")), _THREAD_TURN_BODY_CAP)
        prefix = f"{sender} ({ts})" if ts else sender
        lines.append(f"- {prefix}: {body}")
    return "\n".join(lines)


# Exact re-draft instruction (mirrors slack._REDRAFT_INSTRUCTION) appended when a
# thread has more than one turn: compose ONE cohesive reply addressing the whole
# conversation rather than answering each message in isolation.
_EMAIL_REDRAFT_INSTRUCTION = (
    "This conversation has multiple messages. Read the FULL thread above and write "
    "ONE cohesive reply that addresses everything still open — do not reply to each "
    "message separately, and do not repeat points already settled earlier in the thread."
)


def _build_email_draft_prompt(item: dict) -> str:
    """Assemble the draft-generation prompt for one email queue item.

    Mirrors slack.build_draft_prompt: injects (1) the FULL threadHistory (rendered
    oldest->newest), (2) the learned-style store, (3) the per-contact topic memory,
    a DECISIVE language rule judged from the INCOMING turns (not the style
    samples), and a re-draft instruction when the thread has >1 turn. This
    standalone builder's OUTPUT is unit-testable on its own — that's how the
    style+topic+history reaches the prompt even while ``_run_email_agent`` is
    stubbed. The assembled text is scrubbed so no credential material rides along.
    """
    sender = str(item.get("sender", ""))
    subject = str(item.get("subject", ""))
    snippet = str(item.get("snippet", ""))
    body = str(item.get("emailBody", ""))
    thread_history = item.get("threadHistory") or []

    style = read_email_style_notes().strip()
    topics = read_email_topic_summary(sender).strip()

    history_block = _format_thread_history(thread_history)
    is_redraft = isinstance(thread_history, list) and len(thread_history) > 1

    parts = [
        "You are drafting an email reply in MY voice. Match my style; be concise "
        "and professional.",
        "",
        "## My learned writing style",
        "(These samples teach TONE and PHRASING only — NOT which language to reply "
        "in. The reply language is decided ONLY by the incoming conversation, per "
        "the rule at the bottom.)",
        "",
        style or "(no style notes yet — keep it concise, direct, and warm, matching "
        "the conversation's own register)",
        "",
        f"## What {sender or 'this contact'} and I have discussed",
        topics or "(no prior context captured yet)",
        "",
        "## Email to reply to",
        f"From: {sender}",
        f"Subject: {subject}",
    ]
    if history_block:
        parts += [
            "",
            "## Full conversation thread (oldest → newest)",
            history_block,
        ]
    else:
        parts += [
            "",
            "## Message",
            (body.strip() or snippet.strip() or "(no body available)"),
        ]
    if is_redraft:
        parts += ["", _EMAIL_REDRAFT_INSTRUCTION]
    parts += [
        "",
        "## LANGUAGE RULE (decisive — overrides any language seen in my style samples)",
        "Reply in the SAME language the OTHER people are using in the incoming "
        "message and the conversation thread above. English thread → reply in "
        "English. Chinese thread → reply in Chinese. Mixed → mirror that mix. Judge "
        "ONLY from what they wrote, never from my style samples.",
        "",
        "Write only the reply text.",
    ]
    return _scrub("\n".join(parts))


async def _scan_once(app) -> None:
    """Run ONE deterministic email scan: fetch inbox -> dedupe -> fetch bodies -> write.

    Reads via Microsoft Graph (manager-outlook-mcp): get_emails for the unread
    inbox, get_email per message for the full body + thread history. Filters by new
    Graph message_id, writes needs-classify skeletons. On MCP read failure it FAILS
    LOUD (raises EmailMcpError) -- never fabricates email data.
    """
    global _LAST_SCAN_TS_MS
    now_ms = int(time.time() * 1000)
    now_epoch = now_ms / 1000.0

    # Read the persisted throttle window first so a quota-exhausted GRASP isn't
    # re-hammered: while now < graphThrottledUntil we SKIP the Graph reads entirely
    # (a REAL delay, not a hot re-fire) and let the window lapse so GRASP's quota
    # can reset.
    data, current_etag = _load()
    throttled_until = data.get("graphThrottledUntil")
    throttled = (
        isinstance(throttled_until, (int, float))
        and not isinstance(throttled_until, bool)
        and now_epoch < throttled_until
    )

    quota_hit = False  # set if THIS cycle's inbox read hits a 429/quota condition

    # Fetch the unread inbox via Graph (get_emails -> {emails:[...], count, folder}).
    # GRASP intermittently 400s this call; a transient inbox-fetch failure must NOT
    # abort the whole cycle, because the body-BACKFILL of items ALREADY in the queue
    # (below) is independent of fetching new mail. So we degrade to "no new items
    # this cycle" (raw_items=[]) and still run backfill, rather than raising and
    # leaving body-less items stuck. This is not fabrication: an empty new-item list
    # only means we discovered nothing new, and backfill operates solely on items
    # already persisted. (A persistent failure simply yields empty cycles, visible
    # via lastScanAt not advancing meaningfully and the warning log.)
    if throttled:
        logger.info(
            "Email scan: Graph throttled until %s (backing off, skipping read).",
            throttled_until,
        )
        raw_items = []
    else:
        try:
            inbox_payload = await call_read_tool("get_emails", {
                "folder": "inbox", "limit": 100, "unread_only": True,
            })
            raw_items = _unwrap_list(inbox_payload, "emails", "messages", "items", "value")
        except Exception as e:  # noqa: BLE001 -- inbox fetch is best-effort per cycle
            logger.warning("Email inbox fetch failed (continuing with backfill only): %s",
                           _scrub(str(e)))
            raw_items = []
            if _is_graph_quota_error(e):
                quota_hit = True
                reason = _scrub(_one_line(str(e), 2000))
                data["graphThrottledUntil"] = now_epoch + _GRAPH_THROTTLE_WINDOW_S
                data["lastGraphError"] = {"reason": reason, "ts": now_ms}
                logger.warning(
                    "Email scan: GRASP quota/429 detected -> backing off for %ds: %s",
                    _GRAPH_THROTTLE_WINDOW_S, reason,
                )

    # Prune mutes
    now_s = now_ms / 1000.0
    muted = _prune_muted(data.get("mutedThreads") or {}, now_s)
    data["mutedThreads"] = muted

    # Existing Graph message_ids in active statuses (keyed natively on messageId;
    # conversationId is its alias so older items resolve too).
    def _item_msg_id(it: dict) -> str:
        return str(it.get("messageId", "") or it.get("conversationId", ""))

    existing_msg_ids = {
        _item_msg_id(it)
        for it in data["items"]
        if it.get("status") in {"needs-classify", "needs-draft", "needs-review", "edited"}
    }

    # Filter new items (not already in queue, not muted) -- key on the Graph
    # message_id so the conversationId->messageId re-key drops nothing. Track the
    # ids queued THIS cycle (candidate_ids) so a conversation appearing in BOTH the
    # Inbox fetch and an allowlisted subfolder is admitted ONCE (dedup, AC-9).
    candidate_ids: set[str] = set()
    new_items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        msg_id = str(raw.get("id", "") or raw.get("messageId", ""))
        if not msg_id:
            continue
        if msg_id in existing_msg_ids or msg_id in candidate_ids:
            continue
        if muted and msg_id in muted:
            continue
        raw["_sourceFolder"] = "Inbox"
        candidate_ids.add(msg_id)
        new_items.append(raw)

    # SECOND scan source (D-058): a fixed allowlist of Inbox SUBFOLDERS, read through
    # the aws-outlook-mcp (OWA/EWS) backend via call_read_tool -- OFF the GRASP quota
    # (these tools are NOT in _GRAPH_READ_TOOLS, so they route to the legacy session,
    # never the Graph/manager-outlook session). The cheap LIST calls (email_list_
    # folders + per-folder email_folders, ~15/tick) ALWAYS run; the expensive per-
    # conversation email_read body-prefetch runs ONLY for ADMITTED folder candidates
    # below (headroom-bounded). Discovery + each per-folder read is wrapped in its
    # OWN try/except that logs a WARNING and CONTINUES -- one bad folder never aborts
    # the cycle (mirrors the inbox-fetch best-effort above; AC-10). Never fabricate.
    resolved_folders: list[tuple[str, str]] = []
    try:
        folders_payload = await call_read_tool("email_list_folders", {})
        resolved_folders = _resolve_scan_subfolders(folders_payload)
    except Exception as e:  # noqa: BLE001 -- folder discovery is best-effort per cycle
        logger.warning("Email folder discovery failed (continuing without subfolders): %s",
                       _scrub(str(e)))

    for folder_name, folder_id in resolved_folders:
        try:
            folder_payload = await call_read_tool(
                "email_folders", {"folderId": folder_id, "limit": 100})
        except Exception as e:  # noqa: BLE001 -- one folder read must NOT abort the cycle
            logger.warning("Email subfolder read failed for %r (skipping this folder "
                           "this cycle): %s", folder_name, _scrub(str(e)))
            continue
        # email_folders has NO unread_only param: it returns read+unread mixed, with
        # `items` NOT populated in the listing. Filter client-side to conversations
        # with unreadCount>0 so already-handled mail never re-floods the queue (AC-3).
        conversations = _unwrap_list(folder_payload, "emails", "messages", "items", "value")
        for conv in conversations:
            if not isinstance(conv, dict):
                continue
            unread = conv.get("unreadCount")
            if not (isinstance(unread, (int, float)) and not isinstance(unread, bool)
                    and unread > 0):
                continue
            conv_id = str(conv.get("conversationId", "") or conv.get("id", ""))
            if not conv_id:
                continue
            if conv_id in existing_msg_ids or conv_id in candidate_ids:
                continue
            if muted and conv_id in muted:
                continue
            candidate_ids.add(conv_id)
            # Shape a lightweight raw candidate that flows through the SAME machinery
            # (_graph_ts_ms FIFO, _skeleton_for): key id/messageId/conversationId on
            # the conversationId (Graph items alias conversationId==messageId too), so
            # dedup/skeleton keying stay consistent. Carry the source-folder name and
            # the conversationId for the per-conversation email_read body-prefetch.
            new_items.append({
                "id": conv_id,
                "messageId": conv_id,
                "conversationId": conv_id,
                "subject": conv.get("topic", "") or conv.get("subject", ""),
                "senders": conv.get("senders") or [],
                "preview": conv.get("preview", ""),
                "received": conv.get("lastDeliveryTime", "")
                            or conv.get("received", ""),
                "messageCount": conv.get("messageCount") or conv.get("unreadCount") or 1,
                "_sourceFolder": folder_name,
                "_owaConversationId": conv_id,
            })

    # Active-to-do cap (replenish-on-clear): bound the actionable queue at
    # EMAIL_ACTIVE_TODO_CAP. Compute the current actionable count from the persisted
    # store using the SAME predicate as the "N to review" count (_count_actionable /
    # _TERMINAL_STATUSES, D-035) — never a second inline definition — then admit at
    # most the remaining headroom this cycle. Admit OLDEST-FIRST (FIFO) so the
    # backlog drains toward zero and nothing starves under steady incoming mail; the
    # Graph fetch returns newest-first, so we sort eligible new_items by their Graph
    # timestamp (the same epoch the skeleton `ts` uses, stable tie-break on msg id)
    # before truncating to headroom. Non-admitted unread are simply not ingested THIS
    # cycle (no half-ingested body-less ghosts) and get picked up later once a cleared
    # item frees a slot. This caps the ACTIVE LIST, not the get_emails read window.
    current_actionable = _count_actionable(data["items"])
    headroom = max(0, EMAIL_ACTIVE_TODO_CAP - current_actionable)
    new_items.sort(key=lambda r: (_graph_ts_ms(r),
                                  str(r.get("id", "") or r.get("messageId", ""))))
    admitted = new_items[:headroom]

    # For each admitted email, pre-fetch body + thread history via Graph get_email.
    # The SAME response ({email:{...}}) yields BOTH the concatenated body
    # (_extract_email_body) and the structured multi-turn threadHistory
    # (_extract_thread_history), so the draft worker needs no further MCP call.
    for raw in admitted:
        msg_id = str(raw.get("id", "") or raw.get("messageId", ""))
        owa_conv_id = raw.get("_owaConversationId")
        if owa_conv_id:
            # FOLDER candidate (D-058): read the conversation via the OWA backend.
            # markAs SAFETY (AC-6, load-bearing): call email_read WITHOUT any markAs
            # key. Per docs/meshclaw-email-integration-report.md §3, omitting markAs
            # issues a pure GetConversationItems fetch with NO SetReadState, so the
            # message stays UNREAD; markAs:'read' would fire ApplyConversationAction/
            # SetReadState. NEVER add markAs here (D-058 §5/§7). The conversation's
            # full message list IS the thread history (AC-4) -- _owa_conversation_to_
            # payload remaps the OWA field names (itemId/recipients/ccRecipients) onto
            # the canonical shape the existing extractors consume.
            try:
                read_payload = await call_read_tool(
                    "email_read", {"conversationId": owa_conv_id, "format": "html"})
                owa_msgs = _unwrap_list(read_payload, "emails", "messages", "items")
                body_payload = _owa_conversation_to_payload(owa_msgs)
                body_text = _extract_email_body(body_payload)
                if body_text:
                    raw["emailBody"] = _safe_body_truncate(body_text, _EMAIL_BODY_CAP)
                history = _extract_thread_history(body_payload)
                if history:
                    raw["threadHistory"] = history
                # SENDER LIFT (D-068): the folder candidate carried only a NAME list,
                # so _skeleton_for/_flatten_from would HARD-CODE senderEmail "". Lift
                # the head sender {name,email} off the OWA payload BEFORE _skeleton_for
                # runs so it captures the real address (mirrors the inbox path).
                _lift_owa_sender(raw, body_payload)
                _capture_recipients(raw, body_payload)
                # RECIPIENT CAPTURE FROM BODY (D-068): the OWA recipients[] are
                # NAME-ONLY, so fill the matching toRecipients/ccRecipients email from
                # the quoted-body header lines (match-or-leave-blank, never fabricate).
                _fill_recipient_emails_from_body(raw, body_text)
            except Exception as e:  # noqa: BLE001 -- body fetch is best-effort
                logger.warning("Email folder body fetch failed for %s (%s): %s",
                               owa_conv_id, raw.get("_sourceFolder"), _scrub(str(e)))
        elif msg_id:
            try:
                body_payload = await call_read_tool("get_email", {"message_id": msg_id})
                body_text = _extract_email_body(body_payload)
                if body_text:
                    raw["emailBody"] = _safe_body_truncate(body_text, _EMAIL_BODY_CAP)
                history = _extract_thread_history(body_payload)
                if history:
                    raw["threadHistory"] = history
                # Capture To/CC from the SAME detail payload (the inbox-LIST call
                # has none) at ZERO extra MCP cost. These drive the reply-all
                # default + recipientType (D-056 #3); a missing key yields [].
                _capture_recipients(raw, body_payload)
            except Exception as e:  # noqa: BLE001 -- body fetch is best-effort
                logger.warning("Email body fetch failed for %s: %s", msg_id, _scrub(str(e)))

    # Backfill: retry body fetch for existing items that have an empty emailBody
    # (prior scan failures, timeouts, or transient errors). Cap at 5 per cycle to
    # avoid overwhelming the MCP session.
    #
    # D-068 SELF-HEAL: a folder item ALSO re-enters backfill when its emailBody is
    # already populated but its senderEmail is blank — otherwise a healed-body folder
    # item with a blank sender would NEVER re-enter a body-only-gated backfill (the
    # FRESH-ONLY / CODE-ONLY-NO-DATA-HEAL trap), and the ~200 already-broken items
    # would stay permanently blank. The per-cycle cap below still bounds it, so the
    # backlog drains over successive scans. Inbox items keep the original body-only
    # gate.
    #
    # The re-inclusion is gated on the BLANK SENDER (a determinate, always-healable
    # signal — the OWA head always carries sender.email), NOT on blank recipient
    # emails alone. Recipient addresses are recovered OPPORTUNISTICALLY on the same
    # pass (_fill_recipient_emails_from_body), but a single fresh folder message
    # legitimately has NO quoted-body header lines, so its name-only recipients can
    # never resolve an address — re-including such an item forever on a blank-recipient
    # signal would PERMANENTLY occupy a backfill slot and STARVE the other items'
    # sender heal. So once an item's sender is healed (and body present) it stops
    # re-entering backfill, letting the next blank-sender items drain.
    _BACKFILL_PER_CYCLE = 5

    def _folder_needs_sender_heal(it: dict) -> bool:
        sf = it.get("sourceFolder")
        if not sf or sf == "Inbox":
            return False
        return not str(it.get("senderEmail") or "").strip()

    missing_body = [
        it for it in data["items"]
        if (not it.get("emailBody") or _folder_needs_sender_heal(it))
        and it.get("status") in {"needs-classify", "needs-draft", "needs-review", "edited"}
        and (it.get("messageId") or it.get("conversationId"))
    ][:_BACKFILL_PER_CYCLE]
    # Quoted-copy fallback: some Graph message_ids fail get_email persistently while
    # the SAME message is quoted inside a reply thread we already fetched and split.
    # Build the index once so a body-less item can recover its content from that
    # parsed quoted copy instead of being stuck on the truncated snippet.
    quoted_index = _quoted_body_index(data["items"])
    # While throttled (or after THIS cycle's inbox read hit a quota/429) skip the
    # per-message Graph get_email so we don't keep hammering the exhausted quota;
    # the MCP-FREE quoted-copy recovery below still runs.
    skip_graph_reads = throttled or quota_hit
    for it in missing_body:
        msg_id = str(it.get("messageId", "") or it.get("conversationId", ""))
        body_text = ""
        history = None
        # Folder-sourced items (D-058) backfill through the OWA email_read path, NOT
        # Graph get_email -- so they stay OFF the GRASP quota even on retry and are
        # NOT skipped while Graph is throttled (skip_graph_reads is a GRAPH concern).
        # markAs is OMITTED here too (leaves the mail UNREAD; see prefetch comment).
        source_folder = it.get("sourceFolder")
        is_folder_item = bool(source_folder) and source_folder != "Inbox"
        if is_folder_item:
            try:
                read_payload = await call_read_tool(
                    "email_read", {"conversationId": msg_id, "format": "html"})
                owa_msgs = _unwrap_list(read_payload, "emails", "messages", "items")
                body_payload = _owa_conversation_to_payload(owa_msgs)
                body_text = _extract_email_body(body_payload)
                history = _extract_thread_history(body_payload)
                # SELF-HEAL (D-068): lift the sender + parse recipients-from-body onto
                # the EXISTING persisted item so the ~200 already-blank folder items
                # drain over successive scans. The OWA re-fetch's recipients[] are
                # NAME-ONLY, so snapshot any prior-recovered emails BEFORE
                # _capture_recipients overwrites the list, then re-fill from the body
                # AND that prior snapshot — never blanking an already-good
                # sender/recipient (anti SELF-HEAL-CLOBBERS-GOOD-DATA).
                prior_recip = _recipient_emails_snapshot(it)
                _lift_owa_sender(it, body_payload)
                _capture_recipients(it, body_payload)
                _fill_recipient_emails_from_body(it, body_text, prior=prior_recip)
            except Exception as e:  # noqa: BLE001 -- one folder backfill must not abort
                logger.warning("Email folder body backfill failed for %s (%s): %s",
                               msg_id, source_folder, _scrub(str(e)))
        elif not skip_graph_reads:
            try:
                body_payload = await call_read_tool("get_email", {"message_id": msg_id})
                body_text = _extract_email_body(body_payload)
                history = _extract_thread_history(body_payload)
                # Backfill To/CC onto the existing item from the SAME detail payload
                # (zero extra MCP cost); only writes when the detail actually carries
                # them so a body-less retry never blanks prior recipients.
                _capture_recipients(it, body_payload)
            except Exception as e:  # noqa: BLE001
                logger.warning("Email body backfill failed for %s: %s", msg_id, _scrub(str(e)))
                if _is_graph_quota_error(e):
                    skip_graph_reads = True
                    reason = _scrub(_one_line(str(e), 2000))
                    data["graphThrottledUntil"] = now_epoch + _GRAPH_THROTTLE_WINDOW_S
                    data["lastGraphError"] = {"reason": reason, "ts": now_ms}

        if not body_text:
            # get_email failed or returned nothing — try to recover from a copy of
            # this message quoted in another already-fetched thread (same subject
            # sans Re:/Fwd:, same sender). Never fabricates: only used when a real
            # quoted copy exists.
            quoted = quoted_index.get(
                (_normalize_subject(it.get("subject", "")),
                 str(it.get("sender", "") or "").strip().lower())
            )
            if quoted and quoted.get("body"):
                body_text = str(quoted["body"])
                history = [dict(quoted)]
                logger.info("Email body recovered from quoted copy for %s", msg_id)

        if body_text:
            it["emailBody"] = _safe_body_truncate(body_text, _EMAIL_BODY_CAP)
        if history:
            it["threadHistory"] = history
            # Same overwrite as the new-item path: land the latest sender +
            # participant list on the EXISTING persisted item `it` so a thread
            # last replied to by someone other than the original sender shows
            # the latest sender in the From column.
            _apply_latest_sender(it, history)

    # Write needs-classify skeletons (admitted items only — the cap was applied
    # above, so non-admitted unread are NOT persisted this cycle).
    for raw in admitted:
        skeleton = _skeleton_for(raw)
        # Source-folder tag (D-058, AC-2/AC-7): the folder display name for folder-
        # sourced items, "Inbox" for the root path. Set on BOTH paths in lockstep
        # (raw carries _sourceFolder either way) so an Inbox item is never blank.
        skeleton["sourceFolder"] = raw.get("_sourceFolder") or "Inbox"
        if raw.get("emailBody"):
            skeleton["emailBody"] = raw["emailBody"]
        # Carry forward the To/CC captured from the get_email detail payload above
        # (the inbox-list `raw` _skeleton_for sees has none) plus the resolved
        # recipientType, so the reply-all default has the data on first render.
        if raw.get("toRecipients"):
            skeleton["toRecipients"] = raw["toRecipients"]
        if raw.get("ccRecipients"):
            skeleton["ccRecipients"] = raw["ccRecipients"]
        if raw.get("recipientType"):
            skeleton["recipientType"] = raw["recipientType"]
        history = raw.get("threadHistory")
        if history:
            skeleton["threadHistory"] = history
            # Overwrite the seeded (inbox 'from') sender with the LATEST turn's
            # sender so the From column shows who most recently messaged me, and
            # attach the ordered/deduped participant list driving the "+N" count.
            # threadHistory is oldest->newest, so the newest turn is history[-1].
            # MUST land on the persisted `skeleton`, NOT the throwaway `raw` (which
            # _skeleton_for re-derives sender from). An empty history leaves the
            # seeded sender untouched (no overwrite, no blank).
            _apply_latest_sender(skeleton, history)
        data["items"].append(skeleton)

    # Update lastScanAt
    _LAST_SCAN_TS_MS = now_ms
    data["lastScanAt"] = now_ms

    try:
        filestore.write_json(EMAIL_PATH, data, current_etag)
    except filestore.ConflictError:
        # Concurrent write won -- skip this cycle.
        return

    await app["ws_manager"].broadcast("email_changed", {"scanned": True})


async def _reap_idle_graph_session(app) -> None:
    """Tear down the persistent Graph session when paused or idle.

    The GRASP bundle's TokenRefreshRunner fires a full SAML re-auth every ~10 min
    for as long as the manager-outlook-mcp session is held open -- burning GRASP's
    API-Gateway quota even while the scan worker is paused. So we must NOT hold it:
    if the queue is paused OR no Graph call has run for _GRAPH_IDLE_TEARDOWN_S, we
    call the EXISTING _disconnect_graph_mcp (NOT a no-reconnect flag -- the session
    re-connects lazily on the next scan/read via _connect_graph_mcp). Best-effort:
    a corrupt sidecar or a teardown error must never break the worker tick.
    """
    try:
        data, _ = _load()
        paused = bool(data.get("paused"))
    except Exception:
        paused = False

    idle = (time.time() - _graph_last_activity) > _GRAPH_IDLE_TEARDOWN_S
    if not (paused or idle):
        return

    try:
        await _disconnect_graph_mcp()
    except Exception as e:  # noqa: BLE001 -- reaping must never break the tick
        logger.debug("Graph idle reaper teardown failed: %s", _scrub(str(e)))


async def _run_guarded_scan(app) -> bool:
    """Run ONE _scan_once under the shared scan guard; skip if one is in flight."""
    lock = _get_scan_lock()
    if lock.locked():
        return False
    await lock.acquire()
    try:
        await _scan_once(app)
        return True
    finally:
        lock.release()


async def _scan_worker(app) -> None:
    """Deterministic email scan worker (mirrors Slack scan worker).

    Sleep-first (inert on startup), interruptible via the wake Event.
    Gates on readiness + pause check.
    """
    event = _get_scan_event()
    while True:
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=EMAIL_SCAN_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        event.clear()

        try:
            # Reap the persistent Graph session on EVERY tick -- BEFORE the
            # readiness/pause early-returns -- so a session that got reconnected by
            # a race (or held open while idle) is torn down and GRASP's refresh
            # timer can't keep burning quota.
            await _reap_idle_graph_session(app)

            if not _probe_readiness().get("ready"):
                continue

            data, _ = _load()
            if data.get("paused"):
                logger.info("Email scan-worker: paused, skipping this cycle.")
                continue

            if not await _run_guarded_scan(app):
                logger.info("Email scan-worker: a scan is already in progress, skipping.")
                continue
            mark_worker_run(app, 'Email Scanner')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Email scan-worker iteration failed: %s", _scrub(str(e)))


# ── Classify worker ────────────────────────────────────────────────────────

async def _classify_one(app, batch: list[dict]) -> None:
    """Classify ONE batch of needs-classify items via the MCP-FREE seam.

    On unavailable result, items are left as-is (retried next cycle).
    On success, re-read fresh, re-locate items, flip status per classification.
    """
    payload_items = [
        {
            "id": it.get("id"),
            "sender": it.get("sender", ""),
            "subject": it.get("subject", ""),
            "snippet": it.get("snippet", ""),
        }
        for it in batch
    ]
    result = await _run_email_agent("classify", {"items": payload_items})

    if not result.get("available"):
        logger.info(
            "Email classify-worker: batch unavailable: %s",
            _scrub(str(result.get("reason", "")))[:200],
        )
        return

    # Build id -> (label, next_status) map from untrusted classifications
    routing: dict[str, tuple[str, str]] = {}
    for entry in result.get("classifications") or []:
        if not isinstance(entry, dict):
            continue
        iid = str(entry.get("id", "") or "")
        label = str(entry.get("classification", "") or "").strip().lower()
        next_status = _CLASSIFY_NEXT_STATUS.get(label)
        if iid and next_status:
            routing[iid] = (label, next_status)
    if not routing:
        return

    # Re-read fresh before write
    data, current_etag = _load()
    changed = False
    for item in data["items"]:
        iid = str(item.get("id", "") or "")
        if item.get("status") != "needs-classify":
            continue
        routed = routing.get(iid)
        if routed:
            label, next_status = routed
            item["classification"] = label
            item["status"] = next_status
            changed = True

    if not changed:
        return

    try:
        filestore.write_json(EMAIL_PATH, data, current_etag)
    except filestore.ConflictError:
        return

    await app["ws_manager"].broadcast("email_changed", {"classified": True})


async def _classify_pending(app) -> None:
    """Classify every needs-classify item on disk, batched."""
    data, _ = _load()
    pending = [
        it for it in data["items"]
        if it.get("id") and it.get("status") == "needs-classify"
    ]
    if not pending:
        return
    for start in range(0, len(pending), EMAIL_CLASSIFY_BATCH):
        await _classify_one(app, pending[start:start + EMAIL_CLASSIFY_BATCH])


async def _classify_worker(app) -> None:
    """Email classify worker: batches needs-classify items through the seam.

    Sleep-first (inert on startup), gates on readiness + pause.
    """
    while True:
        try:
            await asyncio.sleep(EMAIL_CLASSIFY_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        try:
            if not _probe_readiness().get("ready"):
                continue

            data, _ = _load()
            if data.get("paused"):
                logger.info("Email classify-worker: paused, skipping this cycle.")
                continue

            await _classify_pending(app)
            mark_worker_run(app, 'Email Classify Worker')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Email classify-worker iteration failed: %s", _scrub(str(e)))


# ── Draft worker ───────────────────────────────────────────────────────────

def _needs_draft(item: dict) -> bool:
    """True if this item is awaiting a draft from the worker."""
    status = item.get("status")
    if status == "needs-draft":
        return True
    return False


async def _draft_one(app, sem: asyncio.Semaphore, item_id: str) -> None:
    """Draft ONE item via the MCP-FREE seam, then persist atomically.

    Concurrency-bounded by sem. On unavailable seam result the item is left
    as-is (retried next cycle). On success, re-read fresh, re-locate by id,
    apply scrubbed+capped draft, flip status to needs-review.
    """
    async with sem:
        # Read current item state for the prompt
        data, _ = _load()
        item = next((it for it in data["items"] if it.get("id") == item_id), None)
        if item is None:
            return
        if item.get("status") not in ("needs-draft",):
            return  # Moved on since we were selected

        result = await _run_email_agent("draft", {
            "id": item_id,
            "item": item,
            "prompt": _build_email_draft_prompt(item),
        })

    if not result.get("available"):
        logger.info(
            "Email draft-worker: item %s draft unavailable: %s",
            item_id, _scrub(str(result.get("reason", "")))[:200],
        )
        return

    new_draft = _scrub(str(result.get("draft", "")))[:_DRAFT_CAP]
    if not new_draft.strip():
        return  # Empty draft not useful -- retry later

    generated = _scrub(str(result.get("generatedDraft", new_draft)))[:_DRAFT_CAP]

    # Re-read fresh right before the write
    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if item is None:
        return  # Vanished (dismissed/approved)

    if item.get("status") == "approved":
        return  # Already approved under us

    # Only draft machine-generated items
    cur_draft = str(item.get("draft", ""))
    cur_generated = str(item.get("generatedDraft", ""))
    if cur_draft.strip() and cur_draft.strip() != cur_generated.strip():
        return  # User edited -- don't overwrite

    item["draft"] = new_draft
    item["generatedDraft"] = generated
    thread_ctx = result.get("threadContext", "")
    if isinstance(thread_ctx, str) and thread_ctx.strip():
        item["threadContext"] = thread_ctx.strip()
    # threadAsk (what the sender needs from me) rides the SAME draft call. An
    # absent/empty ask leaves any prior threadAsk untouched and never fails the
    # save -- a no-ask FYI keeps an empty ask, never a fabricated one.
    ask = result.get("threadAsk", "")
    if isinstance(ask, str) and ask.strip():
        item["threadAsk"] = ask.strip()
    item["status"] = "needs-review"

    try:
        filestore.write_json(EMAIL_PATH, data, current_etag)
    except filestore.ConflictError:
        return  # Concurrent write -- retry next cycle

    await app["ws_manager"].broadcast("email_changed", {"id": item_id, "drafted": True})


async def _draft_pending(app, sem: asyncio.Semaphore) -> None:
    """Draft every needs-draft item currently on disk, in parallel."""
    data, _ = _load()
    pending = [
        it.get("id")
        for it in data["items"]
        if it.get("id") and _needs_draft(it)
    ]
    if not pending:
        return
    await asyncio.gather(
        *(_draft_one(app, sem, item_id) for item_id in pending)
    )


async def _draft_worker(app) -> None:
    """Email draft worker: drafts needs-draft items in parallel.

    Sleep-first (inert on startup), gates on readiness + pause.
    """
    sem = asyncio.Semaphore(EMAIL_DRAFT_CONCURRENCY)
    while True:
        try:
            await asyncio.sleep(EMAIL_DRAFT_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        try:
            if not _probe_readiness().get("ready"):
                continue

            data, _ = _load()
            if data.get("paused"):
                logger.info("Email draft-worker: paused, skipping this cycle.")
                continue

            await _draft_pending(app, sem)
            mark_worker_run(app, 'Email Draft Worker')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Email draft-worker iteration failed: %s", _scrub(str(e)))


# ── Worker lifecycle ────────────────────────────────────────────────────────

# Single env flag gating the three PERIODIC email workers (scan/classify/draft),
# default OFF. Parsed against an explicit truthy allowlist (case-insensitive) — a
# bare bool(os.environ.get(...)) would treat the common "set it to 0 to disable"
# operator habit ('0'/'false' are non-empty -> truthy) as ENABLE, silently breaking
# the default-OFF guarantee. Does NOT gate the persistent-MCP cleanup or any
# non-email worker.
async def _start_scan_worker(app) -> None:
    """on_startup: seed _LAST_SCAN_TS_MS from persisted lastScanAt, then launch.

    Like the Slack workers, the email workers always start on app startup; the
    ONLY runtime control is the store's ``paused`` flag (toggled from the UI via
    PUT /api/email/config), which every worker loop honors per cycle. The former
    CLAUDE_WEB_EMAIL_WORKERS startup env gate (added as a post-GRASP-outage kill
    switch) was retired once the quota-burn cause was fixed structurally: scan
    reads go through the OWA backend (MMCP_AUTH_BACKEND=owa, off the GRASP quota)
    and the scan worker reaps the idle Graph session every tick, while classify
    and draft are MCP-free (claude --print). Pause from the UI to stop the loops.
    """
    global _LAST_SCAN_TS_MS
    if _LAST_SCAN_TS_MS is None:
        try:
            data, _ = _load()
            persisted = data.get("lastScanAt")
            if isinstance(persisted, (int, float)) and not isinstance(persisted, bool):
                _LAST_SCAN_TS_MS = int(persisted)
        except Exception:
            pass
    task = app.get("email_scan_task")
    if task is None or task.done():
        app["email_scan_task"] = asyncio.create_task(_scan_worker(app))
        register_worker(app, 'Email Scanner', 'email_scan_task', EMAIL_SCAN_INTERVAL_S, project='claude-web', description='Fetches new emails from your Outlook inbox')


async def _stop_scan_worker(app) -> None:
    task = app.pop("email_scan_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _start_classify_worker(app) -> None:
    task = app.get("email_classify_task")
    if task is None or task.done():
        app["email_classify_task"] = asyncio.create_task(_classify_worker(app))
        register_worker(app, 'Email Classify Worker', 'email_classify_task', EMAIL_CLASSIFY_POLL_INTERVAL_S, project='claude-web', description='Sorts new emails into actionable vs. ignorable')


async def _stop_classify_worker(app) -> None:
    task = app.pop("email_classify_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _start_draft_worker(app) -> None:
    task = app.get("email_draft_task")
    if task is None or task.done():
        app["email_draft_task"] = asyncio.create_task(_draft_worker(app))
        register_worker(app, 'Email Draft Worker', 'email_draft_task', EMAIL_DRAFT_POLL_INTERVAL_S, project='claude-web', description='Generates draft replies for emails awaiting your review')


async def _stop_draft_worker(app) -> None:
    task = app.pop("email_draft_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _cleanup_persistent_mcp(app) -> None:
    """on_cleanup: tear down ALL persistent email MCP sessions (reap subprocesses).

    Disconnects the legacy aws-outlook-mcp READ session, the manager-outlook-mcp
    (Graph) session AND the aws-outlook-mcp WRITE session so no child subprocess
    is left orphaned on shutdown (and so GRASP's refresh timer dies with the
    Graph subprocess).
    """
    await _disconnect_mcp()
    await _disconnect_graph_mcp()
    await _disconnect_owa_write()


# ── Route handlers ──────────────────────────────────────────────────────────

async def get_health(request: web.Request) -> web.Response:
    """GET /api/email/health -- readiness probe.

    Surfaces the Graph (manager-outlook-mcp) Node-24 readiness so a missing Node
    24 reads as an ACTIONABLE state (graphNodeReady:false + a reason) rather than a
    silent empty queue that looks like 'inbox is empty'.
    """
    claude_on_path = shutil.which("claude") is not None
    node = _graph_node_status()
    return web.json_response({
        "claudeOnPath": claude_on_path,
        "graphNodeReady": node["graphNodeReady"],
        "graphNodeReason": node["graphNodeReason"],
        "ready": claude_on_path,
    })


def _graph_throttled_live(data: dict) -> bool:
    """Compute graphThrottled LIVE as now < graphThrottledUntil (self-clearing).

    NEVER persisted as a sticky bool: an expired window reads False the instant it
    lapses, so the UI can show "rate-limited" only while it's actually true and the
    banner self-clears on recovery (no inverse-lie permanent banner).
    """
    until = data.get("graphThrottledUntil")
    if not (isinstance(until, (int, float)) and not isinstance(until, bool)):
        return False
    return time.time() < until


def _last_graph_error_reason(data: dict) -> str | None:
    """Flatten the persisted lastGraphError ({reason, ts}) to its human string.

    The sidecar stores a machine-readable dict (reason + ts) so backoff has a
    timestamp; the queue response exposes just the human ``reason`` string for the
    UI banner. Tolerates a bare string (older shape) and returns None when absent.
    """
    err = data.get("lastGraphError")
    if isinstance(err, dict):
        reason = err.get("reason")
        return reason if isinstance(reason, str) and reason.strip() else None
    if isinstance(err, str) and err.strip():
        return err.strip()
    return None


async def get_queue(request: web.Request) -> web.Response:
    """GET /api/email/queue -- return the email queue."""
    data, etag = await _load_async()
    return web.json_response({
        "items": data["items"],
        "etag": etag,
        "lastScanAt": data.get("lastScanAt"),
        "paused": data.get("paused", False),
        # Machine-readable throttle flag (computed LIVE -> self-clearing) + the
        # human/operator reason so the UI can honestly show "Outlook temporarily
        # rate-limited (GRASP quota) — retrying later" instead of silently failing.
        "graphThrottled": _graph_throttled_live(data),
        "lastGraphError": _last_graph_error_reason(data),
    })


# Terminal statuses that do NOT count toward "to review" — kept in lockstep with
# the frontend's isActionable (frontend/src/lib/emailQueue.js, D-035). Email's
# terminal outbound state is 'approved' (NOT 'sent'); 'deleted' is set by
# delete_item once a message is moved to Deleted Items, so a deleted-but-not-purged
# item is terminal and must NOT eat a capped active-to-do slot (AC-5). An explicit
# denylist of these three states means a new active status (or a missing/empty
# status on an older item) ALWAYS counts, so the count can never silently undercount.
_TERMINAL_STATUSES = ("approved", "dismissed", "deleted")


def _count_actionable(items) -> int:
    """Count items that still need attention — mirrors frontend countEmailActionable.

    Actionable == status NOT in the terminal set {approved, dismissed, deleted}; a
    missing/empty status counts. This is the SINGLE source of truth shared by the
    "N to review" count and the active-to-do cap (EMAIL_ACTIVE_TODO_CAP).
    """
    if not isinstance(items, list):
        return 0
    return sum(
        1 for it in items
        if isinstance(it, dict) and it.get("status") not in _TERMINAL_STATUSES
    )


async def get_queue_count(request: web.Request) -> web.Response:
    """GET /api/email/queue/count -- just the actionable-item count.

    Lightweight sidebar-badge endpoint: returns ONLY {"count": N} (no item
    payloads), N computed with the SAME semantics as the frontend's
    countEmailActionable so the NavBar bubble matches the full-queue count. Reads
    the sidecar via the async filestore variant so it never blocks the loop.
    """
    data, _ = await _load_async()
    return web.json_response({"count": _count_actionable(data["items"])})


async def put_config(request: web.Request) -> web.Response:
    """PUT /api/email/config -- toggle paused state."""
    body = await read_json_body(request)
    if "paused" not in body:
        raise web.HTTPBadRequest(reason="paused field required")

    data, current_etag = await _load_async()
    data["paused"] = bool(body["paused"])

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"paused": data["paused"]})
    return web.json_response({"paused": data["paused"], "etag": new_etag})


def _sanitize_recipient_emails(value) -> list[str]:
    """Validate + de-dupe a user-supplied recipient list into email strings.

    Accepts a list of email strings (the editor's shape) or {name,email} dicts;
    keeps only basically-valid addresses (_valid_email), de-dupes case-insensitively
    and NEVER fabricates. A non-list yields []. Recipient emails are user-data, so
    this is the validation gate before the value is persisted/applied.
    """
    if not isinstance(value, list):
        return []
    addrs = [_recipient_email(e) for e in value]
    return _dedupe_emails_ci([a for a in addrs if _valid_email(a)])


async def save_draft(request: web.Request) -> web.Response:
    """PUT /api/email/queue/{item_id} -- save an edited draft (+ optional To/CC).

    ``draft`` is the ONLY required field. Optional ``toRecipients``/``ccRecipients``
    arrays (email strings) are the user's reply-all edits: validated, de-duped
    case-insensitively, persisted as plain email-string lists so they survive a
    refresh and become the values approve applies (recipientsEdited flag flips so
    approve uses them verbatim instead of recomputing the default, D-056 #3).
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")
    if "draft" not in body:
        raise web.HTTPBadRequest(reason="draft field required")
    new_draft = _scrub(str(body["draft"]))[:_DRAFT_CAP]

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    item["draft"] = new_draft
    if new_draft.strip() != str(item.get("generatedDraft", "")).strip():
        item["status"] = "edited"

    # Optional reply-all recipient edits. Only touch the item when the key is
    # present so a draft-only save never wipes captured/edited recipients.
    if "toRecipients" in body or "ccRecipients" in body:
        if "toRecipients" in body:
            item["toRecipients"] = _sanitize_recipient_emails(body.get("toRecipients"))
        if "ccRecipients" in body:
            item["ccRecipients"] = _sanitize_recipient_emails(body.get("ccRecipients"))
        item["recipientsEdited"] = True

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id})
    return web.json_response({"item": item, "etag": new_etag})


# Graph mark_email_read accepts at most 10 message_ids per call (verified live).
_MARK_READ_BATCH = 10


async def _mark_messages_read(messages: dict) -> None:
    """Mark one or more messages read in Outlook via Graph mark_email_read.

    ``messages`` is a {message_id: subject-or-None} map. The call goes through the
    SEPARATE explicitly-gated graph write path (_call_graph_write_tool), NOT
    call_read_tool and NOT the read-only frozenset -- the structural guarantee that
    the read client never sends. Batched ≤10 ids/call (Graph's limit).

    VERIFY EFFECT, NOT CALLER: we INSPECT results[]/summary and treat any
    success:false (or an empty/malformed response) as a failure -- never claim a
    read that did not happen. A failure is logged at WARNING (not invisible debug)
    and is NON-FATAL: it must never break the dismiss the caller already persisted.
    """
    clean = {
        str(mid): (subj if subj is None else str(subj))
        for mid, subj in (messages or {}).items()
        if str(mid or "").strip()
    }
    if not clean:
        return

    ids = list(clean.keys())
    for start in range(0, len(ids), _MARK_READ_BATCH):
        chunk = ids[start:start + _MARK_READ_BATCH]
        batch = {mid: clean[mid] for mid in chunk}
        try:
            # SHORT-LIVED spawn: reuses the cached token, performs the call, exits.
            # Never depends on the always-on persistent session (which we now reap
            # while idle) and never re-arms GRASP's refresh timer.
            result = await _call_graph_write_tool_oneshot(
                "mark_email_read", {"emails": batch, "is_read": True}
            )
        except Exception as e:  # noqa: BLE001 -- never fail the dismiss
            logger.warning(
                "mark-read failed for %d message(s): %s",
                len(chunk), _scrub(str(e)),
            )
            continue

        # Inspect the per-id results -- a {success:false} or a missing/empty
        # results array means the read did NOT flip. Surface it at WARNING.
        results = result.get("results") if isinstance(result, dict) else None
        if not isinstance(results, list) or not results:
            logger.warning(
                "mark-read returned no results for %d message(s) (response: %s)",
                len(chunk), _scrub(_one_line(str(result), 200)),
            )
            continue
        failed = [
            str(r.get("message_id", ""))
            for r in results
            if isinstance(r, dict) and not r.get("success")
        ]
        if failed:
            logger.warning(
                "mark-read did NOT flip is_read for %d message(s): %s",
                len(failed), _scrub(_one_line(", ".join(failed), 200)),
            )


def _spawn_mark_read(messages: dict) -> None:
    """Fire-and-forget the mark-read so the dismiss response is not delayed.

    Dismiss (and especially 'Dismiss all' of N items) must stay snappy, so the
    Graph write runs as a background task AFTER the durable write. Errors are
    inspected + logged inside _mark_messages_read (never re-raised). Holds a strong
    ref so the task is not GC'd mid-flight. ``messages`` is a {message_id: subject}
    map (a single-item dict for one dismiss; coalesced/chunked ≤10 by the callee).
    """
    if not messages:
        return
    try:
        task = asyncio.ensure_future(_mark_messages_read(dict(messages)))
        _MARK_READ_TASKS.add(task)
        task.add_done_callback(_MARK_READ_TASKS.discard)
    except RuntimeError:
        # No running loop (e.g. a sync test context) -- skip the background mark.
        logger.debug("mark-read skipped: no running loop")


def _mark_read_map_for(item: dict) -> dict:
    """Build the {message_id: subject} mark-read map for one dismissed item.

    Uses the item's Graph messageId (conversationId is its alias) keyed to the
    subject; returns {} when no message id is resolvable (so we never fire a bogus
    mark-read with an empty id).
    """
    msg_id = str(item.get("messageId", "") or item.get("conversationId", "") or "").strip()
    if not msg_id:
        return {}
    subject = item.get("subject")
    return {msg_id: (str(subject) if subject else None)}


async def dismiss_item(request: web.Request) -> web.Response:
    """DELETE /api/email/queue/{item_id} -- soft-dismiss (preserve item).

    On a SUCCESSFUL dismiss the message is marked read in Outlook via Graph
    mark_email_read (best-effort, background) -- the user has consciously processed
    it. A dismiss that 409s never marks read (we only fire AFTER the durable write).
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    item["status"] = "dismissed"
    mark_read_map = _mark_read_map_for(item)

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    # Dismiss persisted -> mark the message read in Outlook (best-effort,
    # background). Only after the durable write, so a 409'd dismiss never marks
    # read. Approve/mute deliberately do NOT mark read -- dismiss is the one
    # "I've consciously processed this" signal.
    _spawn_mark_read(mark_read_map)

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id, "dismissed": True})
    return web.json_response({"ok": True, "id": item_id, "item": item, "etag": new_etag})


async def undismiss_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/undismiss -- restore a dismissed item."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") != "dismissed":
        raise web.HTTPConflict(reason="item is not dismissed")

    # Restore: if draft differs from generatedDraft -> edited, else needs-review
    draft = str(item.get("draft", ""))
    generated = str(item.get("generatedDraft", ""))
    item["status"] = "edited" if draft.strip() != generated.strip() else "needs-review"

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id})
    return web.json_response({"item": item, "etag": new_etag})


async def _delete_messages_outlook(messages: dict) -> dict:
    """Delete one or more messages in Outlook via Graph delete_email (soft-delete).

    ``messages`` is a {message_id: subject-or-None} map. Moves to Deleted Items
    (permanent=false) which is recoverable. Batched ≤10 ids/call like mark-read.
    Goes through the SHORT-LIVED spawn (never the always-on persistent session) so
    a user delete works even with that session torn down and never re-arms GRASP's
    quota-burning refresh timer.

    RETURNS a per-message outcome map ``{message_id: {success: bool, error: str}}``
    so the caller can flip an item to deleted ONLY on a confirmed success:true (the
    "verify effect, not caller" honesty rule). PROPAGATES r.get('error') instead of
    discarding it -- a failed delete records WHY (status + GRASP error code), and a
    quota/429 failure is logged scrubbed WITH the id. Never raises.
    """
    clean = {
        str(mid): (subj if subj is None else str(subj))
        for mid, subj in (messages or {}).items()
        if str(mid or "").strip()
    }
    outcomes: dict = {}
    if not clean:
        return outcomes

    ids = list(clean.keys())
    for start in range(0, len(ids), _MARK_READ_BATCH):
        chunk = ids[start:start + _MARK_READ_BATCH]
        batch = {mid: clean[mid] for mid in chunk}
        try:
            result = await _call_graph_write_tool_oneshot(
                "delete_email", {"emails": batch, "permanent": False}
            )
        except Exception as e:  # noqa: BLE001
            reason = _scrub(_one_line(str(e), 2000))
            logger.warning(
                "delete-email failed for %d message(s): %s", len(chunk), reason,
            )
            for mid in chunk:
                outcomes[mid] = {"success": False, "error": reason}
            continue

        results = result.get("results") if isinstance(result, dict) else None
        if not isinstance(results, list) or not results:
            reason = _scrub(_one_line(str(result), 2000)) or "no results returned"
            logger.warning(
                "delete-email returned no results for %d message(s) (response: %s)",
                len(chunk), reason,
            )
            for mid in chunk:
                outcomes[mid] = {"success": False, "error": reason}
            continue

        for r in results:
            if not isinstance(r, dict):
                continue
            mid = str(r.get("message_id", ""))
            if not mid:
                continue
            ok = bool(r.get("success"))
            # PROPAGATE the per-message error -- do not discard it. A failed delete
            # records the WHY (GRASP status + error code), not just the id.
            err = _scrub(_one_line(str(r.get("error") or ""), 2000)) if not ok else ""
            outcomes[mid] = {"success": ok, "error": err}
            if not ok:
                logger.warning(
                    "delete-email did NOT delete %s: %s", mid, err or "(no reason given)",
                )
        # An id present in the batch but absent from results -> treat as failed.
        for mid in chunk:
            outcomes.setdefault(mid, {"success": False, "error": "no per-message result"})

    return outcomes


async def delete_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/delete -- delete the email from Outlook.

    HONEST DELETE (verify effect, not caller): the item is flipped to status
    "deleted" / written / broadcast ONLY AFTER the Outlook move returns success:true
    for that message. The delete goes through a SHORT-LIVED spawn that reuses the
    cached GRASP token (no dependency on the always-on persistent session), so it
    works even while that session is correctly torn down.

    On failure (429/quota or any error) the item is LEFT in its prior actionable
    state, item.lastActionError records the scrubbed reason, and the response body
    carries ``deleted:false`` + ``error`` so the frontend never relies on the HTTP
    envelope alone (a 200 with the move queued is NOT proof the email was deleted).
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    delete_map = _mark_read_map_for(item)
    msg_id = next(iter(delete_map), None) if delete_map else None

    # Await the actual Outlook move BEFORE touching the item's status. No id to
    # delete -> treat as a failure (we can't confirm an effect we never requested).
    if msg_id is None:
        reason = "no Outlook message id resolvable for this item"
        item["lastActionError"] = {"action": "delete", "reason": reason}
    else:
        outcomes = await _delete_messages_outlook(delete_map)
        outcome = outcomes.get(msg_id) or {"success": False, "error": "no per-message result"}
        if outcome.get("success"):
            item.pop("lastActionError", None)
            item["status"] = "deleted"
        else:
            item["lastActionError"] = {
                "action": "delete",
                "reason": outcome.get("error") or "delete failed",
            }

    # Re-read fresh and re-locate by id so we write against the CURRENT etag and
    # never clobber a concurrent change (the body-recovery / scan worker may have
    # rewritten the file while we awaited the Outlook move).
    fresh_data, fresh_etag = await _load_async()
    fresh_item = next((it for it in fresh_data["items"] if it.get("id") == item_id), None)
    if fresh_item is None:
        # Item vanished mid-delete (e.g. dismissed elsewhere) -- nothing to persist.
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") == "deleted":
        fresh_item["status"] = "deleted"
        fresh_item.pop("lastActionError", None)
    else:
        fresh_item["lastActionError"] = item.get("lastActionError")

    try:
        new_etag = filestore.write_json(EMAIL_PATH, fresh_data, expected_etag or fresh_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    deleted = fresh_item.get("status") == "deleted"
    await ws.broadcast("email_changed", {"id": item_id, "deleted": deleted})

    if deleted:
        return web.json_response(
            {"ok": True, "id": item_id, "deleted": True, "etag": new_etag}
        )
    # The body carries deleted:false + the WHY so the frontend keeps the row.
    return web.json_response({
        "ok": False,
        "id": item_id,
        "deleted": False,
        "error": (fresh_item.get("lastActionError") or {}).get("reason") or "delete failed",
        "item": fresh_item,
        "etag": new_etag,
    })


async def regenerate_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/refresh -- regenerate the draft."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    result = await _run_email_agent("regenerate", {
        "id": item_id,
        "item": item,
        "prompt": _build_email_draft_prompt(item),
    })
    if not result.get("available"):
        return web.json_response(
            {
                "available": False,
                "reason": result.get("reason", "Email regenerate unavailable"),
                "item": item,
                "etag": current_etag,
            },
            status=502,
        )

    new_draft = _scrub(str(result.get("draft", "")))[:_DRAFT_CAP]
    item["draft"] = new_draft
    item["generatedDraft"] = new_draft
    # Refresh BOTH the summary and the ask from the SAME regenerate call (the
    # draft worker does the same). Each is set ONLY when the parsed value is a
    # non-empty string, so a regenerate that returns an empty summary/ask keeps
    # the pre-regenerate text rather than blanking the Thread Summary.
    thread_ctx = result.get("threadContext", "")
    if isinstance(thread_ctx, str) and thread_ctx.strip():
        item["threadContext"] = thread_ctx.strip()
    ask = result.get("threadAsk", "")
    if isinstance(ask, str) and ask.strip():
        item["threadAsk"] = ask.strip()
    item["status"] = "needs-review"

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id})
    return web.json_response({"available": True, "item": item, "etag": new_etag})


async def approve_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/approve -- approve and save draft.

    The system NEVER sends email directly. Approve calls email_draft directly
    via the aws-outlook-mcp write session (no claude subprocess) and marks the
    item as 'approved'. The user sends from their own email client.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") == "approved":
        raise web.HTTPConflict(reason="item already approved")

    final_draft = str(item.get("draft", "")).strip()

    # Call email_draft directly via MCP (no LLM subprocess — instant).
    # Response shape: {success:true, content:{draftId, draftChangeKey, recipients}}
    # or {success:false, error:{message:"BLOCKED: ..."}} for domain-blocked recipients.
    draft_saved = False
    draft_id = None
    if final_draft:
        # Resolve the reply-all To/CC. Prefer the user's persisted edit (the
        # editor flips recipientsEdited and stores final email-string lists),
        # else compute the reply-all default from the captured recipients. NEVER
        # sends: these only pre-fill the saved Outlook draft (D-056 #3).
        if item.get("recipientsEdited"):
            to_addrs = _sanitize_recipient_emails(item.get("toRecipients"))
            cc_addrs = _sanitize_recipient_emails(item.get("ccRecipients"))
            if not to_addrs:
                # An edit that cleared To still needs a recipient: fall back to
                # the sender (never an empty/fabricated draft recipient).
                to_addrs, _ = _reply_all_recipients(item)
        else:
            to_addrs, cc_addrs = _reply_all_recipients(item)
        if to_addrs:
            try:
                result = await _call_owa_write_tool("email_draft", {
                    "operation": "create",
                    "to": to_addrs,
                    "cc": cc_addrs,
                    "subject": f"Re: {item.get('subject', '')}",
                    # Real reply-all: the user's reply on top + the ENTIRE quoted
                    # thread below it (every turn, oldest→newest, each external
                    # field HTML-escaped). Pure builder, no extra MCP call.
                    "body": _build_reply_all_body(final_draft, item),
                })
                if isinstance(result, dict):
                    draft_saved = bool(result.get("success"))
                    content = result.get("content") or {}
                    if isinstance(content, dict):
                        draft_id = content.get("draftId")
                    if not draft_saved:
                        err = result.get("error") or {}
                        reason = err.get("message", "") if isinstance(err, dict) else str(err)
                        logger.warning("Email approve: draft not saved: %s", _scrub(reason))
            except Exception as e:  # noqa: BLE001
                logger.warning("Email approve: draft save failed: %s", _scrub(str(e)))
        else:
            # FAIL-LOUD (D-068, the headline SILENT-SUCCESS-APPROVE fix): the item has
            # a draft but the reply-all To resolves EMPTY (no senderEmail, no captured
            # recipients). The old code logged this, STILL flipped the item to
            # "approved", and returned 200/draftSaved:false — the frontend rendered a
            # quiet green "Copied!" though NO draft was saved. Instead, do NOT flip the
            # status and do NOT write: return a 422 the page surfaces as a RED banner,
            # DISTINCT from the 409 conflict shape (anti CONFLATE-NO-RECIPIENT-WITH-409
            # — the page must say "add a recipient and retry", not "Conflict...
            # Refreshing"). The item stays actionable so the user can add a recipient
            # and approve again. NEVER fabricate a recipient to force a save.
            logger.warning("Email approve: no recipient resolved — cannot save draft")
            return web.json_response(
                {"error": "no_recipient",
                 "message": ("No recipient address — draft not saved; "
                             "add a recipient and retry"),
                 "etag": current_etag},
                status=422,
            )

    # Topic memory (best-effort, logged)
    try:
        _append_email_topic_note(item.get("sender", ""), item, final_draft)
    except Exception as e:  # noqa: BLE001
        logger.warning("Email approve: topic-note append failed: %s", _scrub(str(e)))

    item["status"] = "approved"

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError:
        # Re-read once and retry: a concurrent write moved the etag. If the item was
        # already approved by that concurrent write, treat THAT as the durable state.
        data, current_etag = await _load_async()
        item = next((it for it in data["items"] if it.get("id") == item_id), None)
        if item and item.get("status") == "approved":
            new_etag = current_etag
        else:
            if item:
                item["status"] = "approved"
            try:
                new_etag = filestore.write_json(EMAIL_PATH, data, current_etag)
            except filestore.ConflictError as e:
                # A PERSISTENT conflict: our approve did NOT land. The Outlook draft
                # + topic note are best-effort side-effects, but the store write is
                # the source of truth — surface a 409 so the client re-reads, the
                # SAME shape dismiss_item/mute_item use. Never report 200 for a
                # non-persisted approve.
                current, current_etag = filestore.read_json(EMAIL_PATH)
                return web.json_response(
                    {"error": "conflict", "message": str(e),
                     "current": current, "etag": current_etag},
                    status=409,
                )

    _spawn_mark_read(_mark_read_map_for(item))

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id, "approved": True})
    return web.json_response({
        "available": True,
        "draftSaved": draft_saved,
        "draftId": draft_id,
        "item": item,
        "etag": new_etag,
    })


async def mute_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/mute -- mute conversation + dismiss."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    # Derive the mute key from the Graph messageId (conversationId is its alias,
    # so the key matches what _scan_once checks when suppressing a muted message).
    mute_key = _mute_key_for(item) or item_id

    # Record the mute and soft-dismiss. Mute deliberately does NOT mark read --
    # dismiss is the one "I've consciously processed this" signal.
    data.setdefault("mutedThreads", {})[mute_key] = int(time.time())
    item["status"] = "dismissed"

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id, "muted": True})
    return web.json_response(
        {"ok": True, "id": item_id, "muteKey": mute_key, "item": item, "etag": new_etag}
    )


async def unmute_thread(request: web.Request) -> web.Response:
    """DELETE /api/email/muted/{mute_key} -- remove a mute."""
    mute_key = request.match_info["mute_key"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    muted = data.get("mutedThreads") or {}
    if mute_key not in muted:
        raise web.HTTPNotFound(reason=f"mute key {mute_key} not found")

    del muted[mute_key]
    data["mutedThreads"] = muted

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(EMAIL_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"muteKey": mute_key, "unmuted": True})
    return web.json_response({"ok": True, "muteKey": mute_key, "etag": new_etag})


async def refresh_queue(request: web.Request) -> web.Response:
    """POST /api/email/queue/refresh -- trigger a manual email scan.

    Mirrors the Slack refresh_queue contract: if a scan is already in progress,
    return 200 with {scanning: false, reason: ...}. Otherwise wake the scan
    worker and return 202 with {scanning: true, etag: ...}.
    """
    _, etag = await _load_async()

    if _get_scan_lock().locked():
        return web.json_response(
            {"scanning": False, "reason": "A scan is already in progress."},
            status=200,
        )

    _get_scan_event().set()
    return web.json_response({"scanning": True, "etag": etag}, status=202)


async def get_muted(request: web.Request) -> web.Response:
    """GET /api/email/muted -- list muted threads for settings UI."""
    data, etag = await _load_async()
    return web.json_response({"muted": data.get("mutedThreads", {}), "etag": etag})


async def get_style(request: web.Request) -> web.Response:
    """GET /api/email/style -- read the learned style store."""
    content, etag = filestore.read_text(_style_path())
    return web.json_response({"content": content, "etag": etag})


async def put_style(request: web.Request) -> web.Response:
    """PUT /api/email/style -- write the learned style store."""
    body = await read_json_body(request)
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(reason="content field required")
    expected_etag = body.get("etag")
    content = _scrub(str(content))

    try:
        new_etag = filestore.write_text(_style_path(), content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(_style_path())
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )
    return web.json_response({"ok": True, "content": content, "etag": new_etag})


async def get_topics(request: web.Request) -> web.Response:
    """GET /api/email/topics/{contact} -- read a contact's topic memory.

    Returns empty content (NOT 404) for a never-seen contact so the UI can render
    a blank editor. The {contact} segment is traversal-checked BEFORE slugifying.
    """
    contact = request.match_info["contact"]
    _validate_contact(contact)
    slug = _slugify_contact(contact)
    if not slug:
        raise web.HTTPBadRequest(reason="invalid contact")
    path = _email_topics_dir() / f"{slug}.md"
    content, etag = filestore.read_text(path)
    return web.json_response(
        {"contact": contact, "slug": slug, "content": content, "etag": etag, "path": str(path)}
    )


async def put_topics(request: web.Request) -> web.Response:
    """PUT /api/email/topics/{contact} -- write a contact's topic memory.

    Etag-guarded: a stale write returns the 409-with-current+etag shape (matches
    put_style). The {contact} segment is traversal-checked BEFORE slugifying.
    """
    contact = request.match_info["contact"]
    _validate_contact(contact)
    slug = _slugify_contact(contact)
    if not slug:
        raise web.HTTPBadRequest(reason="invalid contact")
    body = await read_json_body(request)
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(reason="content field required")
    expected_etag = body.get("etag")
    content = _scrub(str(content))
    path = _email_topics_dir() / f"{slug}.md"

    try:
        new_etag = filestore.write_text(path, content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(path)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )
    return web.json_response(
        {"ok": True, "contact": contact, "slug": slug, "content": content, "etag": new_etag}
    )


async def polish_text(request: web.Request) -> web.Response:
    """POST /api/email/polish -- a stateless fluency rewrite of the draft text.

    Mirrors slack.polish_text: a pure text transform on the request-body text. It
    does NOT load or write email_threads.json (no etag, no status change), does NOT
    read email, and NEVER sends — so it can run on text the user is mid-edit. The
    polished text is returned for the UI to drop back as an unsaved edit; persisting
    it goes through the existing Save / Approve flow.

    FAIL-LOUD: an empty/non-string `text` is a 400; an unavailable seam is a 502.
    Never fabricates a polished result.
    """
    body = await read_json_body(request) if request.can_read_body else {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise web.HTTPBadRequest(reason="polish requires a non-empty 'text'")

    result = await _run_email_agent("polish", {"text": text[:_DRAFT_CAP]})
    if not result.get("available"):
        # Honest failure — never fabricate a polished draft.
        return web.json_response(
            {"available": False, "reason": result.get("reason", "Email polish unavailable")},
            status=502,
        )

    polished = _scrub(str(result.get("draft", "")))[:_DRAFT_CAP]
    return web.json_response({"available": True, "draft": polished})


# ── Registration ────────────────────────────────────────────────────────────

def register(app: web.Application):
    """Register all /api/email/* routes."""
    app.router.add_get("/api/email/health", get_health)
    app.router.add_get("/api/email/queue", get_queue)
    # Lightweight count for the NavBar bubble. MUST be registered BEFORE the
    # parameterized /api/email/queue/{item_id} routes so "count" is not captured
    # as an item_id.
    app.router.add_get("/api/email/queue/count", get_queue_count)
    app.router.add_put("/api/email/config", put_config)
    # Manual scan trigger -- MUST be registered BEFORE the parameterized
    # /api/email/queue/{item_id} routes so "refresh" is not captured as item_id.
    app.router.add_post("/api/email/queue/refresh", refresh_queue)
    app.router.add_put("/api/email/queue/{item_id}", save_draft)
    app.router.add_delete("/api/email/queue/{item_id}", dismiss_item)
    app.router.add_post("/api/email/queue/{item_id}/undismiss", undismiss_item)
    app.router.add_post("/api/email/queue/{item_id}/delete", delete_item)
    app.router.add_post("/api/email/queue/{item_id}/refresh", regenerate_item)
    app.router.add_post("/api/email/queue/{item_id}/approve", approve_item)
    app.router.add_post("/api/email/queue/{item_id}/mute", mute_item)
    # Muted-threads management surface: list + unmute.
    app.router.add_get("/api/email/muted", get_muted)
    app.router.add_delete("/api/email/muted/{mute_key}", unmute_thread)
    app.router.add_get("/api/email/style", get_style)
    app.router.add_put("/api/email/style", put_style)
    # Per-contact topic memory (read/write the running summary for one contact).
    app.router.add_get("/api/email/topics/{contact}", get_topics)
    app.router.add_put("/api/email/topics/{contact}", put_topics)
    # Stateless per-edit fluency pass (MCP-free, never touches the sidecar).
    app.router.add_post("/api/email/polish", polish_text)

    # Worker lifecycle
    app.on_startup.append(_start_scan_worker)
    app.on_cleanup.append(_stop_scan_worker)
    app.on_startup.append(_start_classify_worker)
    app.on_cleanup.append(_stop_classify_worker)
    app.on_startup.append(_start_draft_worker)
    app.on_cleanup.append(_stop_draft_worker)
    # Persistent MCP session cleanup (AFTER workers so no new calls arrive)
    app.on_cleanup.append(_cleanup_persistent_mcp)
