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
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path

from aiohttp import web

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from server import filestore
from server.routes import read_json_body
from server.session_manager import is_auth_error

logger = logging.getLogger(__name__)


# ── Path resolution ─────────────────────────────────────────────────────────

def _resolve_email_path() -> Path:
    """Resolve the email_threads.json sidecar.

    Precedence:
      1. CLAUDE_WEB_EMAIL_PATH env override.
      2. <cwd>/.claude/email_threads.json
    """
    env = os.environ.get("CLAUDE_WEB_EMAIL_PATH")
    if env:
        return Path(env)
    return Path.cwd() / ".claude" / "email_threads.json"


EMAIL_PATH = _resolve_email_path()


def _style_path() -> Path:
    """Human-readable learned-style store, sibling to the sidecar."""
    return EMAIL_PATH.parent / "email_style.md"


# ── Constants ───────────────────────────────────────────────────────────────

# emailBody cap: spec mandates capping at 32k chars when storing items.
_EMAIL_BODY_CAP = 32768

_SNIPPET_CAP = 2000
_DRAFT_CAP = 8000

# Lines that may carry credentials -- never persisted or surfaced.
_SECRET_MARKERS = ("~/.midway", ".midway", "cookie", "mwinit", "aws_secret", "authorization:")

_VALID_STATUS = {
    "needs-classify", "needs-draft", "needs-review", "edited", "approved",
    "dismissed",
}

_VALID_CLASSIFICATIONS = {"needs-reply", "fyi", "actionable"}

# Classification routing: all labels flow to needs-draft so every item gets a
# draft. The label is recorded for UI grouping, not lifecycle gating.
_CLASSIFY_NEXT_STATUS = {
    "needs-reply": "needs-draft",
    "actionable": "needs-draft",
    "fyi": "needs-draft",
}

# Worker tuning constants (mirroring Slack module):
EMAIL_SCAN_INTERVAL_S = 300        # 5 minutes between scans
EMAIL_CLASSIFY_POLL_INTERVAL_S = 6  # classify worker poll interval
EMAIL_CLASSIFY_BATCH = 10          # max items per classify LLM call
EMAIL_DRAFT_POLL_INTERVAL_S = 7    # draft worker poll interval
EMAIL_DRAFT_CONCURRENCY = 3        # parallel draft subprocess cap

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


# ── Send-safety boundary ───────────────────────────────────────────────────

# THE SEND-SAFETY BOUNDARY: the email module may ONLY call read tools via MCP.
# Write tools (reply, send, draft, forward, move, update) are NEVER in this set.
# call_read_tool REFUSES by name any tool NOT in this frozenset BEFORE the SDK
# ever touches the server.
_EMAIL_READ_ONLY_TOOLS = frozenset({
    "email_inbox",
    "email_read",
    "email_search",
    "email_folders",
    "email_list_folders",
    "email_contacts",
    "email_attachments",
    "email_categories",
})


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


def _load() -> tuple[dict, str | None]:
    """Load the email sidecar, returning (data, etag)."""
    data, etag = filestore.read_json(EMAIL_PATH)
    if not data:
        data = {"items": []}
    if "items" not in data:
        data["items"] = []
    return data, etag


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


def _get_mcp_lock() -> asyncio.Lock:
    """Lazy-init the module-level connect/reconnect lock."""
    global _mcp_connect_lock
    if _mcp_connect_lock is None:
        _mcp_connect_lock = asyncio.Lock()
    return _mcp_connect_lock


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

