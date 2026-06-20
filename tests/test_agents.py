"""Agent-context tests — the E1 context-summary parse memo.

These tests live in a SEPARATE file (not test_server.py) to avoid colliding with
the in-flight uncommitted edits there (the monolith is a write-contention
hotspot). They cover the etag-keyed memo in `agents.context_summary` (ADR D-032):

  - The memo caches ONLY the .md-content-derived parse (`parse_context_entries`),
    keyed by (project_dir, name, etag) where etag is the .md's st_mtime_ns. An
    UNCHANGED .md must reuse the memo (parse_context_entries is NOT re-run).
  - The sidecar-driven signals (newEntryCount, the dismissed set feeding
    conflictClusterCount, oversize ack) are RECOMPUTED FRESH on every call, so an
    out-of-band dismiss / review / ack — which touches only `.reviewed/`, not the
    .md — is reflected on the very next listing call (never served stale).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

import server.routes.agents as agents_mod  # noqa: E402
import server.routes.sessions as sessions_mod  # noqa: E402
from server.app import create_app  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path, Path("/etc/__never__")])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    application["allowed_cwd_roots"] = [tmp_path, Path("/etc/__never__")]
    application["default_cwd"] = tmp_path
    application["permission_mode"] = "bypassPermissions"
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """A tmp-backed WORKSPACE_DIR + isolated global agents dir.

    Mirrors the projects_layout monkeypatch surface from test_server.py so the
    agents endpoints resolve against tmp paths and never read the developer's
    real ~/.claude/agents contents.
    """
    ws = tmp_path / "workspace" / "projects"
    ws.mkdir(parents=True)
    monkeypatch.setattr(sessions_mod, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(agents_mod, "GLOBAL_AGENTS_DIR", tmp_path / "dot_claude" / "agents")
    return ws


@pytest.fixture(autouse=True)
def _reset_parse_cache(monkeypatch):
    """Reset the E1 context-summary parse memo per test [E1-5].

    The memo is a module-level dict keyed by (project_dir, name, etag); without a
    reset a parsed-entries result would leak across tests (and break the call-count
    spy in the cache-reuse test). Mirrors _reset_sessions_caches in test_server.py.
    """
    monkeypatch.setattr(agents_mod, "_PARSE_CACHE", {})


@pytest.fixture(autouse=True)
def _reset_worker_primitives():
    """Reset the Slack/email modules' lazily-created loop-bound asyncio primitives
    between tests (mirrors test_email.py's _reset_email_primitives).

    create_app() starts the Slack/email scan workers, which build an asyncio.Event
    lazily bound to the first test's loop; without a reset the next test reuses it
    and aiohttp's cleanup raises 'bound to a different event loop'.
    """
    def _clear():
        for mod_name in ("server.routes.slack", "server.routes.email"):
            try:
                mod = sys.modules.get(mod_name) or __import__(mod_name, fromlist=["x"])
            except ImportError:
                continue
            for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
                if hasattr(mod, attr):
                    setattr(mod, attr, None)
    _clear()
    yield
    _clear()


_AGENT_MD = """---
name: backend-dev
description: Backend developer.
tools: Read, Edit, Bash
model: opus
---
You are the backend developer.
"""

# Two genuinely near-duplicate entries (high overlap of discriminative vocab) →
# exactly one "redundant" cluster, plus two distinct entries that don't cluster.
_CONFLICT_CONTEXT = """# backend-dev -- project context
- 2026-06-01: The deploy pipeline promotes the build artifact through gamma then production after the integration suite passes.
- 2026-06-02: The deploy pipeline promotes the build artifact through gamma then production once the integration suite passes.
- 2026-06-03: Frontend rendering uses Vite with a hashed asset filename for immutable browser caching.
- 2026-06-04: Database migrations run via a separate Alembic revision keyed on the schema version.
"""


def _seed_agent(workspace: Path, project: str, name: str) -> None:
    d = workspace / project / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(_AGENT_MD, encoding="utf-8")


def _seed_context(workspace: Path, project: str, name: str, content: str) -> Path:
    d = workspace / project / ".claude" / "agent-context"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


async def _agent_row(client, project: str, name: str) -> dict:
    """GET /api/projects/{id}/agents and return the row for `name`."""
    rows = await (await client.get(f"/api/projects/{project}/agents")).json()
    match = [r for r in rows if r["slug"] == name]
    assert match, f"agent {name} not in listing: {rows}"
    return match[0]


# ─── E1-1: listing reports a true conflict + correct content-derived fields ───


async def test_listing_reports_conflict_and_content_fields(client, workspace):
    """A project agent whose context has a real redundant cluster still shows
    conflictClusterCount>0 plus correct entryCount/contextBytes/lineCount on the
    /agents listing (the memo never zeroes/defers the content-derived fields)."""
    _seed_agent(workspace, "alpha", "backend-dev")
    ctx = _seed_context(workspace, "alpha", "backend-dev", _CONFLICT_CONTEXT)

    row = await _agent_row(client, "alpha", "backend-dev")
    assert row["conflictClusterCount"] > 0
    assert row["contextEntryCount"] == 4
    assert row["contextBytes"] == len(_CONFLICT_CONTEXT.encode("utf-8"))

    summary = agents_mod.context_summary(workspace / "alpha", "backend-dev")
    assert summary["entryCount"] == 4
    assert summary["lineCount"] == ctx.read_text(encoding="utf-8").count("\n")
    assert summary["oversized"] is False


# ─── E1-4: an unchanged .md reuses the memo (parse is NOT re-run) ─────────────


async def test_unchanged_md_reuses_memo_no_reparse(client, workspace, monkeypatch):
    """Two listing calls on an UNCHANGED .md must parse the context file FEWER
    times the second time — proving the etag memo actually short-circuits the
    scan, not merely stores-then-recomputes (the false-optimization trap)."""
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev", _CONFLICT_CONTEXT)

    real_parse = agents_mod.parse_context_entries
    calls: list[int] = []

    def counting_parse(content: str):
        calls.append(1)
        return real_parse(content)

    monkeypatch.setattr(agents_mod, "parse_context_entries", counting_parse)

    await _agent_row(client, "alpha", "backend-dev")
    first = len(calls)
    assert first >= 1, "first listing must parse the context at least once"

    await _agent_row(client, "alpha", "backend-dev")
    second = len(calls) - first
    # The second (unchanged-.md) listing reused the cached parse for this agent:
    # context_summary did not re-walk the file.
    assert second < first, (
        f"unchanged .md re-parsed {second} time(s); expected fewer than the "
        f"first listing's {first} (memo did not short-circuit)"
    )
    assert second == 0, f"unchanged .md must not re-parse at all; re-parsed {second}x"


# ─── E1-3: a sidecar-only dismiss drops the conflict count on the NEXT call ───


async def test_dismiss_sidecar_drops_conflict_count_not_served_stale(client, workspace):
    """Dismissing the cluster touches only the `.dismissed` sidecar (NOT the .md),
    so the .md mtime/etag doesn't move. The next listing must STILL report the
    lowered conflictClusterCount — proving the count is recomputed fresh against
    the dismissed set every call, never served stale from the memo."""
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev", _CONFLICT_CONTEXT)
    ctx = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before_etag = (ctx.stat().st_mtime_ns)

    row = await _agent_row(client, "alpha", "backend-dev")
    assert row["conflictClusterCount"] == 1
    indices = (await (await client.get(
        "/api/projects/alpha/agents/backend-dev/context/conflicts")).json())["clusters"][0]["entryIndices"]

    # Dismiss the cluster — reconcile's "dismiss" writes ONLY the .dismissed
    # sidecar; the context .md is untouched.
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "dismiss", "entryIndices": indices},
    )
    assert resp.status == 200
    assert ctx.stat().st_mtime_ns == before_etag, ".md must not have changed (mtime moved)"

    row2 = await _agent_row(client, "alpha", "backend-dev")
    assert row2["conflictClusterCount"] == 0, (
        "dismissed cluster still counted — the conflict count was served stale "
        "from the memo despite the .md being unchanged"
    )


# ─── E1-2: a sidecar-only review/oversize-ack flips the cheap signals ─────────


async def test_review_marker_sidecar_flips_new_count_next_call(client, workspace):
    """Advancing the reviewed marker writes only the `.reviewed/<name>.txt`
    sidecar (not the .md). newEntryCount must drop on the very next listing even
    though the .md etag is unchanged — the new-entry signal is recomputed fresh."""
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev", _CONFLICT_CONTEXT)
    ctx = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before_etag = ctx.stat().st_mtime_ns

    row = await _agent_row(client, "alpha", "backend-dev")
    assert row["newEntryCount"] == 4  # never reviewed → every entry is new

    # Mark reviewed (sidecar-only).
    resp = await client.post("/api/projects/alpha/agents/backend-dev/context/mark-reviewed")
    assert resp.status == 200
    assert ctx.stat().st_mtime_ns == before_etag, ".md must not have changed"

    row2 = await _agent_row(client, "alpha", "backend-dev")
    assert row2["newEntryCount"] == 0, (
        "newEntryCount served stale from the memo after a sidecar-only review"
    )


async def test_oversize_ack_sidecar_flips_signal_next_call(client, workspace):
    """Acknowledging an oversize writes only the `.reviewed/<name>.oversize`
    sidecar. oversizeActionable→False / oversizeAcknowledged→True must show on
    the next listing with the .md unchanged (sidecar signals recompute fresh)."""
    # An honestly-large file: > _OVERSIZE_LINES with NO conflicts and NO ephemerals
    # so the ack is accepted (the honesty gate).
    lines = ["# backend-dev -- project context"]
    for i in range(agents_mod._OVERSIZE_LINES + 5):
        lines.append(f"- 2026-06-{(i % 28) + 1:02d}: Distinct note number {i} about subsystem {i} only.")
    big = "\n".join(lines) + "\n"
    _seed_agent(workspace, "alpha", "backend-dev")
    ctx = _seed_context(workspace, "alpha", "backend-dev", big)
    before_etag = ctx.stat().st_mtime_ns

    row = await _agent_row(client, "alpha", "backend-dev")
    assert row["oversized"] is True
    assert row["oversizeAcknowledged"] is False

    resp = await client.post("/api/projects/alpha/agents/backend-dev/context/oversize-ack")
    assert resp.status == 200, await resp.text()
    assert ctx.stat().st_mtime_ns == before_etag, ".md must not have changed"

    row2 = await _agent_row(client, "alpha", "backend-dev")
    assert row2["oversizeAcknowledged"] is True, (
        "oversize ack served stale from the memo (sidecar-only change ignored)"
    )
    assert row2["oversizeActionable"] is False
