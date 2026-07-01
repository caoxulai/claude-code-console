"""Assistant → Slack: draft-queue + style/topic memory + MCP delegation seam.

ARCHITECTURE DECISION (load-bearing — do not "fix" by adding a Slack client):
  The aiohttp server MUST NOT call ``mcp__slack-mcp__*`` tools or any Slack SDK
  directly. MCP is a *harness* capability (it's exposed to a Claude session, not
  importable as a Python library), so there is no in-process way for this server
  to reach the user's Slack. ALL Slack I/O — ``get_unreads`` / ``list_dms`` /
  ``mentions``, draft generation, and the outbound *send* — is delegated to a
  one-shot ``claude --print`` subprocess driven via the SessionManager pattern
  (see ``server/session_manager.py``): the backend spawns ``claude`` with a cwd,
  hands it a prompt instructing it to call the Slack MCP tools and return a
  STRICT JSON payload, then parses that payload.

  That entire delegation lives behind ONE clearly-marked seam:
  ``async def _run_slack_agent(action, payload) -> dict`` is the *only* function
  that ever spawns/awaits the subprocess (it delegates to ``_drive_agent``, the
  sole ``create_subprocess_exec`` site). The seam actions are
  ``'list'`` | ``'regenerate'`` | ``'draft'`` | ``'send'``. D-013 made this a
  SINGLE-ENGINE, cron-only scanner: the durable warm-up cron (the tmux-hosted
  ``claude`` REPL) is the SOLE producer that scans Slack and writes
  ``slack_threads.json``, and D-013's invariant was "the server NEVER spawns a
  subprocess on any interval or event". D-015 RELAXES that ONE clause for a
  read-only, single-item-ENRICHING worker only: the cron now writes draft-free
  SKELETON items (``status:'needs-draft'``) + flags existing items that grew new
  messages (``needsRedraft:true``), and an in-process ``asyncio`` ``_draft_worker``
  drafts those items in PARALLEL (capped by ``asyncio.Semaphore``) via the
  read-only ``'draft'`` action. The worker is NOT a second scanner — it spawns no
  scan, mints no new items, and only fills in drafts on items the cron already
  wrote; the SEND path stays exclusively user-triggered — so D-013's single-scan
  -engine spirit (no second producer racing the cron on the whole file) is intact.
    * ``'list'`` is a FAST, draft-free enumeration of unread DMs/@mentions
      (read-only Slack tools, a SHORTER ``_LIST_TIMEOUT_S`` budget). The
      in-process poller that USED to drive it on an interval is RETIRED (D-013);
      the seam itself is kept as a harmless read-only capability (no scan loop
      reaches it any more), so a caller/test may still invoke it explicitly.
    * ``'regenerate'`` drafts ONE item (the single-item, user-triggered redraft
      path behind POST ``/queue/{id}/refresh``), flipping it to ``needs-review``.
      MCP-FREE (D-022): its conversation history is pre-fetched on the PERSISTENT
      session and rendered into the prompt, so the subprocess calls NO MCP tool.
    * ``'draft'`` (D-015) drafts ONE skeleton/re-draft item for the in-process
      ``_draft_worker``. MCP-FREE (D-022): the worker pre-fetches the item's recent
      history on the PERSISTENT warm-auth session (``_fetch_history_for``) and
      renders it into the prompt, so the subprocess calls NO MCP tool — it gets NO
      ``--allowedTools`` and an EMPTY ``--mcp-config`` (no servers) under
      ``--strict-mcp-config`` so it does NOT inherit the user's global slack-mcp
      entry and never cold-starts one. It just returns
      ``{draft, generatedDraft, threadContext}`` as strict JSON. If the channel was
      unreadable it drafts from the snippet alone; it NEVER cold-starts slack-mcp.
    * ``'send'`` sends EXACTLY ONE approved message (the only write tool) — the ONE
      intentional slack-mcp cold-start, since the read-only persistent session
      refuses ``post_message`` (D-022).
  It returns ``{"available": True, ...}``
  on a parsed STRICT-JSON reply, and FAILS LOUD with
  ``{"available": False, "reason": <scrubbed>}`` on any spawn failure, non-zero
  exit, bounded-timeout, auth error, or unparseable/empty reply. We NEVER
  fabricate message data or a send-success — an item we can't harvest surfaces as
  an explicit not-available state (mirroring crons.py's "result not captured"),
  per feedback_principle_fail_loud_on_missing_input. Every call is bounded by a
  timeout and always reaps the child (terminate→kill) so no orphan survives.
  Tests monkeypatch ``_run_slack_agent`` (or the module's
  ``asyncio.create_subprocess_exec``) to exercise the contract without spawning a
  real ``claude``.

  A SEPARATE, non-spawning ``_probe_readiness()`` (exposed at
  ``GET /api/slack/health``) reports whether the seam *can* work — ``claude`` on
  PATH + a best-effort slack-mcp config check — WITHOUT any message I/O or send,
  so the page can surface an honest "not wired: <reason>" state cheaply.

DETERMINISTIC SCAN (D-018 — the SCAN no longer needs an LLM):
  The cron's LLM-driven scan (call ``list_dms`` + ``get_unreads``, reason about
  channelId dedupe, write ``needs-draft`` skeletons) is deterministic work that
  needs no intelligence — and the LLM turn cost 30-120s and flailed
  nondeterministically. So scanning moves into an in-process ``_scan_worker`` that
  drives the Slack MCP server DIRECTLY over stdio via the official ``mcp`` Python
  SDK (``call_read_tool`` → ``stdio_client`` → ``ClientSession``), with NO
  ``claude`` subprocess and NO LLM in the scan loop. Unlike the ``claude --print``
  seam (gated by ``--allowedTools``), a raw MCP client CAN call any tool the server
  exposes, so the send-safety boundary becomes a HARDCODED module-level
  ``_SLACK_READ_ONLY_TOOLS`` frozenset: ``call_read_tool`` REFUSES by name any tool
  not in it BEFORE the SDK ever touches the server — the worker is structurally
  incapable of calling ``post_message`` or any write tool. The launch
  command/args/env are derived from the SAME ``_find_slack_mcp_entry()`` /
  ``_slack_mcp_config()`` the ``claude`` subprocess uses (same ambient Midway auth,
  no new auth wiring). The DRAFTING step stays LLM-based and UNCHANGED (the
  ``_draft_worker`` above): only the SCAN becomes deterministic Python.

STORE: a JSON sidecar ``slack_threads.json`` resolved via
  ``_resolve_slack_path()`` (``CLAUDE_WEB_SLACK_PATH`` env override, defaulting to
  ``<cwd>/.claude/slack_threads.json`` — same dir convention as crons.TASKS_PATH).
  Kept as a module attribute (``SLACK_PATH``) so tests can monkeypatch it.

  Item shape:
    {id, sender, channel, channelType('dm'|'group_dm'|'mention'), snippet,
     threadContext, draft, status('needs-review'|'edited'|'sent'|'dismissed'),
     ts, generatedDraft?, finalText?, sentAt?, channelId?, userId?}
  Dismiss is a SOFT state flip (status='dismissed') that PRESERVES the item so it
  can be restored via the undismiss route — it is never a hard delete (D-014).

STYLE + TOPIC MEMORY (human-readable, append-and-reconcile — never silently
  overwritten, mirroring the agent-context layer): the learned writing style lives
  in ``slack_style.md`` and per-contact running summaries in
  ``slack_topics/<contact>.md``. On approve-after-edit we distill the
  (generated-draft → user-final) signal into a dated style note appended to the
  style file. Both stores are injected into the draft-generation prompt that
  ``_run_slack_agent('regenerate', ...)`` builds — see
  ``build_draft_prompt`` (unit-testable even with the subprocess stubbed).

SECURITY: credentials are scrubbed defensively before anything is persisted or
  returned (``_looks_secret`` / ``_SECRET_MARKERS``, same approach as crons.py).
  Tokens / cookies / ~/.midway contents are never written to the sidecar or any
  store, and never echoed in a response.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path

import secrets

import anyio
from aiohttp import web

from server import filestore
from server.config import cfg
from server.routes import read_json_body
from server.routes.workers import register_worker, mark_worker_run
from server.session_manager import is_auth_error

# Direct-Python MCP client (D-018): the deterministic scan worker drives the
# Slack MCP server over stdio via the official `mcp` SDK — NO `claude --print`
# subprocess, NO LLM in the scan loop. These resolve to the PyPI `mcp` package
# (NOT server/routes/mcp.py); the investigation proved mcp 1.28.0 drove the real
# @amzn/slack-mcp-server v4.0.2 over stdio with ambient Midway auth.
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


def _resolve_slack_path() -> Path:
    """Resolve the slack_threads.json sidecar.

    Precedence: cfg.slack_path (sourced from CLAUDE_WEB_SLACK_PATH env or
    data_dir default in server/config.py).
    """
    return cfg.slack_path


# Resolved once at import; kept as a module attribute so it stays inspectable and
# overridable (tests monkeypatch it).
SLACK_PATH = _resolve_slack_path()

# The owner's Slack username — used for self-exclusion (skip DMs where *I* sent
# the last message) and style-sample filtering. Derived from cfg.my_username
# (which resolves CLAUDE_WEB_MY_USERNAME or the local part of CLAUDE_WEB_MY_EMAIL).
_MY_USERNAME: str = cfg.my_username


def _style_path() -> Path:
    """Human-readable learned-style store, sibling to the sidecar."""
    return SLACK_PATH.parent / "slack_style.md"


def _topics_dir() -> Path:
    """Per-contact topic-summary dir, sibling to the sidecar."""
    return SLACK_PATH.parent / "slack_topics"


# Lines/text that may carry credentials or cookie material — never persisted or
# surfaced. Mirrors crons._SECRET_MARKERS.
_SECRET_MARKERS = ("~/.midway", ".midway", "cookie", "mwinit", "aws_secret", "authorization:")

# 'dismissed' is a SOFT state (D-014): the item is preserved in the sidecar with
# its draft/generatedDraft intact so Undo can restore it — it is NOT deleted.
# 'needs-classify' (D-025) is the FIRST state a freshly-scanned skeleton lands in:
# the classify worker batches these through ONE LLM call, RECORDS the resulting
# label in the separate ``classification`` field (needs-reply / fyi / actionable),
# and routes EVERY item to 'needs-draft' so it gets thread context + a draft
# regardless of label (D-027). 'fyi' is therefore NO LONGER a status — it is a
# classification on a normally-drafted item, so an FYI message still carries full
# context and a draft (even though you will usually dismiss it).
_VALID_STATUS = {
    "needs-classify", "needs-draft", "needs-review", "edited", "sent",
    "dismissed",
}

# The advisory triage labels the classify worker records in the item's
# ``classification`` field (D-027). PURELY a review-grouping/priority hint — it
# never gates whether an item is drafted (every classified item is). 'fyi' means
# "no reply is likely needed", but the draft is still generated so you can send
# with one click if you decide otherwise.
_VALID_CLASSIFICATIONS = {"needs-reply", "fyi", "actionable"}
_SNIPPET_CAP = 2000
_DRAFT_CAP = 8000
_CONTEXT_CAP = 16000


def _looks_secret(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


def _scrub(text: str) -> str:
    """Drop any line that looks like it carries a credential/cookie/token.

    Defensive: external Slack content (or a stubbed agent payload) could echo
    secret-looking material; we never want it landing in a sidecar/store/response.
    Line-granular so a single tainted line doesn't blank an otherwise useful body.
    """
    if not text:
        return ""
    kept = [ln for ln in text.splitlines() if not _looks_secret(ln)]
    return "\n".join(kept)


def _validate_contact(contact: str) -> None:
    """Reject contact names with path-traversal chars (they index a topic file).

    Mirrors agents._validate_agent_name — ``contact`` comes from the URL and is
    joined as ``slack_topics/<contact>.md``.
    """
    if not contact or ".." in contact or "/" in contact or "\\" in contact:
        raise web.HTTPBadRequest(reason="invalid contact")


def register(app: web.Application):
    # Read-only readiness probe: reports whether the delegation seam can work
    # (claude on PATH + Slack MCP configured) WITHOUT spawning a subprocess or
    # sending anything. The page surfaces this so an unavailable seam shows WHY.
    app.router.add_get("/api/slack/health", get_health)
    app.router.add_get("/api/slack/queue", get_queue)
    # Lightweight count-only feed for the NavBar bubble (C1): returns just the
    # actionable count so the badge never fetches the full (large) queue payload.
    app.router.add_get("/api/slack/queue/count", get_queue_count)
    # PUT the top-level config — currently ONLY the on/off `paused` toggle (D-025).
    # Etag-guarded like the other mutators; broadcasts 'slack_changed' so an open
    # page can show/hide its "Paused" badge live.
    app.router.add_put("/api/slack/config", put_config)
    app.router.add_post("/api/slack/queue/refresh", refresh_queue)
    app.router.add_put("/api/slack/queue/{item_id}", save_draft)
    # SOFT-dismiss (D-014): DELETE flips status to 'dismissed' (item PRESERVED so
    # Undo can restore it) and broadcasts 'slack_changed' (the item still exists).
    app.router.add_delete("/api/slack/queue/{item_id}", dismiss_item)
    # Undo a dismiss: restore a 'dismissed' item back to needs-review/edited.
    app.router.add_post("/api/slack/queue/{item_id}/undismiss", undismiss_item)
    # Mute a thread (D-025): dismiss the item AND remember its mute key so future
    # scans skip the conversation until the mute is removed or it ages out (30d).
    app.router.add_post("/api/slack/queue/{item_id}/mute", mute_item)
    # Muted-threads management surface (for a settings UI): list / unmute.
    app.router.add_get("/api/slack/muted", get_muted)
    app.router.add_delete("/api/slack/muted/{mute_key}", unmute_thread)
    # Per-item regenerate: re-run draft generation for ONE item, pulling in the
    # accumulated style + topic memory. Never sends.
    app.router.add_post("/api/slack/queue/{item_id}/refresh", regenerate_item)
    # Polish the CURRENT draft text for fluency while keeping the user's own voice
    # and language. STATELESS + MCP-FREE: it takes the text in the request body,
    # never reads Slack, never loads/writes the queue, and never sends. The polished
    # text is returned for the UI to drop back into the editable box as an unsaved
    # edit (so the existing diff / Save / Approve flow handles it unchanged).
    app.router.add_post("/api/slack/polish", polish_text)
    # The ONLY send path: per-item, approval-gated. There is deliberately no
    # bulk/auto-send endpoint, and neither refresh nor save ever sends.
    app.router.add_post("/api/slack/queue/{item_id}/approve", approve_item)
    app.router.add_get("/api/slack/style", get_style)
    app.router.add_put("/api/slack/style", put_style)
    app.router.add_get("/api/slack/topics/{contact}", get_topics)
    app.router.add_put("/api/slack/topics/{contact}", put_topics)

    # ── Read-only file-watcher lifecycle (D-013) ──────────────────────────────
    # SINGLE-ENGINE cron-only: the durable warm-up cron writes slack_threads.json
    # from INSIDE its tmux REPL, so the server never sees that write and nothing
    # would otherwise broadcast it to open pages. A single in-process asyncio.Task
    # STAT/etag-reads the sidecar on a short interval and broadcasts 'slack_changed'
    # when the etag moves — a pure local-file read that NEVER spawns a subprocess
    # and NEVER scans Slack. This REPLACES the retired poller's broadcast-after-write
    # (the poller, its 'scan now' Event, and its 'list' scan loop are all gone). It
    # mirrors the deleted poller's startup/cleanup lifecycle so the task is always
    # cancelled+awaited at shutdown (the shape that kept the suite warning-free).
    app.on_startup.append(_start_watcher)
    app.on_cleanup.append(_stop_watcher)

    # ── Draft-worker lifecycle (D-015) ────────────────────────────────────────
    # The cron writes draft-free SKELETON items (status 'needs-draft') and flags
    # existing items that grew new messages (needsRedraft). An in-process asyncio
    # task drafts those items IN PARALLEL (capped by a Semaphore) via the read-only
    # 'draft' seam — moving the slow per-conversation history-fetch+drafting OFF the
    # cron's serial turn. It mirrors the watcher's startup/cleanup lifecycle EXACTLY
    # (idempotent create + cancel-and-await teardown) so the task is reaped cleanly
    # at shutdown with no 'Task was destroyed' warnings. It is READ-ONLY (never
    # sends) and gates each cycle on _probe_readiness() so the test suite — which
    # fires on_startup under aiohttp_client(app) in EVERY test — never spawns a real
    # `claude` (the worker's first action is an interval sleep, then a readiness
    # gate). Self-contained here: app.py is untouched.
    app.on_startup.append(_start_draft_worker)
    app.on_cleanup.append(_stop_draft_worker)

    # ── Classify-worker lifecycle (D-025) ─────────────────────────────────────
    # The scan now writes fresh skeletons in status 'needs-classify' rather than
    # 'needs-draft'. An in-process asyncio task batches those skeletons through ONE
    # read-free `claude --print` LLM call (the MCP-FREE 'classify' action) and routes
    # each: 'needs-reply'/'actionable' → 'needs-draft' (the unchanged draft worker
    # picks them up), 'fyi' → 'fyi' (terminal, no draft generated). It mirrors the
    # draft worker's lifecycle EXACTLY (idempotent create + cancel-and-await teardown,
    # sleep-first so it is inert on startup, gated on _probe_readiness) so the test
    # suite never spawns a real `claude`. Registered between the draft-worker and
    # scan-worker hooks. Self-contained here: app.py is untouched.
    app.on_startup.append(_start_classify_worker)
    app.on_cleanup.append(_stop_classify_worker)

    # ── Scan-worker lifecycle (D-018) ─────────────────────────────────────────
    # The DETERMINISTIC in-process scan worker that replaces the cron's LLM scan.
    # It drives the Slack MCP server DIRECTLY over stdio (via the `mcp` SDK behind
    # call_read_tool — NO `claude` subprocess, NO LLM), calls list_dms +
    # get_unreads, dedupes by channelId in pure Python, and writes needs-draft
    # SKELETONS the unchanged _draft_worker then drafts. It mirrors the draft
    # worker's lifecycle EXACTLY (idempotent create + cancel-and-await teardown) so
    # the task is reaped cleanly at shutdown. Its first action is an interval sleep
    # and each cycle gates on _probe_readiness() — so the test suite, which fires
    # on_startup under aiohttp_client(app) in EVERY test, never spawns a real MCP
    # server. Self-contained here: app.py is untouched.
    app.on_startup.append(_start_scan_worker)
    app.on_cleanup.append(_stop_scan_worker)

    # ── Persistent MCP session cleanup ───────────────────────────────────────
    # Appended AFTER _stop_scan_worker so the ordering at shutdown is:
    #   1. Stop the scan-worker task (cancels the consumer of call_read_tool).
    #   2. Teardown the persistent MCP session (terminates the subprocess).
    # This ensures no new calls arrive during teardown and no orphan slack-mcp
    # process is left. Idempotent: if the session was never connected (lazy-init,
    # never used in test suite) or already disconnected, _disconnect_mcp is a no-op.
    app.on_cleanup.append(_cleanup_persistent_mcp)


def _normalize_loaded(data: dict) -> dict:
    """Normalize a freshly-read sidecar dict in place and return it.

    Shared by the sync ``_load`` and async ``_load_async`` so the two readers can
    never drift: both ensure ``items`` exists and apply the D-027 in-memory fyi
    migration identically.
    """
    if not data:
        data = {"items": []}
    if "items" not in data:
        data["items"] = []
    # MIGRATION (D-027): 'fyi' is no longer a terminal STATUS — it is a
    # ``classification`` label on a normally-drafted item. Convert any legacy
    # fyi-status item written by the old pipeline so it rejoins drafting: record
    # the classification and reset it to needs-draft (a draft will be generated;
    # it was never sent). In-memory only — persisted by the next write on any
    # normal mutation, so we never write here (a pure read must not mutate disk).
    for it in data["items"]:
        if it.get("status") == "fyi":
            it["classification"] = "fyi"
            it["status"] = "needs-draft"
    return data


def _load() -> tuple[dict, str | None]:
    """Synchronous sidecar read — for BACKGROUND tasks (watcher/workers) only.

    Request-path handlers use ``_load_async`` so the blocking read never stalls
    the event loop; this sync variant stays for the off-request-path callers.
    """
    data, etag = filestore.read_json(SLACK_PATH)
    return _normalize_loaded(data), etag


async def _load_async() -> tuple[dict, str | None]:
    """Async sibling of ``_load`` — offloads the blocking read to a thread (B10).

    Returns the same ``(data, etag)`` tuple with identical normalization (via
    ``_normalize_loaded``). Used by REQUEST-PATH handlers so a multi-hundred-KB
    sidecar read doesn't block the event loop. Background tasks keep ``_load``.
    """
    data, etag = await filestore.async_read_json(SLACK_PATH)
    return _normalize_loaded(data), etag


# ── MCP delegation seam ──────────────────────────────────────────────────────

# Bounded wall-clock budget for ONE seam call. The subprocess calls Slack MCP
# read/send tools and drafts replies — generous, but bounded so a hung `claude`
# can never block the request (or leave an orphan) forever.
_AGENT_TIMEOUT_S = 120

# Dedicated budget for the draft-free 'list' enumeration. Listing only walks
# unread DMs/@mentions (no per-item drafting), so it stays shorter than the
# send/regenerate budget (_AGENT_TIMEOUT_S). NOTE (D-013): nothing in this module
# DRIVES 'list' on an interval/event any more — the in-process poller is retired
# and the cron is the sole scanner. The seam (prompt/parse/allowlist + this
# budget) is kept as a harmless read-only capability a caller/test may invoke.
_LIST_TIMEOUT_S = 90


def _timeout_for(action: str) -> float:
    """The bounded wall-clock budget for one seam call, by action."""
    return _LIST_TIMEOUT_S if action == "list" else _AGENT_TIMEOUT_S


# How many most-recent unread conversations one explicit 'list' seam call may
# walk. The dominant cost is the model walking unreads, so we BOUND it in the
# prompt. (The cron does its own scanning; this cap only applies to a direct
# read-only 'list' seam invocation, which no interval/event triggers.)
SLACK_LIST_CAP = 25

# How often the read-only file-watcher STAT/etag-reads slack_threads.json to
# detect a cron write and broadcast 'slack_changed'. Short so an open page
# repaints promptly; a pure local-file read, never a Slack scan or subprocess.
SLACK_WATCH_INTERVAL_S = 5

# ── Draft-worker tuning (D-015) ──────────────────────────────────────────────
# How often the in-process draft-worker polls slack_threads.json for items that
# need a draft (status 'needs-draft', or 'needs-review' + needsRedraft). Short
# enough that a cron-written skeleton gets a draft within a few seconds, long
# enough that an idle queue costs ~nothing (a single local-file read per cycle).
SLACK_DRAFT_POLL_INTERVAL_S = 7

# Max simultaneous draft `claude --print` subprocesses the worker may run. Each
# draft fetches history3d + drafts in the user's voice; capping the fan-out keeps
# a burst of new conversations from spawning an unbounded number of processes.
SLACK_DRAFT_CONCURRENCY = 3

# ── Classify-worker tuning (D-025) ───────────────────────────────────────────
# How often the in-process classify-worker polls slack_threads.json for skeletons
# in status 'needs-classify'. Short so a freshly-scanned skeleton is classified
# within a few seconds (then the draft worker picks up the needs-draft ones).
SLACK_CLASSIFY_POLL_INTERVAL_S = 6

# How many needs-classify items one classify `claude --print` subprocess handles
# in a single batch. Classification is cheap per item (no tool calls, no history
# fetch — it reasons over the snippet already in the skeleton), so batching keeps
# the number of subprocess spawns small even with a burst of new conversations.
SLACK_CLASSIFY_BATCH = 10

# How many history3d messages a single draft may carry, and the per-message text
# cap. The cron used to fetch history3d inline; D-015 moves it into the draft
# subprocess, which returns an ordered (oldest→newest) array we cap defensively
# here too (the prompt also instructs the cap) so a runaway reply can't bloat the
# sidecar.
_HISTORY3D_CAP = 30
_HISTORY3D_TEXT_CAP = 2000

# ── Deterministic scan-worker tuning (D-018) ─────────────────────────────────
# How often the in-process scan worker drives the Slack MCP server (spawn-per-scan
# via the SDK's stdio_client context manager — ~2.5s cold per the investigation).
# Comfortably bounds the per-scan cost while keeping the queue fresh; the worker
# tracks its own last-scan timestamp so the activity window doesn't depend on the
# (now-retired) cron's lastFiredAt.
SLACK_SCAN_INTERVAL_S = 120  # 2 minutes — persistent session eliminates SAML auth cost

# Activity window fallback: when the worker has no prior-scan timestamp yet (first
# cycle after startup), a DM/group-DM counts as a scan candidate if its
# lastActivity is within this many ms of now. After the first cycle the worker
# uses its own last-scan timestamp (minus a small overlap) instead.
_SCAN_WINDOW_MS = 24 * 60 * 60 * 1000  # 24h on first scan (no prior ts); catches any backlog

# Small overlap subtracted from the worker's tracked last-scan timestamp so a
# conversation that became active right around a scan boundary isn't missed.
_SCAN_OVERLAP_MS = 60 * 1000

# How many DMs/group DMs list_dms is asked to return per scan.
_SCAN_LIST_DMS_LIMIT = 30

# ── Thread muting (D-025) ─────────────────────────────────────────────────────
# A muted thread's scan candidates are dropped before the merge. Each mutedThreads
# entry is {<mute_key>: <unix-seconds-when-muted>}; entries older than this TTL are
# pruned on every scan cycle so an old mute can't silently suppress a conversation
# forever (the user can re-mute if it's still noise).
_MUTE_TTL_S = 30 * 24 * 60 * 60  # 30 days

# Bot senders that never need a human reply — skip during scan.
_BOT_SENDERS = frozenset({
    "slackbot",
    "amazon meetings",
    "crux",
    "synerq ai",
    "andes workbench welcome message",
    "private sdm channel welcome",
    "opus apps approval process",
    "asana",
    *([f"meshclaw-{_MY_USERNAME}"] if _MY_USERNAME else []),
})

# Module-level tracker for the worker's last successful scan (epoch ms). None
# until the first cycle completes; kept as a module attribute so it survives across
# cycles and stays inspectable. Reset by tests that exercise the window directly.
# This drives the activity-WINDOW math (_scan_window_start_ms). Its persisted
# mirror is the top-level ``lastScanAt`` key written into slack_threads.json by
# _scan_once (so the "Updated …" freshness label survives a server restart).
_LAST_SCAN_TS_MS: int | None = None

# ── Shared scan guard + manual-refresh wake Event (D-023) ─────────────────────
# A SINGLE shared guard gives mutual exclusion between the periodic worker scan
# and a manual Refresh: only ONE scan runs at a time, and a new trigger that finds
# a scan in progress is dismissed (the worker tick skips; manual Refresh reports
# "already in progress"). The Event lets a manual Refresh WAKE the worker's
# interval sleep so a scan starts promptly instead of waiting the full interval.
# Both are lazily created (asyncio primitives cannot be built at import time — no
# running event loop yet), mirroring the _get_mcp_lock()/_mcp_connect_lock pattern.
_scan_in_progress_lock: asyncio.Lock | None = None
_scan_wake_event: asyncio.Event | None = None

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ COLD-START INVARIANT (D-022 — load-bearing, do not regress):              ║
# ║   The ONLY code path that cold-starts a slack-mcp process is              ║
# ║   _connect_mcp() — the single PERSISTENT read-only session, used by       ║
# ║   call_read_tool / _scan_read / _fetch_history_for / _mark_channel_read.  ║
# ║   One SAML auth on first use, then zero until reconnect — which is what    ║
# ║   eliminates the per-cold-spawn "r5 status: 429" SAML throttling.         ║
# ║                                                                            ║
# ║   The 'draft' and 'regenerate' actions NEVER set needs_mcp=True: they      ║
# ║   pre-fetch any conversation history on THIS persistent session (via       ║
# ║   _fetch_history_for) and render it into the prompt, so their             ║
# ║   `claude --print` subprocess gets NO --allowedTools and an EMPTY          ║
# ║   --mcp-config under --strict-mcp-config (so it does NOT inherit the        ║
# ║   global slack-mcp entry) and never boots its own slack-mcp.               ║
# ║                                                                            ║
# ║   The 'send' action is the ONE intentional exception: approve_item spawns  ║
# ║   a `claude --print --mcp-config` per approved send because this           ║
# ║   read-only session refuses the post_message write tool. Sends are rare    ║
# ║   and user-gated, so that occasional cold auth is acceptable.              ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# ── Persistent MCP session state ────────────────────────────────────────────
# Instead of spawning a FRESH slack-mcp subprocess on every call_read_tool
# invocation (which caused 12+ SAML auths/hour → Slack 429 rate-limit), we hold
# a PERSISTENT session open across scan cycles. One SAML auth on first use, then
# zero until reconnect. Connected lazily on first call_read_tool invocation;
# torn down on app shutdown (via _cleanup_persistent_mcp) or on a transient
# failure (reconnect-once pattern). The scan worker is the only caller of
# call_read_tool (sequential, no parallel calls), but an asyncio.Lock guards
# connect/reconnect against a rare concurrent invocation.


@dataclass
class _PersistentMcpState:
    """Holds the long-lived MCP session + its context managers for teardown.

    All fields start as None (disconnected). On a successful _connect_mcp() they
    are populated atomically; _disconnect_mcp() clears them back to None after
    tearing down the context managers (which reaps the subprocess).
    """

    session: ClientSession | None = None
    read_stream: object | None = None
    write_stream: object | None = None
    # The stdio_client async context manager (holds the subprocess). Calling
    # __aexit__ terminates+waits the child so no orphan survives.
    stdio_cm: object | None = None
    # The ClientSession async context manager.
    session_cm: object | None = None
    # Epoch timestamp of the last successful connect (diagnostics/metrics).
    connected_at: float | None = None


# Module-level singleton: ONE persistent session shared across all scan cycles.
_mcp_state = _PersistentMcpState()

# Serializes connect/reconnect so a rare concurrent call (e.g. a manual refresh
# endpoint racing the scan worker) can't double-spawn. Lazy-init because
# asyncio.Lock cannot be created at module-import time (no running event loop).
_mcp_connect_lock: asyncio.Lock | None = None

# ── Persistent MCP session retry tuning (D-019 → persistent rewrite) ────────
# With a PERSISTENT session, call_read_tool's fast path is just
# session.call_tool() — sub-second, no spawn, no auth. On a transient failure
# (pipe/stream break: anyio BrokenResourceError, the same warmup race from
# D-019) the strategy is: disconnect the dead session, reconnect ONCE (one fresh
# spawn, one SAML auth), and retry the call on the fresh session. If the fresh
# session also fails immediately, fail loud — do NOT loop spawning. So:
#   _SCAN_MCP_RETRY_ATTEMPTS = 2: 1 call on existing session + 1 retry after
#   reconnect. The name-guard rejection happens with ZERO attempts (before any
#   session access). A non-transient error is never retried.
_SCAN_MCP_RETRY_ATTEMPTS = 2  # 1 call on existing session + 1 retry after reconnect
# Backoff (seconds) before the single reconnect attempt. Gives the previous
# process time to fully terminate and any transient OS resource contention to
# clear before we spawn the replacement.
_SCAN_MCP_RETRY_BACKOFF_S = (1.0,)

# THE SEND-SAFETY BOUNDARY (D-018). The deterministic scan worker drives the Slack
# MCP server via a RAW `mcp` SDK client, which — unlike the `claude --print` seam
# gated by --allowedTools — CAN call any tool the server exposes. So the worker is
# made structurally incapable of calling a write tool: call_read_tool REFUSES by
# name any tool NOT in this hardcoded frozenset BEFORE the SDK ever reaches the
# server. These are the server's OWN tool names (UNPREFIXED — the raw SDK calls
# call_tool with the bare names, NOT the claude `mcp__slack-mcp__` prefix). It
# contains ONLY read tools and MUST NEVER contain post_message / edit_message /
# schedule_message / delete_message / any write tool. The ONLY send path remains
# the explicit, user-triggered approve_item (the `claude --print` 'send' seam).
_SLACK_READ_ONLY_TOOLS = frozenset({
    "list_dms",
    "get_unreads",
    "list_my_channels",
    "lookup_user",
    "get_messages",
    "get_thread",
    "search",
    "batch_get_messages",
    "batch_get_threads",
})

# The U+E000 Private-Use-Area sentinel the Slack MCP server substitutes for
# whitespace when it datamarks untrusted text. We normalize it back to a space —
# but ONLY in extracted string FIELDS, AFTER json parsing the payload (normalizing
# the raw JSON BEFORE parse corrupts escape sequences — the known LLM-scan trap
# from the investigation / D-012).
_DATAMARK_SENTINEL = ""

# Per-line read buffer for the subprocess's stdout/stderr StreamReader. The
# stream-json `result` event for a real Slack fetch (many unread conversations +
# their thread context + a generated draft each) is a single line that easily
# exceeds asyncio's 64 KiB default — which raises LimitOverrunError. 8 MiB is
# generous headroom while still bounding memory for one pathological line.
_STREAM_LIMIT_BYTES = 8 * 1024 * 1024

# Slack MCP tools the headless subprocess is pre-approved to call, per action.
# These MUST be passed via --allowedTools or the non-interactive `claude --print`
# stalls on the permission prompt (see _drive_agent). Read tools cover unread/DM
# discovery + thread context for listing/drafting; the write tool (post_message)
# is granted ONLY for 'send', so the list/regenerate subprocess cannot send at all.
_SLACK_READ_TOOLS = [
    "mcp__slack-mcp__get_unreads",
    "mcp__slack-mcp__list_dms",
    "mcp__slack-mcp__get_thread",
    "mcp__slack-mcp__get_messages",
    "mcp__slack-mcp__search",
    "mcp__slack-mcp__lookup_user",
    # Batch read tools let the model fetch many threads/messages in FEWER MCP
    # round-trips, which is the dominant per-scan cost — read-only, so they stay
    # on the 'list'/'regenerate' allowlist and never grant any send ability.
    "mcp__slack-mcp__batch_get_threads",
    "mcp__slack-mcp__batch_get_messages",
]
_SLACK_SEND_TOOLS = ["mcp__slack-mcp__post_message"]
_ALLOWED_TOOLS = {
    # 'list' reuses the read-only allowlist verbatim — the listing subprocess
    # enumerates unreads but is literally unable to send. RETAINED as a read-only
    # seam (D-013): no interval/event drives it any more, but it stays harmless and
    # invocable. 'send' is the live user-triggered per-item write path.
    "list": _SLACK_READ_TOOLS,
    # COLD-START INVARIANT (D-022): 'regenerate' and 'draft' are MCP-FREE —
    # _drive_agent sets needs_mcp=False for them, so they get NO --mcp-config and
    # NO --allowedTools and this map is NEVER consulted at spawn time for them.
    # Their conversation history is pre-fetched on the PERSISTENT warm-auth session
    # (_fetch_history_for) and rendered into the prompt BEFORE the seam runs, so the
    # subprocess never cold-starts a slack-mcp process. These entries are kept ONLY
    # to document that the actions are read-only by design (they grant no write
    # tool); they are dead at spawn time.
    "regenerate": _SLACK_READ_TOOLS,
    "draft": _SLACK_READ_TOOLS,
    # 'send' is the ONLY action that still cold-starts slack-mcp (it needs the
    # post_message write tool the persistent read-only session refuses).
    "send": _SLACK_SEND_TOOLS,
}

# Where the Slack MCP would be configured for the `claude` CLI. Checked
# best-effort by the readiness probe WITHOUT spawning anything; absence yields
# mcpConfigured='unknown' (we can't cheaply prove it either way), never False
# unless we can positively read a config that omits slack-mcp.
def _mcp_config_paths() -> list[Path]:
    home = Path.home()
    return [
        Path.cwd() / ".mcp.json",
        home / ".claude.json",
        home / ".claude" / "mcp.json",
        home / ".config" / "claude" / "mcp.json",
    ]


# Minimal fallback slack-mcp launch spec used ONLY when we cannot read the
# existing entry from a config on disk. Matches the documented slack-mcp shape
# (started via AIM, SAFE_MODE on). It carries NO secrets — slack-mcp gets its
# auth from AIM, never from a token we write.
_SLACK_MCP_FALLBACK = {
    "command": "aim",
    "args": ["mcp", "start-server", "slack-mcp"],
    "env": {"SAFE_MODE": "true"},
}

# Keys we copy from an existing server entry into the minimal --mcp-config. We
# deliberately DROP everything else (and re-scrub) so no stray credential-looking
# material rides into the config file the subprocess reads.
_MCP_ENTRY_KEYS = ("command", "args", "env", "type", "url", "headers")


def _find_slack_mcp_entry() -> dict | None:
    """Best-effort: read the existing slack-mcp server entry from a claude config.

    Looks in the same configs the readiness probe checks, including project-scoped
    ``projects.<cwd>.mcpServers`` maps in ~/.claude.json. Returns the raw entry
    dict for the first server whose name contains 'slack', or None if none is
    readable. NEVER spawns anything.
    """
    def _scan(servers) -> dict | None:
        if not isinstance(servers, dict):
            return None
        for name, entry in servers.items():
            if "slack" in str(name).lower() and isinstance(entry, dict):
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


def _slack_mcp_config() -> dict:
    """Build the minimal ``--mcp-config`` doc that loads ONLY slack-mcp.

    Scoping the subprocess to this ONE server (with --strict-mcp-config) boots ~6
    Slack tools instead of all ~3 globally-configured servers (~150 tools), which
    materially speeds each scan. We reuse the EXISTING slack-mcp entry from disk
    when readable (so we match the live launch shape exactly) and fall back to the
    documented AIM/SAFE_MODE spec otherwise.

    SECURITY: the returned doc is scrubbed of any credential-looking material —
    slack-mcp authenticates via AIM, never via a token we write here. This narrows
    WHICH servers load only; the per-action --allowedTools allowlist remains the
    security boundary for what the subprocess may DO.
    """
    entry = _find_slack_mcp_entry()
    if entry is None:
        entry = dict(_SLACK_MCP_FALLBACK)
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
            # env/headers: drop any pair whose key OR value looks secret.
            clean[key] = {
                k: v for k, v in val.items()
                if not (_looks_secret(str(k)) or (isinstance(v, str) and _looks_secret(v)))
            }
        else:
            clean[key] = val
    return {"mcpServers": {"slack-mcp": clean}}


def _detect_mcp_configured() -> bool | str:
    """Best-effort: does a readable claude/MCP config mention a slack-mcp server?

    Returns True if a config positively lists a slack-mcp entry, False if we read
    a config that has an mcpServers map WITHOUT one, or 'unknown' when we can't
    tell cheaply (no readable config / unparseable). NEVER spawns a subprocess
    and NEVER surfaces any credential material.
    """
    saw_servers = False
    for path in _mcp_config_paths():
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        servers = doc.get("mcpServers") if isinstance(doc, dict) else None
        if isinstance(servers, dict):
            saw_servers = True
            if any("slack" in str(name).lower() for name in servers):
                return True
    return False if saw_servers else "unknown"


def _probe_readiness() -> dict:
    """Report seam readiness WITHOUT any message I/O or send.

    Reports whether ``claude`` is on PATH and (best-effort) whether the Slack MCP
    is configured. ``ready`` is true when claude is present and the MCP isn't
    positively known to be absent ('unknown' is treated as possibly-ready). The
    detail string is scrubbed and never carries credentials.
    """
    claude_on_path = shutil.which("claude") is not None
    mcp_configured = _detect_mcp_configured()
    ready = claude_on_path and mcp_configured is not False

    if not claude_on_path:
        detail = "`claude` CLI not found on PATH — the delegation subprocess can't be spawned."
    elif mcp_configured is False:
        detail = "`claude` is on PATH but no slack-mcp server is configured."
    elif mcp_configured == "unknown":
        detail = "`claude` is on PATH; slack-mcp configuration could not be verified (will be confirmed on first refresh)."
    else:
        detail = "Slack delegation ready: `claude` on PATH and slack-mcp configured."

    return {
        "claudeOnPath": claude_on_path,
        "mcpConfigured": mcp_configured,
        "ready": ready,
        "detail": _scrub(detail),
    }


def _build_agent_prompt(action: str, payload: dict) -> str:
    """Build the STRICT-JSON-returning prompt for one seam action.

    'regenerate' routes drafts through the learned style + per-contact topic
    memory exactly as ``build_draft_prompt`` does. 'list' is draft-free — it only
    ENUMERATES unreads (fast). 'send' instructs the subprocess to send EXACTLY ONE
    message. All prompt text is scrubbed.
    """
    if action == "list":
        # FAST, draft-free enumeration. Per-item drafting in this call is what
        # blew the 120s budget (Bug 4), so it is explicitly forbidden here —
        # drafting happens later via the single-item 'regenerate' path.
        prompt = (
            "Use ONLY the Slack MCP read tools (e.g. mcp__slack-mcp__get_unreads, "
            "mcp__slack-mcp__list_dms, mention lookup, and the batch_get_threads / "
            "batch_get_messages batch tools to fetch context in FEWER round-trips) "
            "to ENUMERATE MY unread direct messages and @mentions, with minimal "
            "thread context for each. Limit yourself to AT MOST "
            f"{SLACK_LIST_CAP} of the MOST-RECENT unread conversations — do not "
            "walk further back than that (more will be picked up on a later scan). "
            "Do NOT send anything. Do NOT generate any reply or draft — listing "
            "must be fast, and drafting will happen later in a separate step.\n\n"
            "Return ONLY a STRICT JSON array (no prose, no markdown fences) where "
            "each element is an object with EXACTLY these keys: "
            '{"id","sender","channel","channelType","snippet","threadContext"}. '
            'channelType must be "dm", "group_dm", or "mention". Do NOT include a "draft" key. '
            "If there are no unread items, return []."
        )
    elif action == "regenerate":
        # MCP-FREE (D-022): regenerate_item already assembled the style+topic-aware
        # prompt AND pre-fetched any recent history on the PERSISTENT warm-auth MCP
        # session (rendered into the prompt via build_draft_prompt). The subprocess
        # gets NO --allowedTools and an EMPTY --mcp-config under --strict-mcp-config
        # (no slack-mcp inherited) and must NOT call any MCP tool — it drafts PURELY
        # from the instructions below.
        base = str(payload.get("prompt", ""))
        prompt = (
            "Re-draft a single Slack reply in MY voice using ONLY the instructions "
            "below — do NOT call any tools, do NOT fetch anything, do NOT send "
            "anything.\n\n"
            f"{base}\n\n"
            'Return ONLY a STRICT JSON object (no prose, no markdown fences): '
            '{"draft": "<the reply text>"}.'
        )
    elif action == "draft":
        # MCP-FREE (D-022): the in-process draft-worker's per-item path.
        # build_draft_prompt already assembled the style+topic-aware instructions,
        # and the worker pre-fetched any recent history on the PERSISTENT warm-auth
        # MCP session (rendered into the prompt). The subprocess gets NO
        # --mcp-config / --allowedTools and must NOT call any MCP tool — it drafts
        # PURELY from the conversation already in the prompt. If history was
        # unreadable the prompt simply carries the snippet alone; we still draft
        # rather than ever cold-starting a slack-mcp subprocess.
        base = str(payload.get("prompt", ""))
        prompt = (
            "Draft a single Slack reply in MY voice using ONLY the instructions "
            "below — do NOT call any tools, do NOT fetch anything, do NOT send "
            "anything.\n\n"
            f"{base}\n\n"
            'Return ONLY a STRICT JSON object (no prose, no markdown fences): '
            '{"draft": "<the reply text>", "generatedDraft": "<the same reply text>", '
            '"threadContext": "<a 1-2 sentence summary of what the conversation is about>"}. '
            "generatedDraft must equal draft. threadContext is a brief plain-text summary "
            "of the conversation topic and what they're asking/discussing — NOT the raw messages."
        )
    elif action == "polish":
        # MCP-FREE (D-029): rewrite the user's draft into a polished professional
        # Slack reply. Has access to thread context and snippet so it can add
        # relevant clarifying questions and structure the reply well. No tool calls,
        # no fetch, no send — empty MCP config under --strict-mcp-config.
        text = str(payload.get("text", ""))
        thread_context = str(payload.get("threadContext", "")).strip()
        snippet = str(payload.get("snippet", "")).strip()
        context_block = ""
        if snippet:
            context_block += f"Message I'm replying to:\n{snippet}\n\n"
        if thread_context:
            context_block += f"Thread context:\n{thread_context}\n\n"
        prompt = (
            "You are helping me polish a Slack reply. Use good judgment on how much "
            "to change based on what the message actually needs:\n\n"
            "- If my draft is already clear and complete: just fix grammar, "
            "awkward phrasing, or clarity. Keep it roughly the same length.\n"
            "- If my draft is rough notes or covers a technical/complex situation: "
            "expand into a well-structured reply. Acknowledge the situation, state "
            "what I will do, and add 1-3 specific clarifying questions ONLY if they "
            "would genuinely help move things forward (base them on the thread — "
            "do NOT add generic questions).\n\n"
            "Always: be concise and direct, no filler phrases, preserve my intent, "
            "use the SAME language (do NOT translate), professional but not stiff.\n\n"
            "Do NOT call any tools, do NOT fetch anything, do NOT send anything.\n\n"
            + (context_block)
            + f"My draft:\n{text}\n\n"
            'Return ONLY a STRICT JSON object (no prose, no markdown fences): '
            '{"draft": "<the polished reply text>"}.'
        )
    elif action == "classify":
        # MCP-FREE (D-025): the classify worker batches needs-classify skeletons and
        # asks the model to label each from the snippet ALONE — no tool calls, no
        # history fetch, no send. The items are rendered inline so the subprocess
        # gets NO --allowedTools and an EMPTY --mcp-config under --strict-mcp-config.
        items = payload.get("items") or []
        lines = []
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id", ""))
            sender = _one_line(str(it.get("sender", "")), 200)
            channel = _one_line(str(it.get("channel", "")), 200)
            snippet = _one_line(str(it.get("snippet", "")), 1000)
            lines.append(
                f'- id: {iid}\n  from: {sender}\n  channel: {channel}\n  message: {snippet}'
            )
        rendered = "\n".join(lines) if lines else "(no items)"
        prompt = (
            "Classify each of MY unread Slack messages below — do NOT call any "
            "tools, do NOT fetch anything, do NOT send anything. For each message, "
            "classify as needs-reply (someone is asking me a question or requesting "
            "action), fyi (status update, notification, acknowledgment, no reply "
            "needed), or actionable (I need to do something but not reply in Slack). "
            "Return JSON array of {id, classification}.\n\n"
            "Messages:\n"
            f"{rendered}\n\n"
            "Return ONLY a STRICT JSON array (no prose, no markdown fences) where "
            "each element is an object with EXACTLY these keys: "
            '{"id", "classification"}. classification must be exactly one of '
            '"needs-reply", "fyi", or "actionable". Include every id above exactly once.'
        )
    elif action == "send":
        # THE ONE intentional cold-start exception (D-022): send spawns a
        # `claude --print --mcp-config` subprocess that boots its own slack-mcp
        # because the persistent read-only session refuses the post_message write
        # tool. Sends are rare and user-gated (approve_item), so the occasional
        # cold auth here is acceptable — unlike the draft/regenerate paths which
        # are now MCP-free. Do NOT route send through the persistent session.
        target = str(payload.get("target", ""))
        text = str(payload.get("text", ""))
        prompt = (
            "Use the Slack MCP send tool to send EXACTLY ONE message and then "
            "stop. Send to this exact Slack channel/user ID and this exact text "
            "— do not modify the text, do not send anything else.\n\n"
            f"Channel ID: {target}\n"
            f"Text: {text}\n\n"
            "This is a routable Slack ID (not a display name) — pass it directly "
            "to post_message as the channel parameter.\n\n"
            'Return ONLY a STRICT JSON object (no prose, no markdown fences): '
            '{"ok": true, "ts": "<message timestamp>"}.'
        )
    else:
        prompt = f"Unknown action: {action}. Return ONLY {{}}."
    return _scrub(prompt)


def _parse_agent_result(text: str, action: str) -> dict:
    """Parse the subprocess's final result text as STRICT JSON.

    On any unparseable/empty/wrong-shape reply, return a fail-loud unavailable
    result — NEVER fabricate items or a send-success. The parsed payload is
    scrubbed/normalized by ``_scrub`` on the caller side; here we only enforce
    shape.
    """
    text = (text or "").strip()
    if not text:
        return {"available": False, "reason": "Slack agent returned an empty reply."}
    # Models sometimes wrap JSON in markdown fences despite the prompt forbidding it.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"available": False, "reason": _scrub(
            "Slack agent reply was not valid JSON: " + _one_line(text, 200))}

    if action == "list":
        if not isinstance(parsed, list):
            return {"available": False, "reason": "Slack list reply was not a JSON array."}
        return {"available": True, "items": parsed}
    if action == "regenerate":
        if not isinstance(parsed, dict) or "draft" not in parsed:
            return {"available": False, "reason": "Slack regenerate reply missing 'draft'."}
        return {"available": True, "draft": parsed.get("draft", "")}
    if action == "polish":
        # Mirrors 'regenerate' exactly (require a dict with 'draft'); fail-loud on a
        # missing 'draft' rather than fabricating a polished text.
        if not isinstance(parsed, dict) or "draft" not in parsed:
            return {"available": False, "reason": "Slack polish reply missing 'draft'."}
        return {"available": True, "draft": parsed.get("draft", "")}
    if action == "draft":
        # Mirrors 'regenerate' (require a dict with 'draft'), but also passes
        # through the history3d list and generatedDraft the subprocess fetched.
        # generatedDraft defaults to draft when absent (it's the machine baseline);
        # history3d defaults to [] and is only kept if it's actually a list.
        if not isinstance(parsed, dict) or "draft" not in parsed:
            return {"available": False, "reason": "Slack draft reply missing 'draft'."}
        draft = parsed.get("draft", "")
        history3d = parsed.get("history3d")
        if not isinstance(history3d, list):
            history3d = []
        generated = parsed.get("generatedDraft")
        if generated is None:
            generated = draft
        thread_context = parsed.get("threadContext", "")
        return {
            "available": True,
            "draft": draft,
            "history3d": history3d,
            "generatedDraft": generated,
            "threadContext": thread_context,
        }
    if action == "classify":
        # Fail-loud on a non-array reply (never fabricate classifications). The
        # routing in _classify_one only acts on the ids it recognizes, so a partial
        # or extra-id array is tolerated; we only require the array shape here.
        if not isinstance(parsed, list):
            return {"available": False, "reason": "Slack classify reply was not a JSON array."}
        return {"available": True, "classifications": parsed}
    if action == "send":
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            return {"available": False, "reason": "Slack send did not confirm ok=true."}
        return {"available": True, "ts": parsed.get("ts")}
    return {"available": False, "reason": f"Unknown action: {action}."}


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Send a signal to the child's whole process GROUP, falling back to the pid.

    The child is spawned with start_new_session=True, so its pid is its own
    process-group id; signalling the group reaches the `claude` shim AND the real
    claude-code binary it forks (which a bare proc.kill() would orphan). Swallows
    ProcessLookupError (already gone) and any loop-teardown error — reaping must
    never raise into the request path.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        # Group gone or ungettable — fall back to signalling the pid directly.
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError, RuntimeError):
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Terminate→kill the subprocess GROUP with bounded waits so no orphan is left.

    Mirrors PersistentSession.stop()'s escalation but signals the whole process
    group (see _signal_group) because `claude` is a shim that forks the real
    binary. None-guards stdin (it can be None mid-shutdown — backend-dev.md F7).
    Every wait is shielded so a cancelled/closing event loop can't leave the
    child unreaped or surface a 'Event loop is closed' error.
    """
    if proc.returncode is not None:
        return
    if proc.stdin:
        try:
            proc.stdin.close()
        except (OSError, RuntimeError):
            pass

    async def _wait(timeout: float) -> bool:
        """Wait up to timeout for exit; True if it exited. Never raises."""
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except (RuntimeError, ProcessLookupError):
            # Loop closing or child already gone — treat as reaped.
            return True

    if await _wait(5):
        return
    _signal_group(proc, signal.SIGTERM)
    if await _wait(5):
        return
    _signal_group(proc, signal.SIGKILL)
    await _wait(5)