async def call_read_tool(name: str, arguments: dict) -> object:
    """Call ONE email MCP READ tool via a PERSISTENT session; FAIL LOUD on error.

    THE SEND-SAFETY BOUNDARY: rejects by name BEFORE any connection or SDK
    interaction. Uses a persistent session with reconnect-once on transient error.
    """
    if name not in _EMAIL_READ_ONLY_TOOLS:
        raise EmailMcpError(
            f"Refusing to call email tool {name!r}: not in the read-only allowlist."
        )

    # Retry strategy: 1 call on existing session + 1 retry after reconnect.
    for attempt in range(2):
        try:
            await _connect_mcp()
            if _mcp_state.session is None:
                raise EmailMcpError("Email MCP session failed to connect.")

            result = await _mcp_state.session.call_tool(name, arguments)

            if getattr(result, "isError", False):
                raise EmailMcpError(
                    _scrub(f"Email MCP tool {name!r} returned an error: "
                           + _one_line(str(getattr(result, "content", "")), 200))
                )

            return _normalize_tool_result(result)

        except EmailMcpError:
            raise
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if _is_transient_mcp_error(e) and attempt == 0:
                logger.debug("Email MCP transient error, reconnecting: %s", _scrub(str(e)))
                await _disconnect_mcp()
                await asyncio.sleep(1.0)
                continue
            raise EmailMcpError(_scrub(f"Email MCP call failed: {e}")) from e

    raise EmailMcpError("Email MCP call failed after retry.")


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
        item = payload.get("item") or {}
        subject = _one_line(str(item.get("subject", "")), 300)
        sender = _one_line(str(item.get("sender", "")), 200)
        body = str(item.get("emailBody", ""))[:_EMAIL_BODY_CAP]
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
        prompt = (
            "Draft a reply to this email in MY voice using ONLY the context below "
            "-- do NOT call any tools, do NOT fetch anything, do NOT send anything."
            f"{style_content}\n\n"
            f"## Email to reply to\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n\n"
            f"{context}\n\n"
            "Return ONLY a STRICT JSON object (no prose, no markdown fences): "
            '{"draft": "<the reply text>", "generatedDraft": "<the same reply text>", '
            '"threadContext": "<a 1-2 sentence summary of what the email is about>"}. '
            "generatedDraft must equal draft."
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
        if not isinstance(parsed, dict) or "draft" not in parsed:
            return {"available": False, "reason": f"Email {action} reply missing 'draft'."}
        return {
            "available": True,
            "draft": parsed.get("draft", ""),
            "generatedDraft": parsed.get("generatedDraft", parsed.get("draft", "")),
            "threadContext": parsed.get("threadContext", ""),
        }
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
      'draft'      -> {available:True, draft:..., generatedDraft:..., threadContext:...}
      'regenerate' -> {available:True, draft:..., generatedDraft:..., threadContext:...}
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


def _probe_readiness() -> dict:
    """Report seam readiness WITHOUT any I/O.

    Returns whether claude is on PATH and email MCP is configured.
    """
    claude_on_path = shutil.which("claude") is not None
    mcp_entry = _find_email_mcp_entry()
    mcp_configured = mcp_entry is not None
    ready = claude_on_path  # Email MCP not strictly required for MCP-FREE actions
    return {
        "claudeOnPath": claude_on_path,
        "mcpConfigured": mcp_configured,
        "ready": ready,
    }


def _mute_key_for(item: dict) -> str:
    """Derive a mute key from an item (conversationId-scoped)."""
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


_MY_EMAIL = "user@example.com"


def _recipient_type(raw: dict) -> str:
    """Determine whether the user is in To (only), To (shared), or CC.

    Returns: 'to-only' | 'to' | 'cc' | 'unknown'.

    Heuristic: since the email reached my inbox, I'm definitely a recipient.
    If toRecipients/ccRecipients are populated, match my personal email OR
    treat the absence of my address in both as 'to' (via a distribution list).
    If the lists are empty/absent, fall back to 'unknown'.
    """
    to_list = raw.get("toRecipients") or []
    cc_list = raw.get("ccRecipients") or []

    # If neither list is populated, we can't determine
    if not to_list and not cc_list:
        return "unknown"

    def _contains_me(lst):
        """True only if MY personal email is literally in the list."""
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


def _skeleton_for(raw: dict) -> dict:
    """Build a needs-classify skeleton from a raw inbox item.

    The Outlook MCP uses: topic (not subject), senders (array, not sender),
    preview (not snippet), recipients (array).
    """
    import secrets as _secrets
    conv_id = str(raw.get("conversationId", "") or raw.get("id", ""))
    # Sender: Outlook uses senders[] array
    senders = raw.get("senders") or []
    sender = senders[0] if isinstance(senders, list) and senders else str(raw.get("sender", "") or raw.get("from", ""))
    # Subject: Outlook uses "topic"
    subject = str(raw.get("topic", "") or raw.get("subject", ""))
    # Snippet: Outlook uses "preview"
    snippet = str(raw.get("preview", "") or raw.get("snippet", "") or raw.get("bodyPreview", ""))
    # Recipients
    recipients = raw.get("recipients") or []
    return {
        "id": _secrets.token_hex(4),
        "conversationId": conv_id,
        "sender": _scrub(_one_line(sender, 200)),
        "senderEmail": "",
        "subject": _scrub(_one_line(subject, 300)),
        "snippet": _scrub(snippet[:_SNIPPET_CAP]),
        "recipients": recipients if isinstance(recipients, list) else [],
        "recipientType": _recipient_type(raw),
        "hasAttachments": bool(raw.get("hasAttachments")),
        "messageCount": raw.get("messageCount") or raw.get("unreadCount") or 1,
        "status": "needs-classify",
        "ts": int(time.time() * 1000),
    }


def _extract_email_body(payload) -> str:
    """Extract the email body text from an email_read MCP response.

    Outlook MCP returns: {success, content: {emails: [{body: "..."}]}}
    We concatenate all message bodies (newest first) into a single string.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    # Try content.emails[].body (the actual Outlook MCP shape)
    content = payload.get("content")
    if isinstance(content, dict):
        emails = content.get("emails") or content.get("messages") or []
        if isinstance(emails, list) and emails:
            parts = []
            for msg in emails:
                if isinstance(msg, dict):
                    body = msg.get("body", "")
                    sender = msg.get("sender") or msg.get("from", "")
                    subject = msg.get("subject", "")
                    if body:
                        header = f"From: {sender}\nSubject: {subject}\n\n" if sender else ""
                        parts.append(header + body)
            return "\n---\n".join(parts) if parts else ""
    # Fallback: top-level body or content as string
    body = payload.get("body", "") or payload.get("content", "")
    return str(body) if body else ""


async def _scan_once(app) -> None:
    """Run ONE deterministic email scan: fetch inbox -> dedupe -> fetch bodies -> write.

    Calls email_inbox via the read-only direct-MCP client, filters by new
    conversationId, fetches full body for each, writes needs-classify skeletons.
    On MCP read failure it FAILS LOUD (raises EmailMcpError).
    """
    global _LAST_SCAN_TS_MS
    now_ms = int(time.time() * 1000)

    # Fetch unread inbox
    inbox_payload = await call_read_tool("email_inbox", {
        "unreadOnly": True, "limit": 25, "enrichRecipients": True,
    })
    raw_items = _unwrap_list(inbox_payload, "messages", "items", "emails", "value")

    data, current_etag = _load()

    # Prune mutes
    now_s = now_ms / 1000.0
    muted = _prune_muted(data.get("mutedThreads") or {}, now_s)
    data["mutedThreads"] = muted

    # Existing conversationIds in active statuses
    existing_conv_ids = {
        str(it.get("conversationId", ""))
        for it in data["items"]
        if it.get("status") in {"needs-classify", "needs-draft", "needs-review", "edited"}
    }

    # Filter new items (not already in queue, not muted)
    new_items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        conv_id = str(raw.get("conversationId", "") or raw.get("id", ""))
        if not conv_id:
            continue
        if conv_id in existing_conv_ids:
            continue
        mute_key = conv_id
        if muted and mute_key in muted:
            continue
        new_items.append(raw)

    # For each new email, pre-fetch body via email_read
    for raw in new_items:
        conv_id = str(raw.get("conversationId", "") or raw.get("id", ""))
        if conv_id:
            try:
                body_payload = await call_read_tool(
                    "email_read", {"conversationId": conv_id, "format": "markdown"}
                )
                body_text = _extract_email_body(body_payload)
                if body_text:
                    raw["emailBody"] = body_text[:_EMAIL_BODY_CAP]
            except (EmailMcpError, Exception) as e:
                # Non-fatal: we can still classify from snippet alone
                logger.debug("Email body fetch failed for %s: %s", conv_id, _scrub(str(e)))

    # Write needs-classify skeletons
    for raw in new_items:
        skeleton = _skeleton_for(raw)
        if raw.get("emailBody"):
            skeleton["emailBody"] = raw["emailBody"]
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
            if not _probe_readiness().get("ready"):
                continue

            data, _ = _load()
            if data.get("paused"):
                logger.info("Email scan-worker: paused, skipping this cycle.")
                continue

            if not await _run_guarded_scan(app):
                logger.info("Email scan-worker: a scan is already in progress, skipping.")
                continue
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

        result = await _run_email_agent("draft", {"id": item_id, "item": item})

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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Email draft-worker iteration failed: %s", _scrub(str(e)))


# ── Worker lifecycle ────────────────────────────────────────────────────────

async def _start_scan_worker(app) -> None:
    """on_startup: seed _LAST_SCAN_TS_MS from persisted lastScanAt, then launch."""
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
    """on_cleanup: tear down the persistent email MCP session (reap subprocess)."""
    await _disconnect_mcp()


# ── Route handlers ──────────────────────────────────────────────────────────

async def get_health(request: web.Request) -> web.Response:
    """GET /api/email/health -- readiness probe."""
    claude_on_path = shutil.which("claude") is not None
    return web.json_response({
        "claudeOnPath": claude_on_path,
        "ready": claude_on_path,
    })


async def get_queue(request: web.Request) -> web.Response:
    """GET /api/email/queue -- return the email queue."""
    data, etag = _load()
    return web.json_response({
        "items": data["items"],
        "etag": etag,
        "lastScanAt": data.get("lastScanAt"),
        "paused": data.get("paused", False),
    })


async def put_config(request: web.Request) -> web.Response:
    """PUT /api/email/config -- toggle paused state."""
    body = await read_json_body(request)
    if "paused" not in body:
        raise web.HTTPBadRequest(reason="paused field required")

    data, current_etag = _load()
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


async def save_draft(request: web.Request) -> web.Response:
    """PUT /api/email/queue/{item_id} -- save an edited draft."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")
    if "draft" not in body:
        raise web.HTTPBadRequest(reason="draft field required")
    new_draft = _scrub(str(body["draft"]))[:_DRAFT_CAP]

    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    item["draft"] = new_draft
    if new_draft.strip() != str(item.get("generatedDraft", "")).strip():
        item["status"] = "edited"

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