def _unlink_quiet(path: str) -> None:
    """Best-effort remove of the temp --mcp-config file; never raises."""
    try:
        os.unlink(path)
    except OSError:
        pass


async def _drive_agent(action: str, payload: dict) -> dict:
    """Spawn ONE `claude --print` subprocess, run the action, parse STRICT JSON.

    Follows the SessionManager.start()/send() spawn shape verbatim (stream-json
    in/out), but as a one-shot driver. The ONLY subprocess site in this module.
    """
    claude_bin = shutil.which("claude") or "claude"
    # Pre-approve EXACTLY the Slack MCP tools this action needs via --allowedTools.
    # WHY: a headless `claude --print` subprocess cannot answer an interactive
    # permission prompt, so without an allowlist it stalls on the first MCP tool
    # call and eventually replies in prose ("I'm blocked on a permission grant…")
    # — which fails the strict-JSON parse and surfaces as available:false.
    # --permission-mode acceptEdits does NOT cover MCP tool calls; only an
    # explicit --allowedTools entry pre-approves them (verified: default mode +
    # this allowlist runs get_unreads headlessly and returns clean JSON).
    # Least-privilege + a real safety boundary: only 'send' is granted the
    # post_message write tool. Nothing else (no Bash, no file edits) is allowed,
    # so the subprocess can't wander off-task.
    #
    # COLD-START INVARIANT (D-022): the 'draft' and 'regenerate' actions NEVER set
    # needs_mcp=True. Their history is pre-fetched on the PERSISTENT warm-auth MCP
    # session (via _fetch_history_for) and injected into the prompt BEFORE the seam
    # is called, so the `claude --print` subprocess just generates text from the
    # supplied history — it gets NO --allowedTools and an EMPTY --mcp-config (no
    # servers) passed WITH --strict-mcp-config, so it does NOT inherit the user's
    # global slack-mcp entry and never cold-starts a slack-mcp process (zero extra
    # SAML auth = no "r5 status: 429"). The --strict-mcp-config is the load-bearing
    # part: WITHOUT it a bare `claude --print` reads the global ~/.claude.json
    # mcpServers (which contains slack-mcp) and boots it on startup.
    # 'send' is the ONE intentional exception: it spawns `claude --print
    # --mcp-config` because the persistent read-only session refuses post_message
    # (sends are rare and user-gated). 'list' is retained as a harmless read-only
    # seam (no scan loop drives it any more, D-013) and keeps its read allowlist.
    # 'classify' (D-025) is MCP-FREE like draft/regenerate: it reasons ONLY over the
    # snippets already in the skeletons, calls no Slack tool, and so gets NO
    # --allowedTools and an EMPTY --mcp-config under --strict-mcp-config (zero
    # slack-mcp cold-start). _ALLOWED_TOOLS has no 'classify' entry — it's dead at
    # spawn time when needs_mcp=False (documented, never grants any tool).
    # 'polish' (D-029) is MCP-FREE for the same reason: it reasons ONLY over the
    # draft text already in the prompt (a pure text transform), calls no Slack tool,
    # and gets NO --allowedTools and an EMPTY --mcp-config under --strict-mcp-config.
    needs_mcp = action not in ("draft", "regenerate", "classify", "polish")
    if needs_mcp:
        allowed_tools = ",".join(_ALLOWED_TOOLS.get(action, _SLACK_READ_TOOLS))
    else:
        allowed_tools = ""

    # ALWAYS write an inline --mcp-config and ALWAYS pass --strict-mcp-config.
    # WHY (D-022 cold-start invariant): without --strict-mcp-config, a bare
    # `claude --print` INHERITS the user's GLOBAL ~/.claude.json mcpServers — which
    # contains slack-mcp — and cold-starts it (full SAML chain) on every draft /
    # regenerate spawn, violating the "spawns ZERO new slack-mcp processes" spec.
    # --strict-mcp-config makes the subprocess use ONLY the servers in our config
    # file and ignore all globally-configured ones. So:
    #   * needs_mcp=True  -> config loads ONLY slack-mcp (the 'send'/'list' seams).
    #   * needs_mcp=False -> EMPTY config (no servers) so the draft/regenerate
    #     subprocess sees no slack-mcp and cold-starts nothing.
    mcp_config_path: str | None = None
    mcp_doc = _slack_mcp_config() if needs_mcp else {"mcpServers": {}}
    try:
        fd, mcp_config_path = tempfile.mkstemp(prefix="slack-mcp-", suffix=".json")
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
        # mkstemp failed: we could not write a scoping config. Pass bare
        # --strict-mcp-config anyway so the subprocess still ignores the global
        # slack-mcp entry rather than silently cold-starting it. (claude treats
        # --strict-mcp-config with no --mcp-config as "use no MCP servers".)
        args += ["--strict-mcp-config"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.cwd()),
            # A real Slack fetch (many unread conversations + thread context)
            # emits a single stream-json line far larger than asyncio's default
            # 64 KiB readline() buffer, which raised LimitOverrunError → an
            # uncaught ValueError → HTTP 500. Raise the per-line StreamReader
            # limit so large-but-legitimate result events parse cleanly.
            limit=_STREAM_LIMIT_BYTES,
            # Run the child in its OWN process group. The `claude` launcher is a
            # shim that forks the real claude-code binary; terminating only the
            # shim (proc.kill) orphaned that grandchild (seen live: a leftover
            # claude --print after a 120s timeout). Killing the whole group in
            # _reap reaches every descendant so nothing survives a timeout.
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
                # An over-limit stderr line (LimitOverrunError subclasses
                # ValueError). Draining stderr is best-effort; never let it
                # crash the drain task.
                pass

    stderr_task = asyncio.create_task(_drain_stderr())

    prompt = _build_agent_prompt(action, payload)
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
            f"Slack agent timed out after {timeout_s}s.")}
    except (OSError, RuntimeError, ValueError) as e:
        # ValueError covers asyncio's LimitOverrunError when a single stream-json
        # line exceeds the (already-raised) read buffer — fail loud, never 500.
        return {"available": False, "reason": _scrub(f"Slack agent I/O error: {e}")}
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        await _reap(proc)
        if mcp_config_path:
            _unlink_quiet(mcp_config_path)

    stderr_blob = "\n".join(stderr_tail)
    evt = outcome["evt"]
    result_text = outcome["text"]

    # Auth failures: classify via result text + drained stderr (some credential
    # errors only appear on stderr), reusing session_manager.is_auth_error.
    auth_blob = " ".join(filter(None, [result_text, stderr_blob]))
    if is_auth_error(auth_blob):
        return {"available": False, "reason": _scrub(
            "Slack agent failed with an auth/credential error — re-authenticate "
            "(e.g. refresh Midway/AWS credentials) and retry.")}

    # Non-zero exit or an error-flagged result → fail loud, no fabrication.
    if proc.returncode not in (0, None):
        return {"available": False, "reason": _scrub(
            f"Slack agent exited with code {proc.returncode}. "
            + _one_line(stderr_blob, 200))}
    if evt is None:
        return {"available": False, "reason": _scrub(
            "Slack agent produced no result event. " + _one_line(stderr_blob, 200))}
    if evt.get("is_error"):
        return {"available": False, "reason": _scrub(
            "Slack agent reported an error: " + _one_line(result_text, 200))}

    return _parse_agent_result(result_text, action)


async def _run_slack_agent(action: str, payload: dict) -> dict:
    """THE seam: the ONLY function that spawns/awaits a Claude subprocess.

    Spawns a one-shot ``claude --print`` process (SessionManager spawn shape)
    with the Slack MCP available, hands it a STRICT-JSON-returning prompt for
    ``action`` ('list' | 'regenerate' | 'draft' | 'send'), and parses the reply:
      * 'list' → {available:True, items:[...]} — a FAST, draft-free enumeration
        of unread DMs/@mentions (shorter _LIST_TIMEOUT_S budget, no drafting).
      * 'regenerate' → {available:True, draft:...} for the one item (the
        single-item, user-triggered redraft path behind POST /queue/{id}/refresh).
      * 'draft' → {available:True, draft, history3d, generatedDraft} for the one
        item — the in-process draft-worker's path (D-015); read-only tools only,
        the subprocess fetches history3d itself and returns it with the draft.
      * 'classify' → {available:True, classifications:[{id, classification}]} for a
        BATCH of needs-classify skeletons (D-025) — the MCP-FREE classify worker's
        path; the subprocess reasons over the snippets already in the skeletons (no
        tool calls), labelling each needs-reply / fyi / actionable.
      * 'polish' → {available:True, draft:...} — the MCP-FREE per-edit fluency pass
        (D-029) behind POST /api/slack/polish; the subprocess polishes the draft
        text already in the prompt (no tool calls, no fetch, no send, no store).
      * 'send' → {available:True, ts:...} after sending EXACTLY ONE message.

    FAIL-LOUD (feedback_principle_fail_loud_on_missing_input): on ANY spawn
    failure, non-zero exit, timeout, auth error, or unparseable/empty/non-JSON
    reply, returns {available:False, reason:<specific, SCRUBBED>}. NEVER
    fabricates an items array or a send-success. The subprocess is always reaped
    (terminate→kill) so no orphan is left. Tests monkeypatch this function to
    exercise the contract without spawning a real `claude`.
    """
    return await _drive_agent(action, payload)