async def dismiss_item(request: web.Request) -> web.Response:
    """DELETE /api/email/queue/{item_id} -- soft-dismiss (preserve item)."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

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
    await ws.broadcast("email_changed", {"id": item_id, "dismissed": True})
    return web.json_response({"ok": True, "id": item_id, "item": item, "etag": new_etag})


async def undismiss_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/undismiss -- restore a dismissed item."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
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


async def regenerate_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/refresh -- regenerate the draft."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    result = await _run_email_agent("regenerate", {
        "id": item_id,
        "item": item,
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

    The system NEVER sends email directly. Approve saves the draft via the
    delegation seam (action 'save_draft') and marks the item as 'approved'.
    The user sends from their own email client.
    """
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")
    if item.get("status") == "approved":
        raise web.HTTPConflict(reason="item already approved")

    final_draft = item.get("draft", "")

    # Delegate to save_draft action (NEVER 'reply' or 'send')
    result = await _run_email_agent("save_draft", {
        "id": item_id,
        "draft": final_draft,
        "item": item,
    })

    draft_saved = result.get("draftSaved", False) if result.get("available") else False

    # Mark approved regardless of whether the save succeeded
    item["status"] = "approved"

    try:
        new_etag = filestore.write_json(EMAIL_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError:
        # Re-read and retry once
        data, current_etag = _load()
        item = next((it for it in data["items"] if it.get("id") == item_id), None)
        if item and item.get("status") != "approved":
            item["status"] = "approved"
            try:
                new_etag = filestore.write_json(EMAIL_PATH, data, current_etag)
            except filestore.ConflictError:
                new_etag = current_etag
        else:
            new_etag = current_etag

    ws = request.app["ws_manager"]
    await ws.broadcast("email_changed", {"id": item_id, "approved": True})
    return web.json_response({
        "available": result.get("available", False),
        "draftSaved": draft_saved,
        "item": item,
        "etag": new_etag,
    })


async def mute_item(request: web.Request) -> web.Response:
    """POST /api/email/queue/{item_id}/mute -- mute conversation + dismiss."""
    item_id = request.match_info["item_id"]
    body = await read_json_body(request) if request.can_read_body else {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
    item = next((it for it in data["items"] if it.get("id") == item_id), None)
    if not item:
        raise web.HTTPNotFound(reason=f"item {item_id} not found")

    # Derive mute key from conversationId
    conv_id = str(item.get("conversationId", "") or "").strip()
    mute_key = conv_id or item_id

    # Record the mute and soft-dismiss
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

    data, current_etag = _load()
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
    _, etag = _load()

    if _get_scan_lock().locked():
        return web.json_response(
            {"scanning": False, "reason": "A scan is already in progress."},
            status=200,
        )

    _get_scan_event().set()
    return web.json_response({"scanning": True, "etag": etag}, status=202)


async def get_muted(request: web.Request) -> web.Response:
    """GET /api/email/muted -- list muted threads for settings UI."""
    data, etag = _load()
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


# ── Registration ────────────────────────────────────────────────────────────

def register(app: web.Application):
    """Register all /api/email/* routes."""
    app.router.add_get("/api/email/health", get_health)
    app.router.add_get("/api/email/queue", get_queue)
    app.router.add_put("/api/email/config", put_config)
    # Manual scan trigger -- MUST be registered BEFORE the parameterized
    # /api/email/queue/{item_id} routes so "refresh" is not captured as item_id.
    app.router.add_post("/api/email/queue/refresh", refresh_queue)
    app.router.add_put("/api/email/queue/{item_id}", save_draft)
    app.router.add_delete("/api/email/queue/{item_id}", dismiss_item)
    app.router.add_post("/api/email/queue/{item_id}/undismiss", undismiss_item)
    app.router.add_post("/api/email/queue/{item_id}/refresh", regenerate_item)
    app.router.add_post("/api/email/queue/{item_id}/approve", approve_item)
    app.router.add_post("/api/email/queue/{item_id}/mute", mute_item)
    # Muted-threads management surface: list + unmute.
    app.router.add_get("/api/email/muted", get_muted)
    app.router.add_delete("/api/email/muted/{mute_key}", unmute_thread)
    app.router.add_get("/api/email/style", get_style)
    app.router.add_put("/api/email/style", put_style)

    # Worker lifecycle
    app.on_startup.append(_start_scan_worker)
    app.on_cleanup.append(_stop_scan_worker)
    app.on_startup.append(_start_classify_worker)
    app.on_cleanup.append(_stop_classify_worker)
    app.on_startup.append(_start_draft_worker)
    app.on_cleanup.append(_stop_draft_worker)
    # Persistent MCP session cleanup (AFTER workers so no new calls arrive)
    app.on_cleanup.append(_cleanup_persistent_mcp)