# ── Read-only direct-MCP client (D-018 — the send-safety boundary) ────────────


class SlackMcpError(Exception):
    """A direct-MCP read failed (connect / handshake / call / parse / tool error).

    Raised by ``call_read_tool`` so callers FAIL LOUD — the scan worker treats it
    as "leave state untouched, retry next cycle" and NEVER fabricates data, exactly
    like the ``_drive_agent`` seam's ``available:False`` contract.
    """


def _slack_mcp_params() -> StdioServerParameters:
    """Build the SDK StdioServerParameters for the Slack MCP server.

    Derived from the SAME source the ``claude --print`` seam uses
    (``_find_slack_mcp_entry`` → ``_SLACK_MCP_FALLBACK``) so the direct-MCP client
    spawns the IDENTICAL ``slack-mcp`` process — inheriting the same ambient Midway
    auth, with NO new auth wiring. Any secret-looking env pair is dropped via the
    existing ``_looks_secret`` guard (slack-mcp authenticates via AIM, never a
    token we pass). The slack-mcp path is NEVER hardcoded.
    """
    entry = _find_slack_mcp_entry()
    if entry is None:
        entry = dict(_SLACK_MCP_FALLBACK)

    command = entry.get("command") or _SLACK_MCP_FALLBACK["command"]
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


def _normalize_tool_result(result) -> object:
    """Normalize a CallToolResult into a plain dict / list / str of structured content.

    The Slack MCP server logs to STDERR and writes only JSON-RPC to STDOUT (the
    investigation confirmed this), so the SDK parses the envelope for us. We still
    parse DEFENSIVELY: prefer the typed ``structuredContent`` when present, else
    join the text content blocks and try to ``json.loads`` them (Slack read tools
    return their payload as a JSON string in a TextContent block). Returns the
    parsed object, or the raw joined text when it isn't JSON. Raises nothing — the
    caller decides what to do with a non-dict/non-list shape.
    """
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
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # Slack MCP wraps responses in a safety directive; JSON is after "---\n"
        sep = "\n---\n"
        if sep in blob:
            json_part = blob.split(sep, 1)[1].strip()
            try:
                return json.loads(json_part)
            except json.JSONDecodeError:
                pass
        return blob


# Retryable stream-error leaves (D-019). The SDK raises the failure from inside an
# anyio TaskGroup, so the cause surfaces wrapped in a BaseExceptionGroup; these are
# the leaf types that mean "the pipe/stream broke transiently — a fresh subprocess
# is likely to succeed." Anything else (auth, parse, an isError result) is NOT here.
_TRANSIENT_MCP_LEAVES = (
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
)

# Broken-pipe-class OSError errnos that also mean a transient stream break.
_TRANSIENT_OS_ERRNOS = frozenset({errno.EPIPE, errno.ECONNRESET, errno.ESHUTDOWN})


def _is_transient_mcp_error(exc: BaseException) -> bool:
    """Recursively unwrap ``exc`` to decide whether it is a RETRYABLE stream break.

    The ``mcp`` SDK raises the real cause from inside an anyio TaskGroup, so it
    surfaces as a ``BaseExceptionGroup``/``ExceptionGroup`` (and/or chained via
    ``__cause__``/``__context__``) wrapping the actual failure. We walk the group
    members AND the cause/context chain, bounded against cycles, looking for a
    retryable LEAF:

      * ``anyio.BrokenResourceError`` / ``ClosedResourceError`` / ``EndOfStream``
        (the stream resource broke mid-flight — the warmup race), OR
      * a broken-pipe-class ``OSError`` (``BrokenPipeError`` / ``ConnectionResetError``,
        or ``errno`` in EPIPE/ECONNRESET/ESHUTDOWN).

    Anything else — and an ExceptionGroup with NO retryable leaf — is NON-transient
    (so an auth/parse error, or an ``isError`` result, is never retried). Never
    raises; returns ``False`` on anything it can't classify.
    """
    seen: set[int] = set()

    def _walk(e: BaseException | None, depth: int) -> bool:
        if e is None or depth > 20 or id(e) in seen:
            return False
        seen.add(id(e))

        if isinstance(e, _TRANSIENT_MCP_LEAVES):
            return True
        if isinstance(e, (BrokenPipeError, ConnectionResetError)):
            return True
        if isinstance(e, OSError) and e.errno in _TRANSIENT_OS_ERRNOS:
            return True

        # ExceptionGroup / BaseExceptionGroup members (guard: not every exc has it).
        for sub in getattr(e, "exceptions", None) or ():
            if _walk(sub, depth + 1):
                return True
        # Chained causes.
        if _walk(getattr(e, "__cause__", None), depth + 1):
            return True
        if _walk(getattr(e, "__context__", None), depth + 1):
            return True
        return False

    return _walk(exc, 0)



def _get_mcp_lock() -> asyncio.Lock:
    """Get or create the module-level asyncio.Lock for persistent MCP connect/reconnect.

    Cannot be created at module-import time (no running event loop), so we lazily
    create it on first use within an async context.
    """
    global _mcp_connect_lock
    if _mcp_connect_lock is None:
        _mcp_connect_lock = asyncio.Lock()
    return _mcp_connect_lock


def _get_scan_lock() -> asyncio.Lock:
    """Get or create the shared scan-in-progress guard (D-023).

    A single asyncio.Lock held for the duration of ONE ``_scan_once`` so the
    periodic worker scan and a manual Refresh can never overlap or double-spawn the
    MCP session. Lazily created (no running loop at import time), exactly like
    ``_get_mcp_lock``. The worker ACQUIRES it (non-blocking) around its scan; the
    refresh endpoint only READS ``.locked()`` to detect an in-flight scan — it never
    acquires it, so there is no check-then-acquire race between the two.
    """
    global _scan_in_progress_lock
    if _scan_in_progress_lock is None:
        _scan_in_progress_lock = asyncio.Lock()
    return _scan_in_progress_lock


def _get_scan_event() -> asyncio.Event:
    """Get or create the manual-refresh wake Event (D-023).

    Set by the refresh endpoint to interrupt the worker's interval sleep so a scan
    starts promptly; the worker clears it at the top of each cycle. Lazily created
    (no running loop at import time), like ``_get_mcp_lock``.
    """
    global _scan_wake_event
    if _scan_wake_event is None:
        _scan_wake_event = asyncio.Event()
    return _scan_wake_event


async def _run_guarded_scan(app) -> bool:
    """Run ONE ``_scan_once`` under the shared scan guard; skip if one is in flight.

    Acquires the scan-in-progress guard NON-BLOCKING: if a scan is already running
    (the guard is held), returns ``False`` immediately (the caller decides how to
    report the skip — the worker logs, the refresh endpoint never reaches here).
    Otherwise acquires the guard, runs the single deterministic scan, and ALWAYS
    releases the guard in ``finally`` so a raising scan can never leave it held /
    deadlock a later trigger. Returns ``True`` when the scan ran.

    The only acquirer is the single scan-worker task, so the ``locked()`` → acquire
    sequence (no await in between) is race-free in practice; the refresh endpoint
    only inspects ``.locked()`` and never acquires.
    """
    lock = _get_scan_lock()
    if lock.locked():
        return False
    await lock.acquire()
    try:
        await _scan_once(app)
        return True
    finally:
        lock.release()


async def _disconnect_mcp() -> None:
    """Tear down the persistent MCP session, reaping the subprocess cleanly.

    Idempotent: if no session is connected, this is a no-op. Properly exits the
    ClientSession and stdio_client context managers (the latter terminates the
    slack-mcp subprocess) so no orphan process is left. Clears ``_mcp_state``
    fields to None so a subsequent ``_connect_mcp()`` will reconnect fresh.

    Called by ``_cleanup_persistent_mcp`` (app shutdown hook) and by
    ``call_read_tool`` on a transient failure (reconnect-once pattern). Always
    acquires the connect lock to prevent a concurrent connect from racing the
    teardown. Swallows exceptions during teardown (logs scrubbed at debug) so a
    dead-process cleanup never raises.
    """
    lock = _get_mcp_lock()
    async with lock:
        if _mcp_state.session is None:
            return  # Already disconnected — nothing to do.

        # Exit session first, then stdio (which reaps the subprocess).
        if _mcp_state.session_cm is not None:
            try:
                await _mcp_state.session_cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001 — teardown must not raise
                logger.debug(
                    "Persistent MCP session_cm teardown swallowed: %s",
                    _scrub(str(e)),
                )
        if _mcp_state.stdio_cm is not None:
            try:
                await _mcp_state.stdio_cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001 — teardown must not raise
                logger.debug(
                    "Persistent MCP stdio_cm teardown swallowed: %s",
                    _scrub(str(e)),
                )

        # Clear all state so the next _connect_mcp() spawns fresh.
        _mcp_state.session = None
        _mcp_state.stdio_cm = None
        _mcp_state.session_cm = None
        _mcp_state.read_stream = None
        _mcp_state.write_stream = None
        _mcp_state.connected_at = None
        logger.info("Persistent MCP session disconnected (subprocess reaped).")


async def _connect_mcp() -> None:
    """Lazily connect the persistent MCP session (idempotent under lock).

    Acquires the connect lock, short-circuits if already connected, else spawns
    ``slack-mcp`` via ``stdio_client``, performs the ``initialize`` handshake (one
    SAML auth on the slack-mcp side), and stores everything in ``_mcp_state``. On
    any exception during connect, tears down whatever was partially opened (calls
    ``__aexit__`` in reverse order) and re-raises so the caller fails loud — NEVER
    leaves a half-opened session or an orphan subprocess lingering.
    """
    lock = _get_mcp_lock()
    async with lock:
        if _mcp_state.session is not None:
            return  # Already connected — short-circuit.

        params = _slack_mcp_params()
        stdio_cm = None
        session_cm = None
        try:
            # Enter the stdio_client context manager — spawns the subprocess.
            stdio_cm = stdio_client(params)
            read_stream, write_stream = await stdio_cm.__aenter__()

            # Enter the ClientSession context manager — wraps the streams.
            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()

            # Initialize the MCP handshake (capabilities exchange / SAML auth).
            await session.initialize()

            # Commit: store everything atomically in the module-level state.
            _mcp_state.session = session
            _mcp_state.stdio_cm = stdio_cm
            _mcp_state.session_cm = session_cm
            _mcp_state.read_stream = read_stream
            _mcp_state.write_stream = write_stream
            _mcp_state.connected_at = time.time()
            logger.info(
                "Persistent MCP session connected (slack-mcp subprocess spawned)."
            )
        except BaseException:
            # Partial-open cleanup: tear down in reverse order, swallowing errors
            # from each layer so we always attempt both and never leave an orphan.
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            if stdio_cm is not None:
                try:
                    await stdio_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            raise


async def call_read_tool(name: str, arguments: dict) -> object:
    """Call ONE Slack MCP READ tool via a PERSISTENT session; FAIL LOUD on error.

    THE SEND-SAFETY BOUNDARY (D-018): the FIRST thing this does — BEFORE any
    connection, handshake, or ``call_tool`` — is reject ``name`` if it is not in the
    hardcoded ``_SLACK_READ_ONLY_TOOLS`` frozenset. A raw MCP client can call ANY
    tool the server exposes (it is NOT gated by ``--allowedTools`` like the
    ``claude`` seam), so this name guard is what makes the scan worker structurally
    incapable of calling ``post_message`` or any write tool. The rejection happens
    locally; the SDK never touches the server for a disallowed name.

    PERSISTENT SESSION (D-020): instead of spawning a fresh slack-mcp subprocess on
    every call (which triggers a full SAML auth each time, causing 429 rate-limit at
    12+ auths/hour), we hold ONE persistent session across scan cycles. Connected
    lazily on first use via ``_connect_mcp``; reused on subsequent calls (sub-second,
    zero auth). On a transient failure (the stream broke — the slack-mcp process
    died or the pipe broke), we disconnect (``_disconnect_mcp`` reaps the subprocess),
    reconnect ONCE (one fresh spawn + one auth), and retry the call on the fresh
    session. If the fresh session also fails immediately, fail loud (no loop).

    Returns the tool's structured content as a plain dict/list/str (parsed
    defensively — the server logs to stderr, only JSON-RPC on stdout). On a
    disallowed name, an ``isError`` result, a failed reconnect, or ANY
    non-transient exception, raises ``SlackMcpError`` — NEVER fabricates data and
    NEVER partially writes (mirrors the ``_drive_agent`` fail-loud contract).
    """
    if name not in _SLACK_READ_ONLY_TOOLS:
        # Structural guard: refuse by name BEFORE the SDK touches the server. This
        # is the single line that makes the worker unable to call a write tool.
        raise SlackMcpError(
            f"Refusing to call Slack tool {name!r}: not in the read-only allowlist "
            "(the deterministic scan worker may only call read tools)."
        )

    result = None
    for attempt in range(_SCAN_MCP_RETRY_ATTEMPTS):
        try:
            # Ensure the persistent session is connected (idempotent, self-locking).
            await _connect_mcp()

            # Call the tool on the persistent session. The call may take seconds;
            # it runs outside any lock (in practice the scan worker is sequential).
            result = await _mcp_state.session.call_tool(name, arguments or {})
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — classify, reconnect transient, else fail loud
            remaining = _SCAN_MCP_RETRY_ATTEMPTS - 1 - attempt
            if remaining > 0 and _is_transient_mcp_error(e):
                # Transient stream break — the persistent session is dead. Tear it
                # down (reaps the subprocess) and reconnect fresh on the next attempt.
                logger.debug(
                    "Slack MCP call %s hit a transient stream error (attempt %d/%d), "
                    "reconnecting: %s",
                    name, attempt + 1, _SCAN_MCP_RETRY_ATTEMPTS,
                    _scrub(str(e)),
                )
                await _disconnect_mcp()
                # Brief backoff before the reconnect attempt.
                backoff = _SCAN_MCP_RETRY_BACKOFF_S[
                    min(attempt, len(_SCAN_MCP_RETRY_BACKOFF_S) - 1)
                ]
                await asyncio.sleep(backoff)
                continue
            # Non-transient, OR transient with no attempts left → fail loud now,
            # exactly as before (never fabricate, never partial-write).
            raise SlackMcpError(_scrub(f"Slack MCP call {name} failed: {e}")) from e

    if getattr(result, "isError", False):
        # The server reported a tool-level error (e.g. expired auth). Surface its
        # text, scrubbed, and fail loud — never treat an error result as data.
        detail = _normalize_tool_result(result)
        raise SlackMcpError(_scrub(f"Slack MCP tool {name} returned an error: "
                                   + _one_line(str(detail), 200)))

    return _normalize_tool_result(result)


async def _scan_read(name: str, arguments: dict) -> object:
    """Thin awaitable the scan worker calls (and tests monkeypatch).

    Keeps ``call_read_tool`` as the guarded primitive while giving the worker —
    and the test suite — ONE function to patch without bypassing the name guard.
    """
    return await call_read_tool(name, arguments)


async def _mark_channel_read(channel_id: str) -> None:
    """Best-effort mark a channel as read via set_last_read on the persistent session.

    This is the ONLY non-read tool allowed through the persistent MCP session.
    It cannot send messages or modify content — it only clears the unread badge.
    Failures are swallowed (non-fatal): the user's action (send/dismiss) already
    succeeded; a failed mark-read just means the Slack badge persists.
    Uses ONLY the persistent session — never spawns a one-shot connection (which
    would trigger a fresh SAML auth and risk 429 rate limits).
    """
    if not channel_id:
        return
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        await _connect_mcp()
        await _mcp_state.session.call_tool("set_last_read", {
            "channel": channel_id,
            "timestamp": now_iso,
        })
    except Exception:  # noqa: BLE001 — non-fatal; the action already succeeded
        pass



async def get_health(request: web.Request) -> web.Response:
    """GET /api/slack/health — read-only seam readiness. No message I/O, no send."""
    return web.json_response(_probe_readiness())


# ── Draft-prompt assembly (unit-testable; injected into the seam) ─────────────


def read_style_notes() -> str:
    """Read the human-readable learned-style store (empty string if absent)."""
    content, _ = filestore.read_text(_style_path())
    return content


def read_topic_summary(contact: str) -> str:
    """Read a contact's running topic/context summary (empty if absent)."""
    if not contact or ".." in contact or "/" in contact or "\\" in contact:
        return ""
    content, _ = filestore.read_text(_topics_dir() / f"{contact}.md")
    return content


# Exact re-draft instruction (D-015, spec item 5) appended when an item received
# new messages since its last draft. Worded so the model addresses ALL unanswered
# messages as ONE cohesive reply rather than replying to each individually.
_REDRAFT_INSTRUCTION = (
    "The sender has sent additional messages since the last draft. Compose ONE "
    "reply that addresses ALL their unanswered messages naturally — do not reply "
    "to each message individually. The reply should feel like a single thoughtful "
    "response to their entire thread of messages."
)


def _format_history3d(history3d: list) -> str:
    """Render an ordered (oldest→newest) history3d array as readable lines.

    Each entry is ``{ts, author, text}``; tolerant of missing keys and capped at
    ``_HISTORY3D_CAP`` (newest kept). Returns "" for an empty/non-list input so
    the caller can fall back to the thread-context block.
    """
    if not isinstance(history3d, list) or not history3d:
        return ""
    kept = history3d[-_HISTORY3D_CAP:]
    lines = []
    for msg in kept:
        if not isinstance(msg, dict):
            continue
        author = str(msg.get("author", "") or "?")
        text = _one_line(str(msg.get("text", "")), _HISTORY3D_TEXT_CAP)
        lines.append(f"- {author}: {text}")
    return "\n".join(lines)


def build_draft_prompt(item: dict) -> str:
    """Assemble the draft-generation prompt for one queue item.

    Injects (1) the accumulated learned-style notes and (2) the relevant
    contact's running topic summary, so generated drafts sound like the user and
    reference prior context. This helper's OUTPUT is unit-testable on its own —
    that's how we demonstrate the style+topic memory reaches the prompt even
    while ``_run_slack_agent`` is stubbed. The assembled text is scrubbed so no
    credential material rides into the prompt.

    RE-DRAFT (D-015): when the item already carries a ``history3d`` array and/or
    ``needsRedraft`` is set (the conversation grew new messages), the FULL
    history3d is injected and the exact re-draft instruction is appended so the
    model composes ONE cohesive reply addressing ALL unanswered messages rather
    than replying to each individually.
    """
    sender = item.get("sender", "")
    channel = item.get("channel", "")
    snippet = item.get("snippet", "")
    thread = item.get("threadContext", "")
    style = read_style_notes().strip()
    topics = read_topic_summary(sender).strip()

    history_block = _format_history3d(item.get("history3d") or [])
    is_redraft = bool(item.get("needsRedraft")) or bool(history_block)

    # Decide the reply language from the CONVERSATION text (incoming message +
    # recent history), not the style samples. The learned-style store is almost
    # entirely Chinese, and an English thread still drafted Chinese because the
    # model anchored on those samples — so when the conversation has NO Chinese,
    # we DROP the Chinese style samples entirely (they hurt more than help) and
    # tell the model to reply in English. CJK Unified Ideographs range check.
    convo_text = (snippet or "") + " " + " ".join(
        str(m.get("text", "")) for m in (item.get("history3d") or [])
        if isinstance(m, dict) and m.get("author", "").lower() != _MY_USERNAME
    )
    convo_has_chinese = any("一" <= ch <= "鿿" for ch in convo_text)

    # When the conversation has no Chinese, the (mostly-Chinese) style samples
    # only drag the reply toward Chinese — so omit them and use a neutral style hint.
    style_for_prompt = style if convo_has_chinese else ""

    parts = [
        "You are drafting a Slack reply in MY voice. Match my style; be concise.",
        "",
        "## My learned writing style",
        "(These samples teach TONE and PHRASING only — NOT which language to reply in. "
        "Many samples are in Chinese because of who I was talking to; do NOT let that "
        "make you reply in Chinese. The reply language is decided ONLY by the incoming "
        "conversation, per the rule at the bottom.)",
        "",
        style_for_prompt or "(no language-relevant style notes apply here — keep it concise, "
        "direct, and warm, matching the conversation's own register)",
        "",
        f"## What {sender or 'this contact'} and I have discussed",
        topics or "(no prior context captured yet)",
        "",
        "## Incoming message",
        f"From: {sender}",
        f"Channel: {channel}",
        f"Message: {snippet}",
    ]
    if history_block:
        parts += [
            "",
            "## Recent conversation (oldest → newest)",
            history_block,
        ]
    else:
        parts += [
            "",
            "## Thread context",
            thread or "(none)",
        ]
    if is_redraft:
        parts += ["", _REDRAFT_INSTRUCTION]
    parts += [
        "",
        "## LANGUAGE RULE (decisive — overrides any language seen in my style samples)",
        "Reply in the SAME language the OTHER people are using in the incoming message "
        "and recent conversation above. English conversation → reply in English. Chinese "
        "conversation → reply in Chinese. Mixed/code-switched → mirror that mix. Judge ONLY "
        "from what they wrote, never from my style samples. If the most recent incoming "
        "message is in English, your reply MUST be in English.",
        "",
        "Write only the reply text.",
    ]
    return _scrub("\n".join(parts))


# ── Queue endpoints ──────────────────────────────────────────────────────────


async def get_queue(request: web.Request) -> web.Response:
    """GET the warm queue plus the durable last-scan timestamp (D-023).

    ``lastScanAt`` is the top-level epoch-millis key _scan_once persists into
    slack_threads.json on EVERY completed scan; it mirrors the in-memory
    ``_LAST_SCAN_TS_MS`` and survives a server restart (``_load`` does not strip
    unknown keys). The frontend renders "Updated {relativeTime(lastScanAt)}" from
    it so the freshness label reflects Slack-scan freshness, not page-fetch
    freshness. It is ``None`` (omitted) until the first scan ever completes, so the
    page degrades gracefully (no "just now" when nothing was scanned).
    """
    data, etag = await _load_async()
    return web.json_response({
        "items": data["items"],
        "etag": etag,
        "lastScanAt": data.get("lastScanAt"),
        # The on/off toggle (D-025). Absent on disk == unpaused (read at use sites
        # with .get(..., False), never seeded into _load's default).
        "paused": data.get("paused", False),
    })


# Terminal statuses that do NOT count toward "to review" — kept in lockstep with
# the frontend's isActionable/countActionable (frontend/src/lib/slackQueue.js).
# Slack's terminal outbound state is 'sent' (NOT 'approved' — that's email). An
# explicit denylist of the two terminal states means a new active status (or a
# missing/empty status on an older item) ALWAYS counts, so the count can never
# silently undercount and make the NavBar badge disagree with the page.
_TERMINAL_STATUSES = ("sent", "dismissed")


def _count_actionable(items) -> int:
    """Count items that still need attention — mirrors frontend isActionable/countActionable.

    Actionable == status NOT in the terminal set {sent, dismissed}; a
    missing/empty status counts. The frontend twin lives in
    frontend/src/lib/slackQueue.js — keep in lockstep.
    """
    if not isinstance(items, list):
        return 0
    return sum(
        1 for it in items
        if isinstance(it, dict) and it.get("status") not in _TERMINAL_STATUSES
    )


async def get_queue_count(request: web.Request) -> web.Response:
    """GET just the actionable-item count — the lightweight NavBar bubble feed (C1).

    Returns ``{"count": N}`` and NOTHING else: NavBar's badge needs only the number,
    so this avoids shipping the multi-hundred-KB full queue (items carry drafts +
    3-day history) on every mount and every 'slack_changed' WS event.

    N uses the EXACT same semantics as the frontend ``countActionable``
    (frontend/src/lib/slackQueue.js): an item COUNTS unless its ``status`` is one of
    the two terminal states {'sent', 'dismissed'} — a missing/empty status counts.
    It is a denylist (not an allowlist) so a new active status can never silently
    fall out of the count and make the badge disagree with the page's "N to review".
    Reads via ``_load_async`` so it never blocks the event loop.
    """
    data, _etag = await _load_async()
    count = _count_actionable(data["items"])
    return web.json_response({"count": count})


async def put_config(request: web.Request) -> web.Response:
    """PUT the top-level Slack config — currently ONLY the on/off `paused` toggle.

    Accepts ONLY ``{"paused": true|false}`` (plus an optional ``etag`` for
    optimistic concurrency). When paused, the scan / classify / draft worker loops
    skip their work (gated INSIDE each loop, never by stopping the tasks) while the
    file-watcher stays alive so toggling back is instant. Etag-guarded exactly like
    save_draft/dismiss_item; broadcasts 'slack_changed' so an open page can show or
    hide its "Paused" badge live. NEVER sends.
    """
    body = await read_json_body(request)
    expected_etag = body.get("etag")
    if "paused" not in body:
        raise web.HTTPBadRequest(reason="paused field required")

    data, current_etag = await _load_async()
    data["paused"] = bool(body["paused"])

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"paused": data["paused"]})
    return web.json_response({"paused": data["paused"], "etag": new_etag})


async def refresh_queue(request: web.Request) -> web.Response:
    """Manual Refresh — trigger a REAL in-process scan, non-blocking (D-023).

    NEW in-process trigger contract (replaces the retired ``.slack_scan_now`` cron
    sentinel of D-013): Refresh wakes the in-process ``_scan_worker`` to run the
    SAME deterministic ``_scan_once`` work the periodic tick does, sharing ONE
    scan-in-progress guard so a manual scan and the system scan never overlap:

      * If a scan is ALREADY in progress (the shared guard is held — a periodic or
        a previous manual scan is mid-flight), this is a no-op: we DISMISS the new
        trigger and return HTTP 200 ``{scanning: false, reason: "A scan is already
        in progress."}`` (a deliberately NON-error shape the page surfaces briefly
        instead of the "Scanning…" hint). ``reason`` is a plain, secret-free string.

      * Otherwise we SET the wake Event (interrupting the worker's interval sleep so
        the scan starts within ~instantly, not after the full ``SLACK_SCAN_INTERVAL_S``)
        and return IMMEDIATELY with HTTP 202 ``{scanning: true, etag: <warm etag>}``
        WITHOUT awaiting the scan — the warmed results repaint via the worker's
        ``slack_changed`` broadcast → useLiveUpdates → GET /queue.

    Either way we return the CURRENT warm etag so the client can re-read the
    persisted queue instantly (it always sees current state right away).
    """
    _, etag = await _load_async()

    # A scan already running? Dismiss the manual trigger with a clear, non-error
    # message (the guard is the single mutual-exclusion point shared with the
    # worker — see _run_guarded_scan).
    if _get_scan_lock().locked():
        return web.json_response(
            {"scanning": False, "reason": _scrub("A scan is already in progress.")},
            status=200,
        )

    # Wake the worker's interval sleep so a real scan starts promptly. We do NOT
    # run the scan inline (the request must not block for the MCP scan's duration);
    # the worker picks up the Event and runs _run_guarded_scan → _scan_once.
    _get_scan_event().set()
    return web.json_response({"scanning": True, "etag": etag}, status=202)


# ── Read-only file-watcher (broadcasts cron writes; spawns nothing) ───────────


async def _slack_watcher(app) -> None:
    """The single in-process file-watcher — broadcasts cron writes, never scans.

    Every SLACK_WATCH_INTERVAL_S it STAT/etag-reads slack_threads.json via
    ``filestore.read_json`` (the etag is mtime_ns-based) and, when the etag moves
    vs the last-seen value, broadcasts 'slack_changed' over the existing ws manager
    so any open page repaints. The cron writes the file from inside its tmux REPL,
    so the server otherwise never sees that write — this is what makes a cron-only
    scan still feel live.

    It is PURE local-file I/O: it NEVER spawns a subprocess and NEVER scans Slack.
    The baseline etag is seeded on the first tick so startup does not emit a
    spurious broadcast. Robustness mirrors the retired poller's contract:
    CancelledError propagates so shutdown can cancel+await cleanly; a per-iteration
    try/except catches+logs (scrubbed) so one bad read never kills the loop.
    """
    last_etag: str | None = None
    seeded = False
    while True:
        try:
            await asyncio.sleep(SLACK_WATCH_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        try:
            _, etag = filestore.read_json(SLACK_PATH)
            if not seeded:
                # First tick seeds the baseline — no broadcast on startup.
                last_etag = etag
                seeded = True
            elif etag != last_etag:
                last_etag = etag
                await app["ws_manager"].broadcast("slack_changed", {"watched": True})
            mark_worker_run(app, 'Slack Watcher')
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one bad read must not kill the loop
            logger.warning("Slack file-watcher iteration failed: %s", _scrub(str(e)))


async def _start_watcher(app) -> None:
    """on_startup: launch the single read-only file-watcher task (idempotent)."""
    try:
        task = app.get("slack_watch_task")
        if task is None or task.done():
            app["slack_watch_task"] = asyncio.create_task(_slack_watcher(app))
            register_worker(app, 'Slack Watcher', 'slack_watch_task', SLACK_WATCH_INTERVAL_S, project='claude-web', description='Pushes live updates to the browser when the Slack queue changes')
    except Exception:
        logger.error("Failed to start Slack watcher", exc_info=True)
        raise


async def _stop_watcher(app) -> None:
    """on_cleanup: cancel AND await the watcher so it tears down cleanly.

    Mirrors the retired poller's teardown shape (the contract that kept the suite
    warning-free). The watcher holds no subprocess, so there is nothing to reap —
    only the task to cancel and await.
    """
    task = app.pop("slack_watch_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── In-process draft-worker pool (D-015) ──────────────────────────────────────
#
# The cron writes draft-free SKELETON items (status 'needs-draft') and flags
# existing items that grew new messages (needsRedraft). This worker drafts them
# IN PARALLEL via the read-only 'draft' seam, capped by a Semaphore. It is the
# read-only enrichment relaxation of D-013's "server never spawns on an interval"
# clause (D-015): it spawns no SCAN, mints no new items, and only fills in drafts
# on items the cron already wrote. The SEND path stays exclusively user-triggered.


def _needs_draft(item: dict) -> bool:
    """True if this item is awaiting a (re-)draft from the worker.

    Two cases (D-015):
      * a brand-new SKELETON the cron wrote (status 'needs-draft'); OR
      * an existing 'needs-review' item the cron flagged needsRedraft because the
        conversation grew new messages.
    EDITED-ITEM GUARD (defense in depth): never re-draft an item whose draft was
    manually edited (draft != generatedDraft) — the user owns it. The cron already
    won't set needsRedraft on an 'edited' item, but we re-check here so a stray
    needsRedraft on a user-edited needs-review item can never clobber their text.
    """
    status = item.get("status")
    if status == "needs-draft":
        return True
    if status == "needs-review" and item.get("needsRedraft"):
        draft = str(item.get("draft", ""))
        generated = str(item.get("generatedDraft", ""))
        # Only re-draft a machine-generated (untouched) draft.
        return draft.strip() == generated.strip()
    return False


async def _draft_one(app, sem: asyncio.Semaphore, item_id: str, prompt: str,
                     history3d: list | None = None, channel_id: str = "") -> None:
    """Draft ONE item via the MCP-FREE seam, then persist atomically (idempotent).

    Concurrency-bounded by ``sem``. COLD-START INVARIANT (D-022): the draft action
    NEVER cold-starts slack-mcp. If the item has no usable ``history3d`` yet, we
    fetch it FIRST on the PERSISTENT warm-auth session via ``_fetch_history_for``
    (zero extra SAML auth) and rebuild the prompt so the subprocess drafts from the
    fetched history. If the channel is unreadable (history fetch returns []), we
    STILL draft from the snippet alone — we NEVER fall back to a cold-auth MCP
    subprocess just to draft text.

    On an unavailable seam result the item is left AS-IS (retried next cycle). On
    success we RE-READ the sidecar fresh, re-locate the item by id, apply the
    scrubbed+capped draft/history3d/generatedDraft, flip status to 'needs-review',
    clear needsRedraft, and write against the FRESH etag.
    """
    async with sem:
        # MCP-FREE history pre-fetch (D-022): if we don't already have history,
        # fetch it on the PERSISTENT session and re-render the prompt with it so the
        # draft subprocess never needs to cold-start slack-mcp. An empty result
        # (unreadable channel) is fine — we draft from the snippet alone.
        if not history3d and channel_id:
            fetched = await _fetch_history_for(channel_id)
            if fetched:
                history3d = fetched
                data, _ = _load()
                cur = next((it for it in data["items"] if it.get("id") == item_id), None)
                if cur is not None:
                    cur = dict(cur)
                    cur["history3d"] = history3d
                    prompt = build_draft_prompt(cur)

        payload = {"id": item_id, "prompt": prompt}
        if history3d:
            payload["history3d"] = history3d
        result = await _run_slack_agent("draft", payload)

    if not result.get("available"):
        # Honest failure — leave the item untouched so it retries next cycle.
        logger.info(
            "Slack draft-worker: item %s draft unavailable: %s",
            item_id, _scrub(str(result.get("reason", "")))[:200],
        )
        return

    new_draft = _scrub(str(result.get("draft", "")))[:_DRAFT_CAP]
    if not new_draft.strip():
        # An empty draft is not useful — treat like unavailable and retry later.
        return

    # generatedDraft defaults to the draft (it's the machine baseline). history3d
    # is scrubbed per-field and capped (newest kept) — never trust the subprocess
    # to bound it for us.
    generated = _scrub(str(result.get("generatedDraft", new_draft)))[:_DRAFT_CAP]
    history3d = _sanitize_history3d(result.get("history3d"))

    # RE-READ fresh right before the write so a concurrent cron/watcher/approve that
    # touched the file doesn't make us clobber their change; re-locate by id.
    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if item is None:
        # The item vanished (e.g. dismissed/sent) between select and persist —
        # nothing to draft into. Idempotent no-op.
        return

    # EDITED-ITEM GUARD re-checked against the FRESH item: if it was edited or sent
    # under us, do NOT overwrite the user's draft. Refresh history3d for context
    # only (so they see the new messages) and clear needsRedraft without touching
    # the draft (spec item 4: edited items keep their draft; needsRedraft is not
    # re-set, but if it's lingering we clear it so the worker stops re-selecting).
    status = item.get("status")
    cur_draft = str(item.get("draft", ""))
    cur_generated = str(item.get("generatedDraft", ""))
    if status == "sent":
        return
    if status == "edited" or cur_draft.strip() != cur_generated.strip():
        # The user edited this draft under us — leave their text ALONE. Only
        # refresh history3d (so the new messages are visible) and clear any
        # lingering needsRedraft so the worker stops re-selecting it.
        if history3d:
            item["history3d"] = history3d
        item.pop("needsRedraft", None)
    else:
        # Machine-generated (untouched) draft — safe to (re)draft.
        item["draft"] = new_draft
        item["generatedDraft"] = generated
        if history3d:
            item["history3d"] = history3d
        thread_ctx = result.get("threadContext", "")
        if isinstance(thread_ctx, str) and thread_ctx.strip():
            item["threadContext"] = thread_ctx.strip()
        item["status"] = "needs-review"
        item.pop("needsRedraft", None)

    try:
        filestore.write_json(SLACK_PATH, data, current_etag)
    except filestore.ConflictError:
        # Another writer won — idempotent by construction: the next cycle re-reads
        # and either finds the item already drafted or re-drafts harmlessly.
        return

    await app["ws_manager"].broadcast("slack_changed", {"id": item_id, "drafted": True})


def _sanitize_history3d(raw) -> list:
    """Scrub + cap a history3d array the draft subprocess returned.

    Keeps the newest ``_HISTORY3D_CAP`` entries, drops non-dict entries, scrubs
    each text/author field, and caps the text length — defensive (the subprocess
    is instructed to bound it, but we never trust an external array to be bounded).
    """
    if not isinstance(raw, list):
        return []
    out = []
    for msg in raw[-_HISTORY3D_CAP:]:
        if not isinstance(msg, dict):
            continue
        out.append({
            "ts": msg.get("ts"),
            "author": _scrub(str(msg.get("author", "")))[:200],
            "text": _scrub(str(msg.get("text", "")))[:_HISTORY3D_TEXT_CAP],
        })
    return out


async def _draft_pending(app, sem: asyncio.Semaphore) -> None:
    """Draft every ``needs-draft`` item currently on disk, in parallel (D-026).

    The shared body of the draft pipeline: the periodic ``_draft_worker`` calls it
    each (unpaused) cycle, and the manual-scan path calls it directly so a
    user-triggered scan drafts its freshly-classified items even while the periodic
    workers are paused. ``_draft_one`` re-reads and writes per item under the etag
    guard, so concurrent callers stay safe. No-op when nothing is pending.
    """
    data, _ = _load()
    # Snapshot the (id, prompt, history, channel) tuples to draft. build_draft_prompt
    # reads the item's history3d/needsRedraft to choose a fresh-draft vs re-draft
    # prompt; we build it now against the current item view.
    pending = [
        (it.get("id"), build_draft_prompt(it),
         None if it.get("needsRedraft") else it.get("history3d"),
         it.get("channelId", ""))
        for it in data["items"]
        if it.get("id") and _needs_draft(it)
    ]
    if not pending:
        return
    await asyncio.gather(
        *(_draft_one(app, sem, item_id, prompt, history3d, channel_id)
          for item_id, prompt, history3d, channel_id in pending)
    )


async def _draft_worker(app) -> None:
    """The in-process draft-worker (D-015): polls for items needing a draft and
    drafts them in PARALLEL via the read-only seam, capped by a Semaphore.

    Mirrors ``_slack_watcher``'s lifecycle EXACTLY (the shape that keeps the suite
    warning-free): the FIRST action each loop is ``await asyncio.sleep(interval)``
    so it is INERT on startup — critical because app.on_startup fires under
    aiohttp_client(app) in EVERY test, so the worker must not spawn anything until
    an interval elapses AND ``_probe_readiness()`` reports ready (the same gate the
    retired poller used so the suite never spawns a real `claude`). CancelledError
    propagates so shutdown can cancel+await cleanly; a per-iteration try/except
    catches+logs (scrubbed) so one bad cycle never kills the loop.
    """
    sem = asyncio.Semaphore(SLACK_DRAFT_CONCURRENCY)
    while True:
        try:
            await asyncio.sleep(SLACK_DRAFT_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        try:
            # Gate on readiness so we never spawn a real `claude` when the seam
            # can't work (and so the test suite — claude absent — never spawns).
            if not _probe_readiness().get("ready"):
                continue

            # PAUSE GATE (D-025): when the queue is paused, skip drafting this cycle
            # (the file-watcher stays alive so un-pausing is instant). Checked INSIDE
            # the loop after the readiness gate — never by stopping the task.
            data, _ = _load()
            if data.get("paused"):
                logger.info("Slack draft-worker: paused, skipping this cycle.")
                continue

            await _draft_pending(app, sem)
            mark_worker_run(app, 'Slack Draft Worker')
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the loop
            logger.warning("Slack draft-worker iteration failed: %s", _scrub(str(e)))


async def _start_draft_worker(app) -> None:
    """on_startup: launch the single draft-worker task (idempotent)."""
    try:
        task = app.get("slack_draft_task")
        if task is None or task.done():
            app["slack_draft_task"] = asyncio.create_task(_draft_worker(app))
            register_worker(app, 'Slack Draft Worker', 'slack_draft_task', SLACK_DRAFT_POLL_INTERVAL_S, project='claude-web', description='Generates draft replies for Slack messages awaiting your review')
    except Exception:
        logger.error("Failed to start Slack draft worker", exc_info=True)
        raise


async def _stop_draft_worker(app) -> None:
    """on_cleanup: cancel AND await the draft-worker so it tears down cleanly.

    Mirrors ``_stop_watcher``. Each in-flight ``_run_slack_agent('draft', ...)``
    subprocess is reaped by ``_drive_agent``'s own finally (terminate→kill) when
    its awaiting task is cancelled, so no `claude` orphan survives shutdown.
    """
    task = app.pop("slack_draft_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── In-process classify-worker (D-025) ────────────────────────────────────────
#
# A freshly-scanned skeleton lands in 'needs-classify'. This worker batches those
# skeletons through ONE read-free `claude --print` LLM call (the MCP-FREE 'classify'
# action) and routes each: 'needs-reply'/'actionable' → 'needs-draft' (the unchanged
# draft worker picks them up), 'fyi' → 'fyi' (terminal, no draft generated). It is a
# read-only, single-stage classifier — it spawns no SCAN, mints no new items, and
# sends nothing — sitting BETWEEN the scan and the draft worker.

# D-027: a classification NO LONGER changes the lifecycle status — EVERY classified
# item flows to 'needs-draft' so it gets thread context + a draft regardless of
# label. The label is recorded in the item's ``classification`` field (a review-
# grouping hint), not baked into the status. We keep this set purely to VALIDATE
# the untrusted label coming back from the seam.
_CLASSIFY_NEXT_STATUS = {
    "needs-reply": "needs-draft",
    "actionable": "needs-draft",
    "fyi": "needs-draft",
}


async def _classify_one(app, batch: list[dict]) -> None:
    """Classify ONE batch of needs-classify items via the MCP-FREE seam.

    Calls ``_run_slack_agent('classify', ...)`` ONCE for the whole batch. On an
    unavailable result the items are left AS-IS (retried next cycle — fail-loud, no
    fabrication). On success we RE-READ the sidecar fresh, re-locate each item by id,
    and flip its status per the classification (needs-reply/actionable → needs-draft;
    fyi → fyi). Items whose id is missing from the reply, or that were touched in the
    meantime (no longer 'needs-classify'), are left alone. Writes against the FRESH
    etag; a ConflictError just skips (idempotent — the next cycle re-reads).
    """
    payload_items = [
        {
            "id": it.get("id"),
            "sender": it.get("sender", ""),
            "channel": it.get("channel", ""),
            "snippet": it.get("snippet", ""),
        }
        for it in batch
    ]
    result = await _run_slack_agent("classify", {"items": payload_items})

    if not result.get("available"):
        # Honest failure — leave the items untouched so they retry next cycle.
        logger.info(
            "Slack classify-worker: batch unavailable: %s",
            _scrub(str(result.get("reason", "")))[:200],
        )
        return

    # Build an id → (label, next-status) map from the (untrusted) classifications.
    # Unknown labels are ignored (the item stays needs-classify and retries next
    # cycle). D-027: the label is RECORDED on the item; the status always becomes
    # needs-draft so every item gets context + a draft regardless of label.
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

    # RE-READ fresh right before the write so a concurrent scan/draft/approve that
    # touched the file doesn't make us clobber their change; re-locate each by id.
    data, current_etag = _load()
    changed = False
    for item in data["items"]:
        iid = str(item.get("id", "") or "")
        # Only act on items STILL awaiting classification (a concurrent dismiss/edit
        # may have moved it on); never override a non-needs-classify status.
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
        filestore.write_json(SLACK_PATH, data, current_etag)
    except filestore.ConflictError:
        # Another writer won — idempotent: the next cycle re-reads and re-classifies
        # any item still in needs-classify.
        return

    await app["ws_manager"].broadcast("slack_changed", {"classified": True})


async def _classify_pending(app) -> None:
    """Classify every ``needs-classify`` item currently on disk, batched (D-026).

    The shared body of the classify pipeline: the periodic ``_classify_worker``
    calls it each (unpaused) cycle, and the manual-scan path calls it directly so a
    user-triggered scan triages its fresh skeletons even while the periodic workers
    are paused. Re-reads inside ``_classify_one`` per batch, so it is safe to call
    from either caller. No-op when nothing is pending.
    """
    data, _ = _load()
    pending = [
        it for it in data["items"]
        if it.get("id") and it.get("status") == "needs-classify"
    ]
    if not pending:
        return
    # Batch up to SLACK_CLASSIFY_BATCH items per LLM call, ONE call per batch.
    for start in range(0, len(pending), SLACK_CLASSIFY_BATCH):
        await _classify_one(app, pending[start:start + SLACK_CLASSIFY_BATCH])


async def _classify_worker(app) -> None:
    """The in-process classify-worker (D-025): batches needs-classify skeletons.

    Mirrors ``_draft_worker``'s lifecycle EXACTLY (the shape that keeps the suite
    warning-free): the FIRST action each loop is ``await asyncio.sleep(interval)`` so
    it is INERT on startup — critical because app.on_startup fires under
    aiohttp_client(app) in EVERY test, so the worker must not spawn anything until an
    interval elapses AND ``_probe_readiness()`` reports ready (the same gate the draft
    worker uses so the suite never spawns a real `claude`). Honors the PAUSE gate
    (D-025) INSIDE the loop. CancelledError propagates so shutdown can cancel+await
    cleanly; a per-iteration try/except catches+logs (scrubbed) so one bad cycle
    never kills the loop.
    """
    while True:
        try:
            await asyncio.sleep(SLACK_CLASSIFY_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        try:
            # Gate on readiness so we never spawn a real `claude` when the seam
            # can't work (and so the test suite — claude absent — never spawns).
            if not _probe_readiness().get("ready"):
                continue

            data, _ = _load()
            # PAUSE GATE (D-025): skip classification while paused (the file-watcher
            # stays alive so un-pausing is instant). Checked INSIDE the loop.
            if data.get("paused"):
                logger.info("Slack classify-worker: paused, skipping this cycle.")
                continue

            await _classify_pending(app)
            mark_worker_run(app, 'Slack Classify Worker')
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the loop
            logger.warning("Slack classify-worker iteration failed: %s", _scrub(str(e)))


async def _start_classify_worker(app) -> None:
    """on_startup: launch the single classify-worker task (idempotent)."""
    try:
        task = app.get("slack_classify_task")
        if task is None or task.done():
            app["slack_classify_task"] = asyncio.create_task(_classify_worker(app))
            register_worker(app, 'Slack Classify Worker', 'slack_classify_task', SLACK_CLASSIFY_POLL_INTERVAL_S, project='claude-web', description='Sorts new Slack messages into actionable vs. ignorable')
    except Exception:
        logger.error("Failed to start Slack classify worker", exc_info=True)
        raise


async def _stop_classify_worker(app) -> None:
    """on_cleanup: cancel AND await the classify-worker so it tears down cleanly.

    Mirrors ``_stop_draft_worker``. Each in-flight ``_run_slack_agent('classify',
    ...)`` subprocess is reaped by ``_drive_agent``'s own finally (terminate→kill)
    when its awaiting task is cancelled, so no `claude` orphan survives shutdown.
    """
    task = app.pop("slack_classify_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── Deterministic scan worker (D-018) ─────────────────────────────────────────
#
# Replaces the cron's LLM-driven scan with deterministic in-process Python: call
# two read tools (list_dms + get_unreads) DIRECTLY over MCP, normalize datamarks,
# dedupe by EXACT channelId in Python, write needs-draft skeletons. No LLM, no
# `claude` subprocess. The unchanged _draft_worker then drafts those skeletons.
# These are the IN-FLIGHT statuses an item can be in while still representing an
# unresolved conversation; a candidate for a channelId already in one of them is a
# duplicate and is not re-appended. 'needs-classify' (D-025) is included so the
# scan dedupes against an in-flight-classify skeleton.
#
# 'dismissed' is deliberately ABSENT (D-028): Dismiss means "clear THIS message",
# not "mute this conversation forever" (that's what Mute is for). A dismissed
# conversation that grows a NEWER message earns a fresh skeleton — handled in
# _merge_scan_candidates exactly like a 'sent' item (suppress only while no
# activity newer than the dismiss). Keeping 'dismissed' here was the bug: a single
# dismiss silently deafened the channel to all future messages until the item
# aged out. 'fyi' is likewise absent (it is a classification, not a status).
_SCAN_ACTIVE_STATUSES = {
    "needs-classify", "needs-draft", "needs-review", "edited",
}


def _normalize_datamarks(value):
    """Replace the U+E000 datamark sentinel with a space — AFTER json parse only.

    Applied to extracted STRING fields (recursively through dict/list) once the
    payload is already parsed. Normalizing the raw JSON BEFORE parse corrupts
    escape sequences (the known LLM-scan trap, D-012); doing it on parsed strings
    is safe and deterministic.
    """
    if isinstance(value, str):
        return value.replace(_DATAMARK_SENTINEL, " ")
    if isinstance(value, list):
        return [_normalize_datamarks(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_datamarks(v) for k, v in value.items()}
    return value


def _unwrap_list(payload, *keys) -> list:
    """Best-effort: pull a list out of a Slack read-tool payload.

    Slack tools return either a bare list, or a dict wrapping the list under one of
    several keys (``dms``/``conversations``/``items``/…). We never trust the exact
    shape, so we try a bare list, then the supplied keys, then any first list value
    — returning [] (never raising) when nothing list-shaped is found.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return val
        for val in payload.values():
            if isinstance(val, list):
                return val
    return []


def _as_int_ms(value) -> int:
    """Coerce a Slack timestamp (epoch ms int, or '1718.5' epoch-seconds str) to ms.

    Slack ``ts`` strings are epoch SECONDS with a fractional part; ``lastActivity``
    is usually epoch MILLIS. We accept either: a value that already looks like ms
    (>= ~year-2001 in ms) is kept; a smaller float/str is treated as epoch seconds
    and scaled. Returns 0 on anything unparseable (never raises) so a missing/odd
    timestamp simply doesn't pass the recency window rather than crashing the scan.
    """
    if isinstance(value, bool):  # bool is an int subclass — exclude it explicitly
        return 0
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            n = float(s)
        except ValueError:
            # ISO 8601 datetime (e.g. "2026-06-17T21:29:52.773Z" from list_dms)
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except (ValueError, OSError):
                return 0
    else:
        return 0
    if n <= 0:
        return 0
    # Epoch-seconds values are ~1e9; epoch-millis are ~1e12. Scale up sub-1e11.
    return int(n) if n >= 1e11 else int(n * 1000)


def _mpdm_first_other(name: str, channel: str) -> str:
    """Derive a sender label for a group DM from its mpdm slug / name.

    Group-DM names look like ``mpdm-alice--bob--carol-1`` (Slack's MPIM slug) or a
    comma/plus list of display names. We return the FIRST participant token, since
    the worker has no self-identity to filter against and does not call get_messages
    (kept fast). Falls back to the raw channel string when nothing parses.
    """
    raw = (name or "").strip()
    if raw.startswith("mpdm-"):
        raw = raw[len("mpdm-"):]
        # Trailing "-1" style suffix is a uniquifier, not a participant.
        raw = re.sub(r"-\d+$", "", raw)
        parts = [p for p in raw.split("--") if p]
        if parts:
            return parts[0]
    for sep in (",", "+", "/"):
        if sep in raw:
            first = raw.split(sep)[0].strip()
            if first:
                return first
    return raw or (channel or "")


def _scan_window_start_ms(now_ms: int) -> int:
    """The lastActivity cutoff for this scan cycle.

    Uses the worker's tracked last-scan timestamp (minus a small overlap) when it
    has one; otherwise a fixed window back from now. The worker owns this state
    because it has no cron ``lastFiredAt`` to read.
    """
    if _LAST_SCAN_TS_MS is not None:
        return max(0, _LAST_SCAN_TS_MS - _SCAN_OVERLAP_MS)
    return now_ms - _SCAN_WINDOW_MS


def _dm_candidates(dms_payload, now_ms: int) -> list[dict]:
    """Build DM / group-DM scan candidates from a parsed list_dms payload.

    Each candidate is a normalized dict {channelId, userId, channelType, sender,
    channel, snippet, ts}. A DM/group-DM qualifies when its lastActivity is newer
    than the scan window cutoff. channelId/userId are captured VERBATIM (routing
    ids, not secrets); sender/channel/snippet are datamark-normalized + scrubbed.
    """
    cutoff = _scan_window_start_ms(now_ms)
    out: list[dict] = []
    for raw in _unwrap_list(dms_payload, "dms", "conversations", "ims", "items"):
        if not isinstance(raw, dict):
            continue
        dm = _normalize_datamarks(raw)
        channel_id = str(dm.get("channelId") or dm.get("id") or dm.get("channel") or "").strip()
        if not channel_id:
            continue  # no routable id → skip (the send path would fail loud anyway)

        last_activity = _as_int_ms(
            dm.get("lastActivity")
            if dm.get("lastActivity") is not None
            else dm.get("ts")
        )
        if last_activity <= cutoff:
            continue

        is_group = bool(dm.get("isGroup") or dm.get("is_mpim") or dm.get("isMpdm"))
        name = str(dm.get("name") or dm.get("username") or "")
        channel = str(dm.get("channel") or dm.get("name") or "")
        if is_group:
            channel_type = "group_dm"
            sender = _mpdm_first_other(name, channel)
            channel = channel or name
            user_id = ""
        else:
            channel_type = "dm"
            sender = name or str(dm.get("user") or "")
            channel = channel or (f"DM with {sender}" if sender else channel_id)
            user_id = str(dm.get("userId") or dm.get("user") or "").strip()

        out.append({
            "channelId": channel_id,
            "userId": user_id,
            "channelType": channel_type,
            "sender": _scrub(sender)[:_SNIPPET_CAP],
            "channel": _scrub(channel)[:_SNIPPET_CAP],
            "snippet": _scrub(str(dm.get("latest") or dm.get("snippet") or ""))[:_SNIPPET_CAP],
            "ts": last_activity,
        })
    return out


def _mention_candidates(unreads_payload) -> list[dict]:
    """Build scan candidates from the get_unreads payload.

    get_unreads returns ALL channels with unread messages — including group DMs
    that list_dms may not surface (Slack's DM list omits channels removed from
    the sidebar). So this is the COMPLEMENT to _dm_candidates, not a subset.

    Entries have a nested `messages` array with the actual content; top-level
    fields like sender/ts/snippet are absent. We extract from the most recent
    message. mpdm-* channels are classified as group_dm (not mention) so the
    UI renders them correctly.
    """
    norm = _normalize_datamarks(unreads_payload)
    # Collect channel entries from ALL list-valued sections of the get_unreads
    # payload. The Slack MCP server may return group DMs under "dms", regular
    # channels/mentions under "channels" or "mentions", etc. — and the key varies
    # across server versions. Checking only a fixed list of keys silently drops any
    # section whose key is not in the list (confirmed root cause: group DMs returned
    # under "dms" were never processed). Deduplication by channelId below ensures a
    # channel appearing in multiple sections is added only once.
    section: list = []
    seen_section_ids: set[str] = set()
    if isinstance(norm, dict):
        for val in norm.values():
            if isinstance(val, list):
                for entry in val:
                    if not isinstance(entry, dict):
                        continue
                    cid = str(entry.get("channelId") or entry.get("id") or entry.get("channel") or "").strip()
                    if cid and cid not in seen_section_ids:
                        seen_section_ids.add(cid)
                        section.append(entry)
    elif isinstance(norm, list):
        for entry in norm:
            if isinstance(entry, dict):
                cid = str(entry.get("channelId") or entry.get("id") or entry.get("channel") or "").strip()
                if cid and cid not in seen_section_ids:
                    seen_section_ids.add(cid)
                    section.append(entry)

    out: list[dict] = []
    for raw in section:
        if not isinstance(raw, dict):
            continue
        channel_id = str(raw.get("channelId") or raw.get("id") or raw.get("channel") or "").strip()
        if not channel_id:
            continue

        name = str(raw.get("name") or raw.get("channel") or channel_id)

        # Classify: mpdm-* names are group DMs, D-prefixed channelIds are 1:1 DMs.
        is_group = "mpdm-" in name or raw.get("isGroup") or raw.get("is_mpim")
        is_dm = (not is_group) and channel_id.startswith("D")
        if is_group:
            channel_type = "group_dm"
        elif is_dm:
            channel_type = "dm"
        else:
            channel_type = "mention"

        messages = raw.get("messages") or []

        if channel_type == "mention":
            # For channels, only include if there's a message that actually
            # mentions the user. Use that specific message for sender/snippet/ts.
            mention_msg = None
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                text = str(msg.get("text", ""))
                if _SELF_MENTION_RE.search(text):
                    mention_msg = msg
                    break
            if mention_msg is None:
                continue
            sender = str(mention_msg.get("user") or mention_msg.get("sender") or "")
            if isinstance(mention_msg.get("user"), dict):
                sender = mention_msg["user"].get("name", "")
            snippet_raw = str(mention_msg.get("text", ""))
            ts_raw = mention_msg.get("ts")
        else:
            # DMs/group DMs: use the most recent message.
            latest_msg = messages[-1] if messages else {}
            sender = str(
                raw.get("sender") or raw.get("user") or raw.get("author")
                or latest_msg.get("user") or latest_msg.get("sender") or ""
            )
            snippet_raw = (
                raw.get("latest") or raw.get("text") or raw.get("snippet")
                or latest_msg.get("text") or ""
            )
            ts_raw = raw.get("ts") or raw.get("lastActivity") or latest_msg.get("ts")

        out.append({
            "channelId": channel_id,
            "userId": str(raw.get("userId") or raw.get("user") or "").strip(),
            "channelType": channel_type,
            "sender": _scrub(sender)[:_SNIPPET_CAP],
            "channel": _scrub(name)[:_SNIPPET_CAP],
            "snippet": _scrub(_resolve_mentions(_strip_slack_xml(str(snippet_raw))))[:_SNIPPET_CAP],
            "ts": _as_int_ms(ts_raw),
        })
    return out


def _unread_channel_ids(unreads_payload, dms_payload) -> set:
    """Return the authoritative set of channel IDs Slack currently considers unread.

    This combines ALL channels from the raw get_unreads payload (every list-valued
    section, same iteration pattern as _mention_candidates) with ALL channels from
    the raw list_dms payload (without the timestamp-window cutoff that _dm_candidates
    applies).  The result is the COMPLETE set of channels Slack reports as having
    unread messages — used by the auto-dismiss logic to detect items whose channel
    is no longer unread.

    IMPORTANT: this reads from the RAW payloads, NOT from the filtered candidates
    list.  The candidates list has bot/self-sender filtering and timestamp-window
    filtering applied, so using it would incorrectly auto-dismiss channels that are
    still unread but filtered (e.g. bot channels, old-but-still-unread DMs).
    """
    ids: set = set()

    # --- get_unreads payload: normalize datamarks, iterate all list-valued sections ---
    norm = _normalize_datamarks(unreads_payload)
    if isinstance(norm, dict):
        for val in norm.values():
            if isinstance(val, list):
                for entry in val:
                    if not isinstance(entry, dict):
                        continue
                    cid = str(entry.get("channelId") or entry.get("id") or entry.get("channel") or "").strip()
                    if cid:
                        ids.add(cid)
    elif isinstance(norm, list):
        for entry in norm:
            if isinstance(entry, dict):
                cid = str(entry.get("channelId") or entry.get("id") or entry.get("channel") or "").strip()
                if cid:
                    ids.add(cid)

    # --- list_dms payload: unwrap without the timestamp-window cutoff ---
    for entry in _unwrap_list(dms_payload, "dms", "conversations", "ims", "items"):
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("channelId") or entry.get("id") or entry.get("channel") or "").strip()
        if cid:
            ids.add(cid)

    return ids


def _skeleton_for(cand: dict) -> dict:
    """A fresh needs-classify SKELETON item from a scan candidate.

    Matches the existing item shape; snippet is carried (the classify worker reads
    it, then the draft worker fills the real draft). channelId/userId are verbatim
    routing ids. A fresh 8-hex id like the rest of the queue.

    D-025: a freshly-scanned skeleton lands in 'needs-classify', NOT 'needs-draft'.
    The classify worker batches it through ONE LLM call and routes it to
    'needs-draft' (needs-reply / actionable) or 'fyi' (terminal); only then does the
    draft worker draft it.
    """
    return {
        "id": secrets.token_hex(4),
        "sender": cand.get("sender", ""),
        "channel": cand.get("channel", ""),
        "channelId": cand.get("channelId", ""),
        "userId": cand.get("userId", ""),
        "channelType": cand.get("channelType", "dm"),
        "snippet": cand.get("snippet", ""),
        "status": "needs-classify",
        "ts": cand.get("ts", 0),
    }


def _mute_key_for(item_or_cand: dict) -> str:
    """Derive the mute key for a queue item OR a scan candidate (D-025).

    The key is ``channelId`` for DMs/group DMs, and ``channelId + '_' + threadTs``
    ONLY when a non-empty ``threadTs`` field is present.

    DESIGN NOTE (ADR D-025): ``threadTs`` does NOT exist anywhere in the codebase
    today — scan candidates / skeletons carry only ``channelId`` — so this
    GRACEFULLY falls back to the bare ``channelId`` when ``threadTs`` is absent
    (the current reality for every DM / @mention item). When a future scan begins
    capturing a per-thread ``threadTs``, the key automatically becomes
    thread-scoped with no change here. Returns "" when there is no channelId (an
    unroutable item can't be muted).
    """
    channel_id = str(item_or_cand.get("channelId", "") or "").strip()
    if not channel_id:
        return ""
    thread_ts = str(item_or_cand.get("threadTs", "") or "").strip()
    if thread_ts:
        return f"{channel_id}_{thread_ts}"
    return channel_id


def _prune_muted(muted: dict, now_s: float) -> dict:
    """Return a copy of ``muted`` with entries older than the 30-day TTL removed.

    Each value is the unix-SECONDS timestamp the thread was muted; an entry whose
    timestamp is older than ``_MUTE_TTL_S`` ago is dropped so a stale mute can't
    suppress a conversation forever. Non-dict input / unparseable timestamps are
    treated defensively (a junk value is dropped rather than kept indefinitely).
    """
    if not isinstance(muted, dict):
        return {}
    cutoff = now_s - _MUTE_TTL_S
    out: dict = {}
    for key, ts in muted.items():
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        if ts >= cutoff:
            out[key] = ts
    return out


def _merge_scan_candidates(items: list[dict], candidates: list[dict]) -> bool:
    """Merge scan candidates into the queue items IN PLACE; return whether changed.

    Dedupe is EXACT channelId (a group DM 'C…' never matches a 1:1 DM 'D…' that
    shares a participant). For each candidate:
      * If an ACTIVE item (status in _SCAN_ACTIVE_STATUSES) already has that exact
        channelId, it is already represented — EXCEPT a 'needs-review' item whose
        incoming activity is NEWER gets needsRedraft + an updated snippet/ts (the
        same redraft signal the cron used). edited is NEVER touched.
      * A 'sent' item does NOT suppress when the conversation's lastActivity is
        newer than the item's sentAt — a genuinely new message gets a fresh
        skeleton.
      * A 'dismissed' item (D-028) suppresses ONLY while the conversation has no
        activity newer than the dismissed item's ts — Dismiss clears the current
        message, it does not mute the thread (use Mute for that). A newer message
        earns a fresh skeleton, exactly like the 'sent' case.
      * Otherwise it is genuinely new → append a fresh needs-draft skeleton.
    """
    changed = False
    for cand in candidates:
        channel_id = cand.get("channelId", "")
        if not channel_id:
            continue
        cand_ts = _as_int_ms(cand.get("ts"))

        # An active (non-sent) item with this exact channelId already represents it.
        active = next(
            (it for it in items
             if str(it.get("channelId", "")) == channel_id
             and it.get("status") in _SCAN_ACTIVE_STATUSES),
            None,
        )
        if active is not None:
            if (active.get("status") == "needs-review"
                    and cand_ts > _as_int_ms(active.get("ts"))):
                # The conversation grew — flag a redraft (the draft worker re-drafts
                # only an UNTOUCHED machine draft; it leaves an edited draft alone).
                active["needsRedraft"] = True
                if cand.get("snippet"):
                    active["snippet"] = cand["snippet"]
                active["ts"] = cand_ts
                changed = True
            elif (active.get("status") == "needs-review"
                    and not active.get("needsRedraft")
                    and cand_ts == _as_int_ms(active.get("ts"))
                    and cand_ts > 0):
                # History-staleness check: the item's ts matches the scan (no NEW
                # messages) but the fetched history3d doesn't cover up to ts — the
                # draft was generated from incomplete history. Flag a redraft so the
                # worker re-fetches history and re-drafts with full context.
                h3d = active.get("history3d")
                if isinstance(h3d, list) and h3d:
                    last_h_ts = _as_int_ms(h3d[-1].get("ts") if isinstance(h3d[-1], dict) else None)
                    if last_h_ts < cand_ts:
                        # Only flag if draft is machine-generated (untouched).
                        draft = str(active.get("draft", ""))
                        gen = str(active.get("generatedDraft", ""))
                        if draft.strip() == gen.strip():
                            active["needsRedraft"] = True
                            changed = True
            continue

        # No active item — but a 'sent' item suppresses ONLY while the conversation
        # has no newer activity than the send. A newer message earns a fresh card.
        sent = next(
            (it for it in items
             if str(it.get("channelId", "")) == channel_id
             and it.get("status") == "sent"),
            None,
        )
        if sent is not None and cand_ts <= _as_int_ms(sent.get("sentAt")):
            continue

        # Likewise a 'dismissed' item suppresses ONLY while no message is newer
        # than what was dismissed (D-028). Compare against the dismissed item's ts
        # (the last message it represented); a strictly-newer candidate resurfaces
        # the conversation with a fresh skeleton. Dismiss ≠ Mute.
        dismissed = next(
            (it for it in items
             if str(it.get("channelId", "")) == channel_id
             and it.get("status") == "dismissed"),
            None,
        )
        if dismissed is not None and cand_ts <= _as_int_ms(dismissed.get("ts")):
            continue

        items.append(_skeleton_for(cand))
        changed = True
    return changed


def _is_bot_sender(sender: str) -> bool:
    """Return True if the sender is a known bot that never needs a reply."""
    return sender.strip().lower() in _BOT_SENDERS


_SLACK_XML_RE = re.compile(
    r'<slack-user-content[^>]*>(.*?)</slack-user-content>',
    re.DOTALL,
)

# Matches a Slack @mention of the current user (W0187CHBRU0 or U06PZ036D98).
_SELF_MENTION_RE = re.compile(r'<@(?:W0187CHBRU0|U06PZ036D98)>')


_USER_MENTION_RE = re.compile(r'<@([WU][A-Z0-9]+)>')

# In-memory cache of Slack user ID → display name. Populated lazily during scans.
_user_name_cache: dict[str, str] = {}


def _strip_slack_xml(text: str) -> str:
    """Strip <slack-user-content> wrappers and datamark sentinels.

    The U+E000 datamark is Slack's substitution for a SINGLE SPACE (verified
    against raw slack-mcp output: `Soundsgood`), so it MUST be replaced
    with a space — replacing it with "" deletes word boundaries in English text
    ("Thanks, we got" → "Thanks,wegot"). Chinese text has no inter-word spaces so
    carries no datamarks; collapsing runs of spaces keeps it clean either way.
    """
    stripped = _SLACK_XML_RE.sub(r'\1', text)
    stripped = stripped.replace(_DATAMARK_SENTINEL, " ")
    return stripped.strip()


def _resolve_mentions(text: str) -> str:
    """Replace <@WXXXX> mentions with @displayName from cache."""
    def _replace(m):
        uid = m.group(1)
        name = _user_name_cache.get(uid)
        if name:
            return f"@{name}"
        return m.group(0)
    return _USER_MENTION_RE.sub(_replace, text)


def _build_thread_context(history3d: list) -> list:
    """Build a threadContext array from the full history3d."""
    return [
        {"sender": msg.get("author", ""), "text": msg.get("text", ""), "ts": msg.get("ts")}
        for msg in history3d
        if msg.get("text", "").strip()
    ]


async def _fetch_history_for(channel_id: str) -> list:
    """Fetch recent message history for one channel via the persistent MCP session.

    Returns a sanitized history3d array (oldest→newest). On failure returns []
    (non-fatal — the draft worker can still draft from the snippet alone).
    """
    try:
        raw = await _scan_read("get_messages", {"channel": channel_id, "limit": _HISTORY3D_CAP})
    except Exception:  # noqa: BLE001 — non-fatal; empty history still drafts from snippet
        return []
    if not isinstance(raw, dict):
        return []
    messages = raw.get("messages", [])

    # Collect all mentioned user IDs not yet in cache, resolve them in batch.
    all_text = " ".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))
    unknown_ids = {
        uid for uid in _USER_MENTION_RE.findall(all_text)
        if uid not in _user_name_cache
    }
    for uid in unknown_ids:
        await _resolve_user_id(uid)

    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        user = msg.get("user", "")
        if isinstance(user, dict):
            user = user.get("name", user.get("realName", ""))
        text = str(msg.get("text", ""))
        text = _strip_slack_xml(text)
        text = _resolve_mentions(text)
        ts_raw = msg.get("ts") or msg.get("timestamp")
        out.append({
            "ts": _as_int_ms(ts_raw),
            "author": _scrub(str(user))[:200],
            "text": _scrub(text)[:_HISTORY3D_TEXT_CAP],
        })
    # get_messages returns newest-first; we want oldest→newest
    out.reverse()
    return out[-_HISTORY3D_CAP:]


async def _resolve_user_id(uid: str) -> None:
    """Look up one Slack user ID and cache the display name."""
    try:
        raw = await _scan_read("lookup_user", {"query": uid})
        if isinstance(raw, dict):
            name = raw.get("displayName") or raw.get("name") or raw.get("realName") or ""
            if name:
                _user_name_cache[uid] = name
    except Exception:  # noqa: BLE001 — non-fatal
        pass


async def _scan_once(app) -> None:
    """Run ONE deterministic scan: get_unreads → parse → dedupe → fetch history → write.

    Calls get_unreads via the read-only direct-MCP client, which returns ALL
    channels with unread messages (DMs, group DMs, @mentions) with actual message
    content and sender. Filters out bot senders, classifies each channel, dedupes
    by EXACT channelId, fetches 3-day history for new DM/group_dm items (so the
    draft worker doesn't need MCP), and writes skeletons with history attached.
    On a Slack/MCP read failure it FAILS LOUD — never fabricates, never partial-writes.
    """
    global _LAST_SCAN_TS_MS
    now_ms = int(time.time() * 1000)

    unreads_payload = await _scan_read("get_unreads", {})
    candidates = _mention_candidates(unreads_payload)

    # list_dms surfaces DMs that get_unreads misses (cross-workspace enterprise-grid
    # DMs where the two users share no common workspace team are invisible to
    # get_unreads but visible to list_dms). For each list_dms candidate NOT already
    # covered by get_unreads, fetch the latest message to verify: (a) the channel
    # has readable messages, and (b) the last sender is not us.
    dms_payload = await _scan_read("list_dms", {"limit": _SCAN_LIST_DMS_LIMIT})
    dm_cands = _dm_candidates(dms_payload, now_ms)
    seen_ids = {c["channelId"] for c in candidates}
    for dc in dm_cands:
        if dc["channelId"] in seen_ids:
            continue
        cid = dc["channelId"]
        try:
            peek = await _scan_read("get_messages", {"channel": cid, "limit": 1})
            msgs = (peek.get("messages") if isinstance(peek, dict) else peek) or []
            if not msgs:
                continue
            last_msg = msgs[0] if isinstance(msgs, list) else {}
            # user field may be a string (username) or an enriched dict ({name: ...})
            raw_user = last_msg.get("user") or last_msg.get("sender") or ""
            if isinstance(raw_user, dict):
                last_author = raw_user.get("name") or raw_user.get("id") or ""
            else:
                last_author = str(raw_user)
            if _MY_USERNAME and last_author.lower() == _MY_USERNAME:
                continue
            dc["sender"] = _scrub(last_author)[:_SNIPPET_CAP] or dc["sender"]
            dc["snippet"] = _scrub(
                _resolve_mentions(_strip_slack_xml(str(last_msg.get("text", ""))))
            )[:_SNIPPET_CAP] or dc["snippet"]
        except Exception:  # noqa: BLE001 — skip unreadable DMs
            continue
        candidates.append(dc)
        seen_ids.add(cid)

    # Filter out bot senders that never need replies.
    candidates = [c for c in candidates if not _is_bot_sender(c.get("sender", ""))]

    # Filter out conversations where we are the last sender — nothing to reply to.
    candidates = [c for c in candidates if not _MY_USERNAME or c.get("sender", "").lower() != _MY_USERNAME]

    data, current_etag = _load()

    # Thread muting (D-025): prune entries older than the 30-day TTL on EVERY scan
    # cycle (persisted in the same write below), then drop any candidate whose mute
    # key matches a (still-live) muted thread. The drop happens BEFORE the merge so a
    # muted conversation never produces a skeleton. A pruned/empty dict is written
    # back so a stale mute can't suppress a conversation indefinitely.
    now_s = now_ms / 1000.0
    muted = _prune_muted(data.get("mutedThreads") or {}, now_s)
    data["mutedThreads"] = muted
    if muted:
        candidates = [c for c in candidates if _mute_key_for(c) not in muted]

    # Identify which candidates are genuinely NEW (not already in queue) so we
    # can pre-fetch history only for those — avoid redundant MCP calls.
    existing_ids = {
        str(it.get("channelId", ""))
        for it in data["items"]
        if it.get("status") in _SCAN_ACTIVE_STATUSES
    }
    new_dm_candidates = [
        c for c in candidates
        if c.get("channelId") not in existing_ids
    ]

    # Pre-fetch history for new DM/group_dm candidates on the SAME MCP connection.
    # This means the draft worker won't need its own slack-mcp (zero extra auth).
    histories: dict[str, list] = {}
    for cand in new_dm_candidates:
        cid = cand.get("channelId", "")
        if cid:
            histories[cid] = await _fetch_history_for(cid)

    changed = _merge_scan_candidates(data["items"], candidates)

    # Auto-dismiss: items whose channel is no longer unread in Slack (AC-1).
    unread_ids = _unread_channel_ids(unreads_payload, dms_payload)
    for item in data["items"]:
        if (item.get("status") in _SCAN_ACTIVE_STATUSES
                and item.get("channelId")
                and item["channelId"] not in unread_ids):
            item["status"] = "dismissed"
            changed = True

    # Attach pre-fetched history to newly-appended skeletons. Freshly-scanned
    # skeletons land in 'needs-classify' now (D-025), so key the attach on that
    # status — the history rides along through classification into drafting.
    if histories:
        for item in data["items"]:
            cid = item.get("channelId", "")
            if (item.get("status") == "needs-classify"
                    and cid in histories
                    and not item.get("history3d")):
                item["history3d"] = histories[cid]

    _LAST_SCAN_TS_MS = now_ms

    # EVERY completed scan advances freshness, even a no-op that found nothing new
    # (D-023, spec item 4). We persist the top-level ``lastScanAt`` mirror so the
    # "Updated …" label reflects when a scan actually completed (and survives a
    # restart), then broadcast 'slack_changed' so open pages re-render the label
    # and the manual-refresh "Scanning…" hint clears. When nothing changed the
    # items array is byte-identical — only ``lastScanAt`` advances.
    data["lastScanAt"] = now_ms

    try:
        filestore.write_json(SLACK_PATH, data, current_etag)
    except filestore.ConflictError:
        # A concurrent write won — skip this cycle (same fail-safe as before; the
        # next scan re-reads fresh). Freshness still advances on the next cycle.
        return

    # Reuse the existing 'slack_changed' event so the frontend's existing
    # useLiveUpdates path clears the "Scanning…" hint and re-renders freshness —
    # no new broadcast type needed.
    await app["ws_manager"].broadcast("slack_changed", {"scanned": True})


async def _scan_worker(app) -> None:
    """The deterministic in-process scan worker (D-018, D-023).

    Mirrors ``_draft_worker``'s lifecycle EXACTLY (the shape that keeps the suite
    warning-free): each loop FIRST awaits an INTERRUPTIBLE interval sleep — a manual
    Refresh sets the wake Event to start a scan promptly, otherwise the
    ``asyncio.wait_for`` simply times out after ``SLACK_SCAN_INTERVAL_S`` (the
    normal periodic tick). This is inert on startup — critical because
    app.on_startup fires under aiohttp_client(app) in EVERY test: absent a set Event
    AND readiness, the worker behaves EXACTLY as a plain sleep-first loop (the
    wait_for timeout path replaces the bare sleep), so under the harness (no MCP
    server, Event never set) it never scans before the full interval elapses.

    Each cycle gates on ``_probe_readiness()`` (the same gate the draft worker uses
    so the suite never spawns a real MCP server) and runs the scan THROUGH the
    shared guard (``_run_guarded_scan``): if a manual scan is already in progress
    the tick simply SKIPS this cycle (logged, NOT an error — manual and system
    scans never overlap or double-spawn the MCP session). CancelledError propagates
    so shutdown can cancel+await cleanly; a per-iteration try/except catches+logs
    (scrubbed) so one bad cycle never kills the loop.
    """
    event = _get_scan_event()
    sem = asyncio.Semaphore(SLACK_DRAFT_CONCURRENCY)
    while True:
        manual = False
        try:
            # Interruptible sleep: wake early when a manual Refresh sets the Event,
            # else fall through on the normal periodic timeout. A completed wait()
            # (no TimeoutError) means the Event fired — i.e. a MANUAL scan; a
            # TimeoutError is the ordinary periodic tick (NOT an error).
            try:
                await asyncio.wait_for(event.wait(), timeout=SLACK_SCAN_INTERVAL_S)
                manual = True
            except asyncio.TimeoutError:
                manual = False
        except asyncio.CancelledError:
            raise
        # Clear the wake Event at the top of the cycle so a manual Refresh that
        # arrives DURING this scan re-arms a fresh wake for the next cycle.
        event.clear()

        try:
            # Gate on readiness so we never drive a real MCP server when the seam
            # can't work (and so the test suite — claude/MCP absent — never spawns).
            if not _probe_readiness().get("ready"):
                continue
            # PAUSE GATE (D-026): `paused` controls ONLY the periodic tick. A MANUAL
            # scan (the Refresh button → wake Event) ALWAYS runs, even while paused —
            # pause means "stop auto-scanning", not "stop me from scanning on
            # demand". Checked INSIDE the loop — never by stopping the task.
            if not manual:
                data, _ = _load()
                if data.get("paused"):
                    logger.info("Slack scan-worker: paused, skipping this periodic tick.")
                    continue
            # Run under the shared guard: skip (log, not error) if a scan is already
            # in flight so a periodic tick and a manual scan never overlap — the
            # newer trigger is simply dismissed (the Refresh endpoint reports this).
            if not await _run_guarded_scan(app):
                logger.info("Slack scan-worker: a scan is already in progress, skipping this %s.",
                            "manual scan" if manual else "tick")
                continue
            # A MANUAL scan drives the FULL one-time pipeline (D-026): its fresh
            # needs-classify skeletons are classified and the resulting needs-draft
            # items drafted right now, so a user gets ready-to-review drafts even
            # while the periodic classify/draft workers are paused. The periodic tick
            # leaves this to those workers (which run only when unpaused).
            if manual:
                await _classify_pending(app)
                await _draft_pending(app, sem)
            mark_worker_run(app, 'Slack Scanner')
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the loop
            logger.warning("Slack scan-worker iteration failed: %s", _scrub(str(e)))


async def _start_scan_worker(app) -> None:
    """on_startup: launch the single scan-worker task (idempotent).

    Seeds the in-memory ``_LAST_SCAN_TS_MS`` from the persisted ``lastScanAt`` when
    present (D-023) so the activity-window math (``_scan_window_start_ms``) stays
    sane across a restart instead of falling back to the wide first-scan window.
    Best-effort: a missing/odd value leaves it None (the wide-window fallback).
    """
    try:
        global _LAST_SCAN_TS_MS
        if _LAST_SCAN_TS_MS is None:
            try:
                data, _ = _load()
                persisted = data.get("lastScanAt")
                if isinstance(persisted, (int, float)) and not isinstance(persisted, bool):
                    _LAST_SCAN_TS_MS = int(persisted)
            except Exception:  # noqa: BLE001 — seeding is best-effort, never blocks startup
                pass

        task = app.get("slack_scan_task")
        if task is None or task.done():
            app["slack_scan_task"] = asyncio.create_task(_scan_worker(app))
            register_worker(app, 'Slack Scanner', 'slack_scan_task', SLACK_SCAN_INTERVAL_S, project='claude-web', description='Fetches new Slack messages from monitored channels')
    except Exception:
        logger.error("Failed to start Slack scan worker", exc_info=True)
        raise


async def _stop_scan_worker(app) -> None:
    """on_cleanup: cancel AND await the scan-worker so it tears down cleanly.

    Mirrors ``_stop_draft_worker``. The direct-MCP subprocess for an in-flight
    scan is reaped by ``stdio_client``'s context manager when the awaiting task is
    cancelled, so no MCP-server orphan survives shutdown.
    """
    task = app.pop("slack_scan_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _cleanup_persistent_mcp(app) -> None:
    """on_cleanup: teardown the persistent MCP session, reaping the subprocess.

    Registered AFTER _stop_scan_worker in register() so the ordering is:
      1. _stop_scan_worker — cancels the scan-worker task (the consumer of
         call_read_tool), so no new calls will arrive during teardown.
      2. _cleanup_persistent_mcp — terminates the persistent slack-mcp subprocess
         and closes the session, so no orphan process is left.

    Idempotent: if the scan worker's CancelledError already propagated through
    call_read_tool and the session was torn down as part of error handling, or if
    the session was never connected (lazy-init, never used), _disconnect_mcp is a
    no-op.
    """
    await _disconnect_mcp()


async def save_draft(request: web.Request) -> web.Response:
    """Save an edited draft. Editing (text differs from the generated draft)
    flips status to 'edited' — that edit is the preference-learning signal,
    consumed on approve. NEVER sends.
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
    if item.get("status") == "sent":
        raise web.HTTPConflict(reason="item already sent")

    item["draft"] = new_draft
    if new_draft.strip() != str(item.get("generatedDraft", "")).strip():
        item["status"] = "edited"

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"id": item_id})
    return web.json_response({"item": item, "etag": new_etag})


async def dismiss_item(request: web.Request) -> web.Response:
    """SOFT-dismiss one item without sending anything (D-014).

    Sets the found item's status to 'dismissed' IN PLACE — every other field
    (draft, generatedDraft, sender, channel, ts, …) is preserved so the undismiss
    route can restore it. The item is NOT removed from the sidecar, so we broadcast
    'slack_changed' (NOT 'slack_deleted' — the item still exists; nothing emits
    'slack_deleted' from dismiss any more). Etag-guarded like the other mutators.

    Dismissing a 'sent' item is BLOCKED (D-014): a sent item is real outbound
    history, and hiding it in the Dismissed section would misrepresent a send that
    actually went out. We surface 409 rather than silently flipping it. NEVER sends.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") == "sent":
        raise web.HTTPConflict(reason="cannot dismiss a sent item")

    # Soft state flip — preserve every other field so Undo can restore it.
    item["status"] = "dismissed"

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    # Clear unread badge in Slack (best-effort, non-fatal).
    await _mark_channel_read(item.get("channelId", ""))

    ws = request.app["ws_manager"]
    # The item still EXISTS (soft-dismiss) — broadcast 'slack_changed', not
    # 'slack_deleted'. Pages repaint it into the Dismissed section.
    await ws.broadcast("slack_changed", {"id": item_id, "dismissed": True})
    return web.json_response({"ok": True, "id": item_id, "item": item, "etag": new_etag})


async def undismiss_item(request: web.Request) -> web.Response:
    """Undo a dismiss — restore a 'dismissed' item to needs-review/edited (D-014).

    Only acts on a 'dismissed' item: it restores to 'edited' if the saved draft
    differs from the generated draft (mirroring save_draft's edited-detection),
    else 'needs-review'. Any other status is a no-op conflict (409) rather than a
    silent status flip, so the caller can't accidentally un-send/re-arm an item.
    Etag-guarded like the other mutators; broadcasts 'slack_changed'. NEVER sends.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") != "dismissed":
        # Only a dismissed item can be undismissed — surface the conflict instead
        # of silently flipping a sent/needs-review item.
        raise web.HTTPConflict(reason="item is not dismissed")

    # Restore status mirroring save_draft's edited-detection: an item whose draft
    # diverged from its generated draft was an edit-in-progress → 'edited'.
    draft = str(item.get("draft", ""))
    generated = str(item.get("generatedDraft", ""))
    item["status"] = "edited" if draft.strip() != generated.strip() else "needs-review"

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"id": item_id})
    return web.json_response({"item": item, "etag": new_etag})


def _is_clean_mute_key(key: str) -> bool:
    """Defensive sanity check on a mute key from the URL (D-025).

    A mute key is a dict key only (channelId, or channelId_threadTs) — NEVER a
    filesystem path — but we still reject path-traversal characters so a malformed
    key can't be mistaken for one, mirroring _validate_contact.
    """
    return bool(key) and ".." not in key and "/" not in key and "\\" not in key


async def mute_item(request: web.Request) -> web.Response:
    """Mute a thread AND dismiss the item (D-025).

    Derives the mute key (channelId for DMs/group DMs, channelId_threadTs only when
    a threadTs is present — see _mute_key_for), records it in ``mutedThreads`` with
    the current unix timestamp, and SOFT-dismisses the item (status='dismissed', all
    other fields preserved) so subsequent scans skip the conversation until the mute
    is removed or ages out (30 days). Etag-guarded like dismiss_item; best-effort
    marks the channel read; broadcasts 'slack_changed' with {muted:True}.

    Muting a 'sent' item is BLOCKED (409, like dismiss): a sent item is real
    outbound history and hiding it would misrepresent a send. NEVER sends.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") == "sent":
        raise web.HTTPConflict(reason="cannot mute a sent item")

    mute_key = _mute_key_for(item)
    if not mute_key:
        raise web.HTTPBadRequest(reason="item has no channel id to mute")

    # Record the mute (unix seconds) and soft-dismiss the item.
    data.setdefault("mutedThreads", {})[mute_key] = int(time.time())
    item["status"] = "dismissed"

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    # Clear unread badge in Slack (best-effort, non-fatal).
    await _mark_channel_read(item.get("channelId", ""))

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"id": item_id, "muted": True})
    return web.json_response(
        {"ok": True, "id": item_id, "muteKey": mute_key, "item": item, "etag": new_etag}
    )


async def get_muted(request: web.Request) -> web.Response:
    """GET the muted-threads map (D-025) for a settings/management UI."""
    data, etag = await _load_async()
    return web.json_response({"muted": data.get("mutedThreads", {}), "etag": etag})


async def unmute_thread(request: web.Request) -> web.Response:
    """Remove a mute key from ``mutedThreads`` (D-025).

    404 when the key is absent; etag-guarded like the other mutators; broadcasts
    'slack_changed'. Does NOT resurrect any dismissed item — un-muting only stops
    future scans from skipping the conversation (a genuinely new message then earns
    a fresh skeleton). NEVER sends.
    """
    mute_key = request.match_info["mute_key"]
    if not _is_clean_mute_key(mute_key):
        raise web.HTTPBadRequest(reason="invalid mute key")

    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    muted = data.get("mutedThreads") or {}
    if mute_key not in muted:
        raise web.HTTPNotFound(reason=f"mute key {mute_key} not found")

    del muted[mute_key]
    data["mutedThreads"] = muted

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"muteKey": mute_key, "unmuted": True})
    return web.json_response({"ok": True, "muteKey": mute_key, "etag": new_etag})


async def regenerate_item(request: web.Request) -> web.Response:
    """Re-generate the draft for ONE existing queue item via the delegation seam.

    Builds the draft prompt (style + topic memory injected) and asks the seam to
    redraft for this single item. When the seam is unavailable we surface an
    explicit not-available state and DO NOT touch the stored draft — never
    fabricating a draft. NEVER sends.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") == "sent":
        raise web.HTTPConflict(reason="item already sent")

    # MCP-FREE history pre-fetch (D-022): the 'regenerate' action no longer
    # cold-starts slack-mcp. If this item lacks usable history, fetch it on the
    # PERSISTENT warm-auth session and inject it so build_draft_prompt renders it
    # (via _format_history3d) into the prompt the MCP-free subprocess drafts from.
    # An unreadable channel ([] result) just leaves the existing prompt — we still
    # redraft, never falling back to a cold-auth MCP subprocess.
    if not item.get("history3d"):
        channel_id = item.get("channelId", "")
        if channel_id:
            fetched = await _fetch_history_for(channel_id)
            if fetched:
                item["history3d"] = fetched

    result = await _run_slack_agent("regenerate", {
        "id": item_id,
        "prompt": build_draft_prompt(item),
    })
    if not result.get("available"):
        # Honest failure — never fabricate a draft.
        return web.json_response(
            {
                "available": False,
                "reason": result.get("reason", "Slack regenerate unavailable"),
                "item": item,
                "etag": current_etag,
            },
            status=502,
        )

    new_draft = _scrub(str(result.get("draft", "")))[:_DRAFT_CAP]
    item["draft"] = new_draft
    # A regenerated draft is the new baseline for the edit-diff signal, and the
    # item goes back to needs-review (it's a fresh machine draft, not an edit).
    item["generatedDraft"] = new_draft
    item["status"] = "needs-review"

    try:
        new_etag = filestore.write_json(SLACK_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_json(SLACK_PATH)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"id": item_id})
    return web.json_response({"available": True, "item": item, "etag": new_etag})


async def polish_text(request: web.Request) -> web.Response:
    """Polish the CURRENT draft text for fluency, preserving the user's voice.

    STATELESS by design (D-029): this is a pure text transform on the text in the
    request body. It does NOT load or write slack_threads.json (no etag, no status
    change), does NOT read Slack, and NEVER sends — so it can run on text the user
    is mid-edit without any concurrency/store concern. The polished text is returned
    for the UI to drop back into the editable box as an unsaved edit; persisting it
    goes through the existing Save / Approve flow.

    FAIL-LOUD: an empty/non-string `text` is a 400; an unavailable seam is a 502.
    Never fabricates a polished result.
    """
    body = await read_json_body(request) if request.can_read_body else {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise web.HTTPBadRequest(reason="polish requires a non-empty 'text'")

    result = await _run_slack_agent("polish", {
        "text": text[:_DRAFT_CAP],
        "threadContext": str(body.get("threadContext") or "")[:1000],
        "snippet": str(body.get("snippet") or "")[:500],
    })
    if not result.get("available"):
        # Honest failure — never fabricate a polished draft.
        return web.json_response(
            {"available": False, "reason": result.get("reason", "Slack polish unavailable")},
            status=502,
        )

    polished = _scrub(str(result.get("draft", "")))[:_DRAFT_CAP]
    return web.json_response({"available": True, "draft": polished})


async def approve_item(request: web.Request) -> web.Response:
    """The ONLY send path — per-item, single-item, approval-gated.

    Requires the item to exist and carry non-empty final text. Sends via the
    delegation seam; on success marks status='sent'. If the item was EDITED from
    its generated draft, that (generated → final) diff is distilled into a durable
    style note (preference learning) and the contact's topic summary is updated.
    There is deliberately NO bulk/auto-send endpoint; refresh and save never send.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")
    # Optional per-ITEM content baseline (the "waited 10s, didn't send" fix,
    # D-014). The client forwards the draft it reviewed at confirm time. The
    # WHOLE-FILE etag is too coarse for the 9s undo window: the */3 cron + the 5s
    # file-watcher refresh the sidecar (moving its etag) without touching THIS
    # item, which 409s a legitimate unedited send. When the client forwards a
    # baseline AND this item's own content still matches it, the send is safe to
    # proceed even though the whole-file etag moved.
    expected_draft = body.get("expectedDraft")
    if expected_draft is None:
        expected_draft = body.get("baseText")

    data, current_etag = await _load_async()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    # AUTHORITATIVE double-send guard: an already-'sent' item is never re-sent.
    # This is the ONLY boundary that prevents a double-send; the per-item content
    # match below is solely for the etag-staleness path and never weakens this.
    if item.get("status") == "sent":
        raise web.HTTPConflict(reason="item already sent")

    # Final text = explicit body text if provided, else the current saved draft.
    final_text = body.get("draft") or body.get("text") or item.get("draft", "")
    final_text = _scrub(str(final_text))[:_DRAFT_CAP].strip()
    if not final_text:
        raise web.HTTPBadRequest(reason="approved final text required")

    # Per-item content match for the etag-staleness path: if the client forwarded
    # the draft it reviewed and THIS item's current saved draft no longer matches
    # it, the item itself changed under the user (e.g. a cron redraft) — surface a
    # 409 so they re-review, rather than sending stale-vs-current text.
    item_content_changed = False
    if expected_draft is not None:
        current_draft = str(item.get("draft", ""))
        item_content_changed = current_draft.strip() != str(expected_draft).strip()
        if item_content_changed:
            return web.json_response(
                {
                    "error": "conflict",
                    "message": "item content changed since you reviewed it",
                    "current": item,
                    "etag": current_etag,
                },
                status=409,
            )

    # Extract a routable Slack target: prefer channelId (D…/C…/G…), fall back
    # to userId (U…/W…). If NEITHER is present, fail loudly — never send to a
    # bare display name like "DM with <name>" which is ambiguous and unroutable.
    routable_target = (item.get("channelId") or "").strip() or (item.get("userId") or "").strip()
    if not routable_target:
        return web.json_response(
            {
                "available": False,
                "reason": (
                    "No routable Slack channel ID for this item — re-scan or "
                    "run the backfill to capture it."
                ),
                "item": item,
                "etag": current_etag,
            },
            status=400,
        )

    result = await _run_slack_agent("send", {
        "id": item_id,
        "target": routable_target,
        "channelType": item.get("channelType"),
        "text": final_text,
    })
    if not result.get("available"):
        # Honest failure — never fabricate a send-success.
        return web.json_response(
            {
                "available": False,
                "reason": result.get("reason", "Slack send unavailable"),
                "item": item,
                "etag": current_etag,
            },
            status=502,
        )

    # Preference learning: if the user edited the generated draft, capture the
    # (generated → final) signal as a durable, human-readable style note.
    generated = str(item.get("generatedDraft", "")).strip()
    if generated and generated != final_text:
        _append_style_note(item, generated, final_text)
    # Topic memory: record what we just replied about, append-style.
    _append_topic_note(item.get("sender", ""), item, final_text)

    # The send already SUCCEEDED above (the destructive, irreversible act). Now we
    # must durably record status='sent' or we'd double-send on a retry. A
    # concurrent */3 cron / 5s watcher write may have moved the WHOLE-FILE etag
    # during the send call even though THIS item is unchanged, so when the client
    # vouched for the item's content (expected_draft) and it still matched, persist
    # against a FRESH re-read etag rather than the stale client etag — a refresh
    # that didn't touch this item must not 409 a send that already went out. We
    # re-read, re-locate the item, re-apply the sent markers, and write with the
    # fresh etag. The already-sent guard above remains the double-send boundary;
    # if THIS item is now 'sent' in the fresh read, a concurrent approve beat us
    # and we must NOT mutate/re-broadcast it as a fresh send.
    if expected_draft is not None:
        data, current_etag = await _load_async()
        item = next((it for it in data["items"] if it.get("id") == item_id), None)
        if item is None:
            # The item vanished between send and persist — the send went out but we
            # can't durably mark it. Fail loud rather than silently dropping it.
            return web.json_response(
                {"error": "conflict", "message": "item disappeared during send",
                 "etag": current_etag},
                status=409,
            )
        if item.get("status") == "sent":
            # A concurrent approve already recorded the send — return its state
            # rather than re-broadcasting a duplicate "sent".
            return web.json_response({"available": True, "item": item, "etag": current_etag})
        write_etag = current_etag
    else:
        # No per-item baseline forwarded — preserve the strict whole-file etag
        # behaviour (client etag if supplied), so an explicit optimistic-concurrency
        # caller still gets a 409 on a genuinely stale view.
        write_etag = expected_etag or current_etag

    item["status"] = "sent"
    item["finalText"] = final_text
    item["sentAt"] = int(time.time() * 1000)

    # The send ALREADY SUCCEEDED — we MUST persist status='sent' to prevent
    # double-sends. If a concurrent write moved the etag, re-read and retry
    # the persist (the send is irreversible; failing to mark it is the bug
    # that causes double-sends via client retry).
    for _persist_attempt in range(3):
        try:
            new_etag = filestore.write_json(SLACK_PATH, data, write_etag)
            break
        except filestore.ConflictError:
            data, write_etag = await _load_async()
            item = next((it for it in data["items"] if it.get("id") == item_id), None)
            if item is None:
                break
            if item.get("status") == "sent":
                new_etag = write_etag
                break
            item["status"] = "sent"
            item["finalText"] = final_text
            item["sentAt"] = int(time.time() * 1000)
    else:
        new_etag = write_etag

    # Clear unread badge in Slack (best-effort, non-fatal).
    await _mark_channel_read(item.get("channelId", ""))

    ws = request.app["ws_manager"]
    await ws.broadcast("slack_changed", {"id": item_id, "sent": True})
    return web.json_response({"available": True, "item": item, "etag": new_etag})


# ── Preference-learning + topic memory (human-readable, append-and-reconcile) ──


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _ensure_heading(content: str, heading: str) -> str:
    return content if content.strip() else f"{heading}\n"


def _append_style_note(item: dict, generated: str, final: str) -> None:
    """Append a dated style note distilled from a (generated → final) edit.

    Append-only and human-readable (the agent-context model): we never rewrite or
    delete prior notes — reconciling duplicates is a human action. The note
    records both texts so the distilled preference is auditable. Scrubbed.
    """
    path = _style_path()
    content, _ = filestore.read_text(path)
    content = _ensure_heading(content, "# Slack reply style — learned from my edits")
    sender = _scrub(str(item.get("sender", "")))[:200]
    note = (
        f"- {_today()}: For {sender or 'a contact'}, I edited the draft before sending. "
        f"Drafted: {_one_line(generated)} → I sent: {_one_line(final)}. "
        f"Prefer my phrasing/tone over the drafted version."
    )
    new_content = content.rstrip("\n") + "\n" + _scrub(note) + "\n"
    filestore.write_text(path, new_content)


def _append_topic_note(contact: str, item: dict, final: str) -> None:
    """Append a per-contact running topic/context summary entry.

    Append-and-reconcile, human-readable, never silently overwritten — this is
    read back into the draft prompt (read_topic_summary) so future drafts
    reference prior context.
    """
    contact = _scrub(str(contact or "")).strip()
    if not contact or ".." in contact or "/" in contact or "\\" in contact:
        return
    path = _topics_dir() / f"{contact}.md"
    content, _ = filestore.read_text(path)
    content = _ensure_heading(content, f"# Topics with {contact}")
    snippet = _scrub(str(item.get("snippet", "")))
    note = (
        f"- {_today()}: They wrote: {_one_line(snippet)}. "
        f"I replied: {_one_line(final)}."
    )
    new_content = content.rstrip("\n") + "\n" + _scrub(note) + "\n"
    filestore.write_text(path, new_content)


def _one_line(text: str, cap: int = 300) -> str:
    """Collapse multi-line text to a single capped line for a memory bullet."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    return collapsed[:cap]


# ── Style + topic stores (read/write endpoints) ──────────────────────────────


async def get_style(request: web.Request) -> web.Response:
    content, etag = filestore.read_text(_style_path())
    return web.json_response({"content": content, "etag": etag, "path": str(_style_path())})


async def put_style(request: web.Request) -> web.Response:
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
    contact = request.match_info["contact"]
    _validate_contact(contact)
    path = _topics_dir() / f"{contact}.md"
    content, etag = filestore.read_text(path)
    return web.json_response({"contact": contact, "content": content, "etag": etag, "path": str(path)})


async def put_topics(request: web.Request) -> web.Response:
    contact = request.match_info["contact"]
    _validate_contact(contact)
    body = await read_json_body(request)
    content = body.get("content")
    if content is None:
        raise web.HTTPBadRequest(reason="content field required")
    expected_etag = body.get("etag")
    content = _scrub(str(content))
    path = _topics_dir() / f"{contact}.md"

    try:
        new_etag = filestore.write_text(path, content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(path)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )
    return web.json_response({"ok": True, "contact": contact, "content": content, "etag": new_etag})
