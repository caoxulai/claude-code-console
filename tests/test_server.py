"""Smoke + API-shape tests for claude-web. claude is mocked."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from server.app import create_app, ALLOWED_CWD_ROOTS  # noqa: E402


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    # Patch ALLOWED_CWD_ROOTS in server.app so the test client can pass cwd values
    # without hitting the production allowlist.
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path, Path("/etc/__never__")])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    # Also patch the runtime copy stored in app state
    application["allowed_cwd_roots"] = [tmp_path, Path("/etc/__never__")]
    application["default_cwd"] = tmp_path
    # Pin a deterministic permission mode so the chat/restart threading tests can
    # assert the spawn sites forward exactly this value (create_app resolves it
    # from config/env, which would otherwise vary by environment).
    application["permission_mode"] = "bypassPermissions"
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def test_healthz_is_public(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}


async def test_chat_rejects_empty_prompt(client):
    resp = await client.post("/api/chat", json={"prompt": "   "})
    assert resp.status == 400


async def test_chat_rejects_cwd_outside_allowlist(client):
    # "/" is not under the allowlist (which is just tmp_path in this test setup).
    resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": "/"})
    assert resp.status == 403


async def test_chat_streams_sse(client, tmp_path):
    fake_events = [
        {"type": "system", "session_id": "abc"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "result", "is_error": False, "result": "hello"},
    ]

    async def fake_send(prompt):
        for evt in fake_events:
            yield evt

    # Mock the session manager to return a fake session
    fake_session = AsyncMock()
    fake_session.session_id = "abc"
    fake_session.send = fake_send

    fake_manager = AsyncMock()
    fake_manager.get_or_create = AsyncMock(return_value=fake_session)
    fake_manager.register = lambda s: None

    with patch.dict(client.app, {"session_manager": fake_manager}):
        resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": str(tmp_path)})
    assert resp.status == 200
    body = await resp.text()
    # Should be a sequence of SSE frames terminated by [DONE].
    assert "data: [DONE]" in body
    assert '"type":"system"' in body or '"type": "system"' in body
    assert '"type":"assistant"' in body or '"type": "assistant"' in body


async def test_chat_threads_app_permission_mode_into_get_or_create(client, tmp_path):
    """The chat route must spawn the session with the app's resolved permission
    mode, not a hardcoded value. Regression guard: a session created via the web
    UI keeps write ability (bypassPermissions) rather than silently dropping to
    'default'.
    """
    async def fake_send(prompt):
        yield {"type": "system", "session_id": "abc"}
        yield {"type": "result", "is_error": False, "result": "ok"}

    fake_session = AsyncMock()
    fake_session.session_id = "abc"
    fake_session.send = fake_send

    fake_manager = AsyncMock()
    fake_manager.get_or_create = AsyncMock(return_value=fake_session)
    fake_manager.register = lambda s: None

    with patch.dict(client.app, {"session_manager": fake_manager}):
        resp = await client.post("/api/chat", json={"prompt": "hi", "cwd": str(tmp_path)})
    assert resp.status == 200
    await resp.text()  # drain the stream so the handler runs to completion
    # The app fixture pins permission_mode to 'bypassPermissions'; the spawn site
    # must forward exactly that.
    _, kwargs = fake_manager.get_or_create.await_args
    assert kwargs["permission_mode"] == "bypassPermissions"


# --------------------------------------------------------------------------- #
# Sessions endpoints
#
# The sessions routes read from two module-level constants in
# server.routes.sessions:
#   - CLAUDE_PROJECTS_BASE: ~/.claude/projects (project dirs full of *.jsonl)
#   - WORKSPACE_DIR: ~/workspace/projects (real project dirs)
# Both are captured at import time, so we monkeypatch the module globals to
# tmp_path-backed dirs. The handlers look the names up as module globals at
# call time, so patching the attributes is sufficient.
# --------------------------------------------------------------------------- #
import server.routes.sessions as sessions_mod  # noqa: E402


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write a list of records as JSONL (one JSON object per line)."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _slug(path: Path) -> str:
    """Mirror Claude Code's cwd-to-project-dir encoding (/ -> -, prepend -)."""
    return "-" + str(path).lstrip("/").replace("/", "-")


@pytest.fixture
def projects_base(tmp_path: Path, monkeypatch) -> Path:
    """A tmp-backed ~/.claude/projects with a couple of project dirs.

    Layout:
      <base>/<home_bucket_slug>/    <- matches app['default_cwd'] (tmp_path)
          interactive.jsonl         (first line type=mode)         -> kept
          renamed.jsonl             (first line type=custom-title) -> kept
          printmode.jsonl           (first line type=queue-operation) -> filtered
      <base>/-some-other-proj/      <- a non-home project dir
          other.jsonl               (first line type=queue-operation) -> filtered
                                      (print-mode is hidden in every bucket, like
                                       claude --resume — even with real turns)
          real.jsonl                (first line type=mode, has a real turn) -> kept
    """
    base = tmp_path / "claude_projects"
    base.mkdir()
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(sessions_mod, "WORKSPACE_DIR", tmp_path / "workspace")

    # The home bucket is derived from app['default_cwd'], which the `app`
    # fixture sets to tmp_path. _is_interactive_session is only applied there.
    home_bucket = _slug(tmp_path)
    home_dir = base / home_bucket
    home_dir.mkdir()
    _write_jsonl(home_dir / "interactive.jsonl", [
        {"type": "mode", "mode": "default"},
        {"type": "ai-title", "aiTitle": "An AI generated title"},
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ])
    _write_jsonl(home_dir / "renamed.jsonl", [
        {"type": "custom-title", "customTitle": "First name"},
        {"type": "custom-title", "customTitle": "Final name"},
        {"type": "user", "message": {"content": "do a thing"}},
    ])
    _write_jsonl(home_dir / "printmode.jsonl", [
        {"type": "queue-operation"},
        {"type": "custom-title", "customTitle": "should stay hidden"},
    ])
    # Orphaned/empty session: interactive first line but no real turn -> filtered
    # by the _has_real_turn guard.
    _write_jsonl(home_dir / "empty.jsonl", [
        {"type": "mode", "mode": "default"},
        {"type": "custom-title", "customTitle": "started but never ran"},
    ])

    other_dir = base / "-some-other-proj"
    other_dir.mkdir()
    # Print-mode session in a NON-home dir, WITH a real turn: must still be
    # filtered (matches claude --resume, which never lists print-mode sessions).
    _write_jsonl(other_dir / "other.jsonl", [
        {"type": "queue-operation"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    # Genuine interactive session in the same non-home dir: kept.
    _write_jsonl(other_dir / "real.jsonl", [
        {"type": "mode", "mode": "default"},
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "yo"}]}},
    ])

    return base


async def test_list_sessions_shape_and_home_bucket_filter(client, projects_base):
    resp = await client.get("/api/sessions")
    assert resp.status == 200
    data = await resp.json()

    # Shape: { sessions: [...], total: int }
    assert isinstance(data["sessions"], list)
    assert isinstance(data["total"], int)

    ids = {s["id"] for s in data["sessions"]}
    # Home-bucket print-mode session is filtered out...
    assert "printmode" not in ids
    # ...the orphaned/empty session (no real turn) is filtered out...
    assert "empty" not in ids
    # ...but interactive + renamed home sessions (with a real turn) are kept...
    assert "interactive" in ids
    assert "renamed" in ids
    # ...the print-mode session in a NON-home dir is ALSO filtered (even though it
    # has a real turn) — claude --resume never lists print-mode sessions...
    assert "other" not in ids
    # ...and a genuine interactive session in that non-home dir is kept.
    assert "real" in ids

    # total counts only the files that survived filtering (3 here).
    assert data["total"] == 3
    assert data["total"] == len(data["sessions"])

    # Each session carries the expected fields.
    for s in data["sessions"]:
        assert set(s) >= {"id", "title", "project", "projectPath", "date", "mtime", "size"}


async def test_list_sessions_project_traversal_rejected(client, projects_base, tmp_path):
    """GET /api/sessions?project=../../etc must not escape CLAUDE_PROJECTS_BASE.

    Validates task-1 fix #1: traversal in the project slug is rejected rather
    than letting CLAUDE_PROJECTS_BASE / '../../etc' resolve outside the base.
    """
    # Plant a file outside the base that a traversal could reach if unguarded.
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_jsonl(outside / "leak.jsonl", [{"type": "mode"}])

    resp = await client.get("/api/sessions", params={"project": "../../etc"})
    if resp.status == 200:
        # If not rejected outright, it must at least not have escaped the base.
        data = await resp.json()
        assert all(s["id"] != "leak" for s in data["sessions"])
    else:
        assert resp.status in (400, 404)

    # A traversal aimed squarely at the planted file must never leak it.
    rel = "../" * 8 + str(outside).lstrip("/")
    resp2 = await client.get("/api/sessions", params={"project": rel})
    if resp2.status == 200:
        data2 = await resp2.json()
        assert all(s["id"] != "leak" for s in data2["sessions"])
    else:
        assert resp2.status in (400, 404)


async def test_get_session_title_and_extract_last_custom_title(client, projects_base):
    # renamed.jsonl has two custom-title entries; the LAST one wins.
    resp = await client.get("/api/sessions/renamed/title")
    assert resp.status == 200
    data = await resp.json()
    assert data["sessionId"] == "renamed"
    assert data["title"] == "Final name"
    # The title response carries the session's cwd (decoded from its project-dir
    # slug) so the Chat page can resume in the correct directory. renamed lives
    # in the home bucket, whose slug decodes back to the home cwd.
    assert data["cwd"] == str(client.app["default_cwd"])


async def test_get_session_title_cwd_matches_project_dir(client, projects_base):
    """A session in a non-home project dir reports that project's cwd, not home.

    Regression guard for the resume bug: the Chat page must resume in the
    directory the session was created under (claude --resume is cwd-scoped).
    """
    resp = await client.get("/api/sessions/other/title")
    assert resp.status == 200
    data = await resp.json()
    # `other` lives in base/-some-other-proj, which decodes to /some/other/proj.
    assert data["cwd"] == "/some/other/proj"


async def test_set_session_title_round_trips(client, projects_base):
    resp = await client.put("/api/sessions/interactive/title", json={"title": "Renamed via API"})
    assert resp.status == 200
    assert (await resp.json())["title"] == "Renamed via API"

    # GET reflects the newly appended custom-title.
    resp2 = await client.get("/api/sessions/interactive/title")
    assert (await resp2.json())["title"] == "Renamed via API"

    # Append another rename; the LAST custom-title must win.
    await client.put("/api/sessions/interactive/title", json={"title": "Renamed again"})
    resp3 = await client.get("/api/sessions/interactive/title")
    assert (await resp3.json())["title"] == "Renamed again"


async def test_set_session_title_rejects_empty(client, projects_base):
    resp = await client.put("/api/sessions/interactive/title", json={"title": "   "})
    assert resp.status == 400


async def test_set_session_title_404_for_unknown(client, projects_base):
    resp = await client.put("/api/sessions/does-not-exist/title", json={"title": "x"})
    assert resp.status == 404


async def test_delete_session_removes_file(client, projects_base):
    home_dir = projects_base / _slug_from_default(client)
    target = home_dir / "interactive.jsonl"
    assert target.exists()

    resp = await client.delete("/api/sessions/interactive")
    assert resp.status == 200
    assert (await resp.json()) == {"deleted": "interactive"}
    assert not target.exists()


async def test_delete_session_traversal_rejected(client, projects_base, tmp_path):
    """DELETE /api/sessions/{id} with a traversal id must be rejected.

    Validates task-1 fix #2: a session id containing '..' / '/' is rejected
    before being joined as f'{id}.jsonl', so it can't delete arbitrary files.
    """
    # Plant a sentinel the traversal would target if unguarded.
    sentinel = tmp_path / "victim.jsonl"
    sentinel.write_text("{}\n", encoding="utf-8")

    # URL-encode the slashes so the segment survives to match_info as a
    # traversal-bearing id rather than being collapsed by URL normalization.
    rel = "../" * 8 + str(sentinel).lstrip("/")[:-len(".jsonl")]
    encoded = rel.replace("/", "%2F")
    resp = await client.delete(f"/api/sessions/{encoded}")
    assert resp.status in (400, 404)
    # The sentinel must still be there regardless.
    assert sentinel.exists()


async def test_get_transcript_paginates(client, projects_base):
    home_dir = projects_base / _slug_from_default(client)
    records = [{"type": "user", "i": i} for i in range(10)]
    _write_jsonl(home_dir / "longsess.jsonl", records)

    # First page.
    resp = await client.get("/api/sessions/longsess/transcript", params={"limit": "3", "offset": "0"})
    assert resp.status == 200
    data = await resp.json()
    assert data["limit"] == 3 and data["offset"] == 0
    assert [m["i"] for m in data["messages"]] == [0, 1, 2]

    # Second page via offset.
    resp2 = await client.get("/api/sessions/longsess/transcript", params={"limit": "3", "offset": "3"})
    data2 = await resp2.json()
    assert [m["i"] for m in data2["messages"]] == [3, 4, 5]

    # total reflects the full record count regardless of the page window.
    assert data["total"] == 10


async def test_get_transcript_tail_returns_latest(client, projects_base):
    """tail=true returns the LAST `limit` records — what the transcript viewer
    needs so a long session shows recent activity, not the oldest lines.
    """
    home_dir = projects_base / _slug_from_default(client)
    records = [{"type": "user", "i": i} for i in range(10)]
    _write_jsonl(home_dir / "tailsess.jsonl", records)

    # tail with limit 3 -> the last 3 records [7, 8, 9].
    resp = await client.get(
        "/api/sessions/tailsess/transcript",
        params={"limit": "3", "offset": "0", "tail": "true"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["total"] == 10
    assert [m["i"] for m in data["messages"]] == [7, 8, 9]

    # tail offset pages backward toward older records: offset 3 -> [4, 5, 6].
    resp2 = await client.get(
        "/api/sessions/tailsess/transcript",
        params={"limit": "3", "offset": "3", "tail": "true"},
    )
    data2 = await resp2.json()
    assert [m["i"] for m in data2["messages"]] == [4, 5, 6]


async def test_get_transcript_404_for_unknown(client, projects_base):
    resp = await client.get("/api/sessions/nope/transcript")
    assert resp.status == 404


def _slug_from_default(client) -> str:
    """Compute the home-bucket slug from the test app's default_cwd."""
    return "-" + str(client.app["default_cwd"]).lstrip("/").replace("/", "-")


# --------------------------------------------------------------------------- #
# Projects endpoint (/api/projects)
#
# This is the surface that broke: list_projects derives a slug from each
# workspace project path and looks up the matching ~/.claude/projects/<slug>
# dir to count sessions/memories. A path-resolution regression (e.g. not
# following a symlinked $HOME) makes the slug mismatch and all counts go to 0.
# These tests build an isolated workspace + projects layout and assert the
# counts line up.
# --------------------------------------------------------------------------- #


@pytest.fixture
def projects_layout(tmp_path: Path, monkeypatch):
    """Build an isolated workspace + ~/.claude/projects + config layout.

    Returns a dict with the workspace dir and the project names created.

    Workspace projects:
      alpha/  -> CLAUDE.md, agent-sops/a.md, skills/s.md, docs/d.md
                 2 sessions, 3 memory files
      beta/   -> .claude/settings.json, 1 session, 0 memory
      gamma/  -> no claude activity (0/0)
      .hidden/-> must be skipped
    """
    workspace = tmp_path / "workspace" / "projects"
    workspace.mkdir(parents=True)
    claude_base = tmp_path / "dot_claude" / "projects"
    claude_base.mkdir(parents=True)
    config_path = tmp_path / "claude-web-config.json"

    monkeypatch.setattr(sessions_mod, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", claude_base)
    monkeypatch.setenv("CLAUDE_WEB_CONFIG", str(config_path))

    # Isolate the global ("universal") agents dir so tests don't pick up the
    # developer's real ~/.claude/agents/ contents.
    import server.routes.agents as agents_mod
    global_agents = tmp_path / "dot_claude" / "agents"
    monkeypatch.setattr(agents_mod, "GLOBAL_AGENTS_DIR", global_agents)

    def slug_for(p: Path) -> str:
        return "-" + str(p).lstrip("/").replace("/", "-")

    # alpha: CLAUDE.md + sop/skill/docs + 2 sessions + 3 memories
    alpha = workspace / "alpha"
    alpha.mkdir()
    (alpha / "CLAUDE.md").write_text("# Alpha\n\nThe alpha project.\n", encoding="utf-8")
    (alpha / "README.md").write_text("# Alpha README\n\nHow to use alpha.\n", encoding="utf-8")
    (alpha / "agent-sops").mkdir()
    (alpha / "agent-sops" / "a.md").write_text("sop", encoding="utf-8")
    (alpha / "skills").mkdir()
    (alpha / "skills" / "s.md").write_text("skill", encoding="utf-8")
    (alpha / "docs").mkdir()
    (alpha / "docs" / "d.md").write_text("doc", encoding="utf-8")
    alpha_sessions = claude_base / slug_for(alpha)
    alpha_sessions.mkdir()
    _write_jsonl(alpha_sessions / "s1.jsonl", [{"type": "mode"}])
    _write_jsonl(alpha_sessions / "s2.jsonl", [{"type": "mode"}])
    alpha_mem = alpha_sessions / "memory"
    alpha_mem.mkdir()
    for name in ("m1.md", "m2.md", "m3.md"):
        (alpha_mem / name).write_text("mem", encoding="utf-8")

    # beta: settings.json + 1 session, no memory
    beta = workspace / "beta"
    beta.mkdir()
    (beta / ".claude").mkdir()
    (beta / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    beta_sessions = claude_base / slug_for(beta)
    beta_sessions.mkdir()
    _write_jsonl(beta_sessions / "b1.jsonl", [{"type": "mode"}])

    # gamma: no claude activity
    (workspace / "gamma").mkdir()

    # hidden dir must be skipped
    (workspace / ".hidden").mkdir()

    # config with project URLs (substring match)
    config_path.write_text(json.dumps({"projectUrls": {
        "alpha": [{"url": "http://127.0.0.1:9001", "label": "Local", "type": "local"}],
    }}), encoding="utf-8")

    return {"workspace": workspace, "claude_base": claude_base, "config": config_path}


async def _get_projects(client) -> dict:
    resp = await client.get("/api/projects")
    assert resp.status == 200
    return {p["name"]: p for p in await resp.json()}


async def test_config_endpoint_exposes_paths_and_projects(client, projects_layout):
    """GET /api/config surfaces home + slug + defaultCwd + workspace projects so
    the frontend can avoid hardcoded personal paths.
    """
    resp = await client.get("/api/config")
    assert resp.status == 200
    cfg = await resp.json()

    # Core fields present.
    assert set(cfg) >= {"home", "homeSlug", "defaultCwd", "workspaceDir", "projects"}

    # homeSlug is the slug encoding of the resolved home path.
    from pathlib import Path as _P
    expected_home = str(_P.home().resolve())
    assert cfg["home"] == expected_home
    assert cfg["homeSlug"] == sessions_mod._project_path_to_claude_slug(expected_home)

    # Projects list the workspace dirs (alpha/beta/gamma), skipping hidden, each
    # with a path and a slug that matches the encoding.
    names = {p["name"] for p in cfg["projects"]}
    assert names == {"alpha", "beta", "gamma"}
    for p in cfg["projects"]:
        assert p["slug"] == sessions_mod._project_path_to_claude_slug(p["path"])
    assert ".hidden" not in names


async def test_projects_lists_all_skips_hidden(client, projects_layout):
    projects = await _get_projects(client)
    assert set(projects) == {"alpha", "beta", "gamma"}
    assert ".hidden" not in projects


async def test_projects_session_and_memory_counts(client, projects_layout):
    projects = await _get_projects(client)
    assert projects["alpha"]["sessionCount"] == 2
    assert projects["alpha"]["memoryCount"] == 3
    assert {m["name"] for m in projects["alpha"]["memoryFiles"]} == {"m1.md", "m2.md", "m3.md"}
    assert projects["beta"]["sessionCount"] == 1
    assert projects["beta"]["memoryCount"] == 0
    assert projects["gamma"]["sessionCount"] == 0


async def test_projects_claude_md_and_settings(client, projects_layout):
    projects = await _get_projects(client)
    assert "The alpha project." in projects["alpha"]["claudeMd"]
    assert projects["alpha"]["hasSettings"] is False
    assert projects["beta"]["hasSettings"] is True
    assert projects["beta"]["claudeMd"] is None


async def test_project_id_path_traversal_rejected(client, projects_layout):
    """Regression: a URL-encoded ..%2f.. project_id must NOT escape the
    workspace. Before the _validate_project_id guard, this wrote a CLAUDE.md
    outside WORKSPACE_DIR (verified live). It must now 400 and write nothing.
    """
    workspace = projects_layout["workspace"]
    # ..%2f.. decodes to "../.." as one path segment → would resolve to
    # workspace.parent.parent. Assert the write is refused and nothing lands.
    escaped = workspace.parent.parent / "CLAUDE.md"
    assert not escaped.exists()
    resp = await client.put(
        "/api/projects/..%2f../claude-md", json={"content": "PWNED"}
    )
    assert resp.status == 400
    assert not escaped.exists()
    # The read/SOP/memory project endpoints share the guard.
    assert (await client.get("/api/projects/..%2f../sop/x.md")).status == 400
    assert (await client.get("/api/projects/..%2f../memory/x.md")).status == 400


# ── Design decisions (.claude/DESIGN.md) — Phase 1 of the Role Agents framework ──

_DESIGN_SAMPLE = """# alpha — design decisions

Some preamble that is not a decision and must be ignored.

## D-002: Use append-only context  (2026-06-13, accepted)
**Decision:** Agents only append.
**Why:** Can't corrupt existing notes.

## D-001: Native subagents  (2026-06-12, proposed)
**Decision:** Roles are .claude/agents/*.md.
"""


def test_parse_adrs_extracts_entries_and_metadata():
    from server.routes.agents import parse_adrs
    adrs = parse_adrs(_DESIGN_SAMPLE)
    assert [a["id"] for a in adrs] == ["D-002", "D-001"]
    assert adrs[0]["title"] == "Use append-only context"
    assert adrs[0]["date"] == "2026-06-13"
    assert adrs[0]["status"] == "accepted"
    assert "Agents only append." in adrs[0]["body"]
    # Preamble before the first header is not captured as a decision.
    assert "preamble" not in adrs[0]["body"].lower()
    # Status is lower-cased; proposed entries are detectable.
    assert adrs[1]["status"] == "proposed"


def test_parse_adrs_empty_and_headerless():
    from server.routes.agents import parse_adrs
    assert parse_adrs("") == []
    assert parse_adrs("# Just a title\n\nNo ADR headers here.") == []


async def test_get_project_design_returns_parsed_adrs(client, projects_layout):
    workspace = projects_layout["workspace"]
    (workspace / "alpha" / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / "alpha" / ".claude" / "DESIGN.md").write_text(_DESIGN_SAMPLE, encoding="utf-8")

    resp = await client.get("/api/projects/alpha/design")
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is True
    assert data["etag"] is not None
    assert [d["id"] for d in data["decisions"]] == ["D-002", "D-001"]


async def test_get_project_design_absent_is_empty_not_404(client, projects_layout):
    """A project with no DESIGN.md is a valid empty state, not an error."""
    resp = await client.get("/api/projects/beta/design")
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is False
    assert data["decisions"] == []


async def test_get_project_design_unknown_project_404(client, projects_layout):
    resp = await client.get("/api/projects/nope/design")
    assert resp.status == 404


async def test_get_project_design_traversal_rejected(client, projects_layout):
    resp = await client.get("/api/projects/..%2f../design")
    assert resp.status == 400


async def test_projects_listing_surfaces_design_summary(client, projects_layout):
    workspace = projects_layout["workspace"]
    (workspace / "alpha" / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / "alpha" / ".claude" / "DESIGN.md").write_text(_DESIGN_SAMPLE, encoding="utf-8")

    projects = await _get_projects(client)
    # alpha has a DESIGN.md with one proposed (D-001) decision.
    assert projects["alpha"]["hasDesignDoc"] is True
    assert projects["alpha"]["proposedDecisionCount"] == 1
    # beta has none — fields always present.
    assert projects["beta"]["hasDesignDoc"] is False
    assert projects["beta"]["proposedDecisionCount"] == 0


# ── Role agents (.claude/agents/*.md) — Phase 2 of the Role Agents framework ──

_AGENT_SAMPLE = """---
name: backend-dev
description: Backend developer for alpha.
tools: Read, Edit, Bash
model: opus
---
You are the backend developer.
"""


def _seed_agent(workspace: Path, project: str, name: str, content: str = _AGENT_SAMPLE):
    agents_dir = workspace / project / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(content, encoding="utf-8")


async def test_list_agents_parses_frontmatter(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")

    resp = await client.get("/api/projects/alpha/agents")
    assert resp.status == 200
    agents = await resp.json()
    assert len(agents) == 1
    a = agents[0]
    assert a["name"] == "backend-dev"
    assert a["description"] == "Backend developer for alpha."
    assert a["model"] == "opus"
    assert a["tools"] == ["Read", "Edit", "Bash"]
    assert a["contextExists"] is False


async def test_list_agents_empty_when_none(client, projects_layout):
    resp = await client.get("/api/projects/beta/agents")
    assert resp.status == 200
    assert await resp.json() == []


async def test_list_agents_skips_unreadable_file(client, projects_layout):
    """A non-UTF-8 agent file must be skipped, not 500 the whole listing."""
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "good", "---\nname: good\ndescription: ok\n---\nbody\n")
    bad = workspace / "alpha" / ".claude" / "agents" / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00bad")

    resp = await client.get("/api/projects/alpha/agents")
    assert resp.status == 200
    names = {a["name"] for a in await resp.json()}
    assert "good" in names  # the readable one survives the unreadable sibling


async def test_get_agent_returns_content_and_etag(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")

    resp = await client.get("/api/projects/alpha/agents/backend-dev")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "backend-dev"
    assert data["tools"] == ["Read", "Edit", "Bash"]
    assert data["etag"] is not None
    assert "You are the backend developer." in data["content"]


async def test_get_agent_unknown_404(client, projects_layout):
    resp = await client.get("/api/projects/alpha/agents/nope")
    assert resp.status == 404


async def test_agents_traversal_rejected(client, projects_layout):
    # Bad project id.
    assert (await client.get("/api/projects/..%2f../agents")).status == 400
    # Bad agent name.
    assert (await client.get("/api/projects/alpha/agents/..%2f..%2fsecret")).status == 400


async def test_projects_listing_surfaces_agent_count(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_agent(workspace, "alpha", "qa")

    projects = await _get_projects(client)
    assert projects["alpha"]["agentCount"] == 2
    assert projects["beta"]["agentCount"] == 0


# ── Dual-scope agents: project + global ("universal") ────────────────────────

def _seed_global_agent(name: str, content: str = _AGENT_SAMPLE):
    import server.routes.agents as agents_mod
    agents_mod.GLOBAL_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    (agents_mod.GLOBAL_AGENTS_DIR / f"{name}.md").write_text(content, encoding="utf-8")


async def test_global_agents_visible_in_every_project(client, projects_layout):
    _seed_global_agent("universal-helper")
    # A global agent shows up for a project with no project agents of its own.
    resp = await client.get("/api/projects/beta/agents")
    assert resp.status == 200
    agents = {a["slug"]: a for a in await resp.json()}
    assert "universal-helper" in agents
    assert agents["universal-helper"]["scope"] == "global"


async def test_project_agent_shadows_global_same_name(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_global_agent("backend-dev")
    resp = await client.get("/api/projects/alpha/agents")
    agents = [a for a in await resp.json() if a["slug"] == "backend-dev"]
    # Only one entry, and the project one wins.
    assert len(agents) == 1
    assert agents[0]["scope"] == "project"


async def test_agent_count_counts_project_and_global_union(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_global_agent("backend-dev")  # shadowed — counts once
    _seed_global_agent("universal-helper")
    projects = await _get_projects(client)
    # alpha sees: backend-dev (project, shadows global) + universal-helper = 2
    assert projects["alpha"]["agentCount"] == 2
    # beta has no project agents: sees both globals = 2
    assert projects["beta"]["agentCount"] == 2


async def test_promote_agent_to_global(client, projects_layout):
    import server.routes.agents as agents_mod
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")

    resp = await client.post("/api/projects/alpha/agents/backend-dev/scope", json={"scope": "global"})
    assert resp.status == 200
    assert (await resp.json())["moved"] is True
    # File moved out of the project, into global.
    assert not (workspace / "alpha" / ".claude" / "agents" / "backend-dev.md").exists()
    assert (agents_mod.GLOBAL_AGENTS_DIR / "backend-dev.md").is_file()
    # Now it lists as global for the project.
    agents = {a["slug"]: a for a in await (await client.get("/api/projects/alpha/agents")).json()}
    assert agents["backend-dev"]["scope"] == "global"


async def test_demote_global_agent_to_project(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_global_agent("universal-helper")

    resp = await client.post("/api/projects/alpha/agents/universal-helper/scope", json={"scope": "project"})
    assert resp.status == 200
    assert (workspace / "alpha" / ".claude" / "agents" / "universal-helper.md").is_file()


async def test_scope_move_conflict_when_dest_exists(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_global_agent("backend-dev")
    # Promoting the project agent would collide with the existing global one.
    resp = await client.post("/api/projects/alpha/agents/backend-dev/scope", json={"scope": "global"})
    assert resp.status == 409


async def test_scope_rejects_bad_value_and_traversal(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    assert (await client.post("/api/projects/alpha/agents/backend-dev/scope", json={"scope": "nope"})).status == 400
    assert (await client.post("/api/projects/..%2f../agents/x/scope", json={"scope": "global"})).status == 400


# ── Append-only context layer + conflict review — Phase 3 ────────────────────

_CONTEXT_SAMPLE = """# backend-dev — project context

- 2026-06-01: Use the read_json_body helper for body parsing; raw request.json() returns 500. (B12)
- 2026-06-05: Tests share a module-level cache; reset it in an autouse fixture.
- 2026-06-12: filestore.write_text takes an expected_etag for optimistic concurrency.
- 2026-06-13: Supersedes the 2026-06-05 note — the cache reset must use a fresh _Cache() instance, not clear. (correction)
"""


def _seed_context(workspace: Path, project: str, name: str, content: str = _CONTEXT_SAMPLE):
    ctx_dir = workspace / project / ".claude" / "agent-context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / f"{name}.md").write_text(content, encoding="utf-8")


def test_parse_context_entries_dates_and_text():
    from server.routes.agents import parse_context_entries
    entries = parse_context_entries(_CONTEXT_SAMPLE)
    assert len(entries) == 4
    assert entries[0]["date"] == "2026-06-01"
    assert "read_json_body" in entries[0]["text"]
    # Heading line is not an entry.
    assert all("project context" not in e["text"] for e in entries)


def test_detect_conflicts_clusters_supersede_and_redundancy():
    from server.routes.agents import parse_context_entries, detect_conflicts
    entries = parse_context_entries(_CONTEXT_SAMPLE)
    clusters = detect_conflicts(entries)
    # The 06-05 "cache/autouse/reset" note and the 06-13 correction about the
    # cache must cluster together, flagged as superseded (explicit marker).
    assert any(
        c["reason"] == "superseded" and 1 in c["entryIndices"] and 3 in c["entryIndices"]
        for c in clusters
    ), clusters


def test_detect_conflicts_none_when_unrelated():
    from server.routes.agents import parse_context_entries, detect_conflicts
    txt = "# x\n- 2026-01-01: Frontend uses Vite.\n- 2026-01-02: Database is Postgres.\n"
    assert detect_conflicts(parse_context_entries(txt)) == []


async def test_get_agent_context_reports_new_entries(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")

    resp = await client.get("/api/projects/alpha/agents/backend-dev/context")
    assert resp.status == 200
    data = await resp.json()
    assert data["exists"] is True
    assert len(data["entries"]) == 4
    # Never reviewed → all entries are new.
    assert data["newEntryCount"] == 4


async def test_context_conflicts_endpoint(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")

    resp = await client.get("/api/projects/alpha/agents/backend-dev/context/conflicts")
    assert resp.status == 200
    clusters = (await resp.json())["clusters"]
    assert len(clusters) >= 1
    assert "entries" in clusters[0]  # embedded for the UI


async def test_reconcile_keep_removes_others(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"

    # Keep entry 1, drop entry 3 (the cache cluster).
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "keep", "entryIndices": [1, 3]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["removed"] == 1
    # The correction line is gone; the kept note remains; unrelated entries stay.
    text = ctx_path.read_text(encoding="utf-8")
    assert "fresh _Cache() instance" not in text
    assert "autouse fixture" in text
    assert "read_json_body" in text


async def test_reconcile_merge_replaces_cluster(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"

    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "merge", "entryIndices": [1, 3], "mergedText": "Reset the cache with a fresh _Cache() in an autouse fixture."},
    )
    assert resp.status == 200
    text = ctx_path.read_text(encoding="utf-8")
    assert "Reset the cache with a fresh _Cache() in an autouse fixture." in text
    # Two entries collapsed into one → 3 entries remain.
    from server.routes.agents import parse_context_entries
    assert len(parse_context_entries(text)) == 3


async def test_reconcile_dismiss_removes_nothing(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before = ctx_path.read_text(encoding="utf-8")

    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "dismiss", "entryIndices": [1, 3]},
    )
    assert resp.status == 200
    assert (await resp.json())["removed"] == 0
    assert ctx_path.read_text(encoding="utf-8") == before  # untouched


async def test_dismiss_persists_so_cluster_stops_flagging(client, projects_layout):
    """Regression: 'Keep all' must remember the cluster so it doesn't re-flag.

    Previously dismiss returned ok but recorded nothing, so the same conflict
    re-appeared on every reload.
    """
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")

    # Detect the cluster, then dismiss it.
    clusters = (await (await client.get("/api/projects/alpha/agents/backend-dev/context/conflicts")).json())["clusters"]
    assert len(clusters) >= 1
    target = clusters[0]
    await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "dismiss", "entryIndices": target["entryIndices"]},
    )

    # The dismissed cluster no longer appears, and the agent's conflict count drops.
    after = (await (await client.get("/api/projects/alpha/agents/backend-dev/context/conflicts")).json())["clusters"]
    assert all(c["entryIndices"] != target["entryIndices"] for c in after)
    agents = {a["slug"]: a for a in await (await client.get("/api/projects/alpha/agents")).json()}
    assert agents["backend-dev"]["conflictClusterCount"] == len(after)


async def test_reconcile_rejects_bad_action(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "nuke", "entryIndices": [0]},
    )
    assert resp.status == 400


async def test_mark_reviewed_clears_new_count(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")

    assert (await client.post("/api/projects/alpha/agents/backend-dev/context/mark-reviewed")).status == 200
    resp = await client.get("/api/projects/alpha/agents/backend-dev/context")
    assert (await resp.json())["newEntryCount"] == 0


async def test_context_traversal_rejected(client, projects_layout):
    assert (await client.get("/api/projects/alpha/agents/..%2f..%2fx/context")).status == 400
    assert (await client.post("/api/projects/..%2f../agents/x/context/mark-reviewed")).status == 400


async def test_put_design_accepts_proposed_adr(client, projects_layout):
    workspace = projects_layout["workspace"]
    (workspace / "alpha" / ".claude").mkdir(parents=True, exist_ok=True)
    design_path = workspace / "alpha" / ".claude" / "DESIGN.md"
    design_path.write_text(_DESIGN_SAMPLE, encoding="utf-8")

    # Read to get etag, flip D-001 proposed → accepted, PUT back.
    get = await (await client.get("/api/projects/alpha/design")).json()
    new_content = get["content"].replace("2026-06-12, proposed", "2026-06-12, accepted")
    resp = await client.put("/api/projects/alpha/design", json={"content": new_content, "etag": get["etag"]})
    assert resp.status == 200
    decisions = {d["id"]: d for d in (await resp.json())["decisions"]}
    assert decisions["D-001"]["status"] == "accepted"


async def test_put_design_etag_conflict(client, projects_layout):
    workspace = projects_layout["workspace"]
    (workspace / "alpha" / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / "alpha" / ".claude" / "DESIGN.md").write_text(_DESIGN_SAMPLE, encoding="utf-8")
    resp = await client.put("/api/projects/alpha/design", json={"content": "x", "etag": "stale-etag-value"})
    assert resp.status == 409


async def test_projects_listing_surfaces_unreviewed_entries(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    projects = await _get_projects(client)
    assert projects["alpha"]["unreviewedAgentEntries"] == 4


async def test_projects_readme_inlined(client, projects_layout):
    """A root README is inlined as `readme` with its filename in `readmeName`;
    projects without one report null for both (field always present)."""
    projects = await _get_projects(client)
    # alpha has a README.md.
    assert "How to use alpha." in projects["alpha"]["readme"]
    assert projects["alpha"]["readmeName"] == "README.md"
    # beta/gamma have no README — both fields present and null.
    for name in ("beta", "gamma"):
        assert projects[name]["readme"] is None
        assert projects[name]["readmeName"] is None


def test_read_project_readme_prefers_and_labels_variant(tmp_path):
    """The helper finds a README, returns its real filename, and yields
    (None, None) when there's none."""
    proj = tmp_path / "p"
    proj.mkdir()
    assert sessions_mod._read_project_readme(proj) == (None, None)
    (proj / "README.rst").write_text("rst readme", encoding="utf-8")
    name, content = sessions_mod._read_project_readme(proj)
    assert name == "README.rst" and content == "rst readme"
    # README.md takes precedence over README.rst when both exist.
    (proj / "README.md").write_text("md readme", encoding="utf-8")
    name2, content2 = sessions_mod._read_project_readme(proj)
    assert name2 == "README.md" and content2 == "md readme"


async def test_projects_sop_files_collected(client, projects_layout):
    projects = await _get_projects(client)
    names = {f["name"] for f in projects["alpha"]["sopFiles"]}
    assert names == {"a.md", "s.md", "d.md"}


async def test_projects_app_urls_from_config(client, projects_layout):
    projects = await _get_projects(client)
    alpha_urls = projects["alpha"]["appUrls"]
    assert len(alpha_urls) == 1
    assert alpha_urls[0]["url"] == "http://127.0.0.1:9001"
    assert projects["alpha"]["appUrl"] == "http://127.0.0.1:9001"
    # No config entry for beta -> empty list and null appUrl.
    assert projects["beta"]["appUrls"] == []
    assert projects["beta"]["appUrl"] is None


async def test_projects_slug_roundtrips_to_session_dir(client, projects_layout):
    """The slug derived from each emitted project path must match the on-disk
    session dir name. This is the invariant the symlink bug violated: when the
    emitted path didn't resolve symlinks, its slug didn't match the stored dir
    and counts silently went to 0.
    """
    claude_base = projects_layout["claude_base"]
    projects = await _get_projects(client)
    for name in ("alpha", "beta"):
        slug = sessions_mod._project_path_to_claude_slug(projects[name]["path"])
        assert (claude_base / slug).is_dir(), (
            f"slug {slug!r} for project {name} does not match an on-disk session dir"
        )


async def test_projects_last_activity_presence_and_type(client, projects_layout):
    """`lastActivity` is present on every project; it's a formatted string for
    projects with sessions and None for projects with none."""
    projects = await _get_projects(client)
    for name in ("alpha", "beta", "gamma"):
        assert "lastActivity" in projects[name]

    # alpha (2 sessions) and beta (1 session) get a formatted timestamp string.
    assert isinstance(projects["alpha"]["lastActivity"], str)
    assert projects["alpha"]["lastActivity"]
    assert isinstance(projects["beta"]["lastActivity"], str)
    assert projects["beta"]["lastActivity"]

    # gamma has no Claude activity -> no timestamp.
    assert projects["gamma"]["lastActivity"] is None


async def test_projects_last_activity_reflects_newest_session_mtime(client, projects_layout):
    """`lastActivity` is derived from the most recent .jsonl mtime in the
    project's session dir, formatted as '%b %d %H:%M'."""
    claude_base = projects_layout["claude_base"]
    alpha_dir = claude_base / sessions_mod._project_path_to_claude_slug(
        str(projects_layout["workspace"] / "alpha")
    )
    # Make s2.jsonl the newest file with a fixed, known mtime.
    newest = alpha_dir / "s2.jsonl"
    os.utime(alpha_dir / "s1.jsonl", (1_700_000_000, 1_700_000_000))
    os.utime(newest, (1_700_100_000, 1_700_100_000))

    projects = await _get_projects(client)
    from datetime import datetime
    expected = datetime.fromtimestamp(1_700_100_000).strftime("%b %d %H:%M")
    assert projects["alpha"]["lastActivity"] == expected


async def test_projects_sorted_by_recency_with_nulls_last(client, projects_layout):
    """Projects are returned most-recent-first; projects without activity
    (null lastActivity) sort to the end. The internal `_mtime` sort key must
    not leak into the response."""
    claude_base = projects_layout["claude_base"]
    alpha_dir = claude_base / sessions_mod._project_path_to_claude_slug(
        str(projects_layout["workspace"] / "alpha")
    )
    beta_dir = claude_base / sessions_mod._project_path_to_claude_slug(
        str(projects_layout["workspace"] / "beta")
    )
    # beta's session is newer than any of alpha's -> beta sorts before alpha.
    for f in alpha_dir.glob("*.jsonl"):
        os.utime(f, (1_700_000_000, 1_700_000_000))
    os.utime(beta_dir / "b1.jsonl", (1_700_200_000, 1_700_200_000))

    resp = await client.get("/api/projects")
    assert resp.status == 200
    ordered = await resp.json()
    names = [p["name"] for p in ordered]

    # beta (newest) before alpha (older); gamma (no activity) last.
    assert names.index("beta") < names.index("alpha")
    assert names[-1] == "gamma"

    # The internal sort key must never be exposed.
    for p in ordered:
        assert "_mtime" not in p


# --------------------------------------------------------------------------- #
# codeUrl: GitFarm origin remote -> code.amazon.com package URL
#
# list_projects reads each workspace project's .git/config origin remote and,
# when it is a git.amazon.com/pkg/<Package> URL, surfaces
# https://code.amazon.com/packages/<Package> as `codeUrl`. Projects whose
# remote isn't a GitFarm package URL (or that aren't git repos) get null.
# --------------------------------------------------------------------------- #


def _write_git_remote(project_dir: Path, url: str) -> None:
    """Plant a minimal .git/config with an origin remote pointing at `url`."""
    git_dir = project_dir / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n'
        f'[remote "origin"]\n\turl = {url}\n'
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
        encoding="utf-8",
    )


async def test_projects_code_url_present_on_every_project(client, projects_layout):
    """`codeUrl` is present on every project object (string or null)."""
    projects = await _get_projects(client)
    for name in ("alpha", "beta", "gamma"):
        assert "codeUrl" in projects[name]


async def test_projects_code_url_maps_gitfarm_remote(client, projects_layout):
    """A git.amazon.com/pkg/<Pkg> origin maps to the code.amazon.com URL.

    Mirrors the real claude-web (ClaudeCodeConsole) and oncall-agent
    (OncallAgent) mappings the UAT checks.
    """
    workspace = projects_layout["workspace"]
    _write_git_remote(workspace / "alpha", "ssh://git.amazon.com/pkg/ClaudeCodeConsole")
    _write_git_remote(
        workspace / "beta", "https://git.amazon.com/pkg/OncallAgent"
    )

    projects = await _get_projects(client)
    assert projects["alpha"]["codeUrl"] == "https://code.amazon.com/packages/ClaudeCodeConsole"
    assert (
        projects["beta"]["codeUrl"]
        == "https://code.amazon.com/packages/OncallAgent"
    )


async def test_projects_code_url_null_without_gitfarm_remote(client, projects_layout):
    """Projects lacking a git.amazon.com/pkg remote get codeUrl null.

    Covers three cases: no git repo at all (gamma), and a non-GitFarm remote
    (alpha pointed at github). Both must yield null rather than a bogus URL.
    """
    workspace = projects_layout["workspace"]
    _write_git_remote(workspace / "alpha", "git@github.com:someuser/somerepo.git")

    projects = await _get_projects(client)
    # gamma has no .git at all.
    assert projects["gamma"]["codeUrl"] is None
    # alpha's remote isn't a GitFarm package URL.
    assert projects["alpha"]["codeUrl"] is None


def test_code_url_strips_dot_git_suffix(tmp_path):
    """A trailing .git on the package segment is stripped from the package name."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_git_remote(proj, "ssh://git.amazon.com/pkg/ClaudeCodeConsole.git")
    urls = sessions_mod._code_urls_for_project(proj)
    assert urls == [{"name": "ClaudeCodeConsole",
                     "url": "https://code.amazon.com/packages/ClaudeCodeConsole"}]


def test_code_url_matches_remote_with_port(tmp_path):
    """A git.amazon.com:2222/pkg/<Pkg> remote (ssh-with-port) still maps."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_git_remote(proj, "ssh://git.amazon.com:2222/pkg/XiaoGeLao")
    urls = sessions_mod._code_urls_for_project(proj)
    assert urls == [{"name": "XiaoGeLao",
                     "url": "https://code.amazon.com/packages/XiaoGeLao"}]


def test_code_urls_scans_nested_brazil_repos(tmp_path):
    """When the project root isn't a git repo, nested package repos are found.

    Mirrors a Brazil workspace wrapper: <project>/workspace_<ts>/src/<Pkg>/.git.
    Multiple nested repos all surface, deduped, as {name, url} entries.
    """
    proj = tmp_path / "agentify-xiaogelao"
    src = proj / "workspace_2026" / "src"
    src.mkdir(parents=True)
    _write_git_remote(src / "XiaoGeLao", "ssh://git.amazon.com:2222/pkg/XiaoGeLao")
    _write_git_remote(src / "XiaoGeLaoFrontend", "ssh://git.amazon.com:2222/pkg/XiaoGeLaoFrontend")

    urls = sessions_mod._code_urls_for_project(proj)
    names = {u["name"] for u in urls}
    assert names == {"XiaoGeLao", "XiaoGeLaoFrontend"}
    assert all(u["url"].startswith("https://code.amazon.com/packages/") for u in urls)


async def test_projects_code_urls_field_present(client, projects_layout):
    """Every project carries codeUrls (list) and codeUrl (first/back-compat)."""
    workspace = projects_layout["workspace"]
    _write_git_remote(workspace / "alpha", "ssh://git.amazon.com/pkg/ClaudeCodeConsole")
    projects = await _get_projects(client)
    for name in ("alpha", "beta", "gamma"):
        assert "codeUrls" in projects[name]
        assert isinstance(projects[name]["codeUrls"], list)
    # alpha's single repo: codeUrl mirrors codeUrls[0].
    assert projects["alpha"]["codeUrl"] == "https://code.amazon.com/packages/ClaudeCodeConsole"
    assert projects["alpha"]["codeUrls"][0]["url"] == projects["alpha"]["codeUrl"]


def test_code_urls_for_non_git_dir_is_empty(tmp_path):
    """A directory that isn't a git repo (and has no nested repos) yields []."""
    proj = tmp_path / "plain"
    proj.mkdir()
    assert sessions_mod._code_urls_for_project(proj) == []


# --------------------------------------------------------------------------- #
# Workspace resolution (the symlink regression guard)
# --------------------------------------------------------------------------- #


def test_resolve_workspace_dir_honors_env(monkeypatch, tmp_path):
    real = tmp_path / "real_workspace"
    real.mkdir()
    monkeypatch.setenv("CLAUDE_WEB_WORKSPACE", str(real))
    assert sessions_mod._resolve_workspace_dir() == real.resolve()


def test_resolve_workspace_dir_follows_symlink(monkeypatch, tmp_path):
    """REGRESSION GUARD for the empty-counts bug.

    On Cloud Desktops $HOME (/home/<user>) is a symlink to /local/home/<user>,
    but Claude Code records session cwds under the REAL path. If the workspace
    dir isn't symlink-resolved, the derived slug won't match the stored
    ~/.claude/projects/<slug> dir and all session/memory counts go to 0.

    This test points CLAUDE_WEB_WORKSPACE at a SYMLINKED path and asserts the
    resolver returns the REAL path. It fails if `.resolve()` is dropped from
    _resolve_workspace_dir — i.e. it actually guards the regression.
    """
    real = tmp_path / "real_home" / "workspace" / "projects"
    real.mkdir(parents=True)
    link = tmp_path / "linked_home"
    link.symlink_to(tmp_path / "real_home")
    linked_workspace = link / "workspace" / "projects"

    # Sanity: the symlinked path is a different string than the real path.
    assert str(linked_workspace) != str(real)

    monkeypatch.setenv("CLAUDE_WEB_WORKSPACE", str(linked_workspace))
    resolved = sessions_mod._resolve_workspace_dir()

    # Must resolve through the symlink to the real path.
    assert resolved == real.resolve()
    # And the slug derived from the resolved path must match the real-path slug,
    # which is what Claude Code actually stores.
    assert (
        sessions_mod._project_path_to_claude_slug(str(resolved))
        == sessions_mod._project_path_to_claude_slug(str(real.resolve()))
    )


def test_load_project_urls_config_substring_match(monkeypatch, tmp_path):
    """Pin the documented substring-match behavior of project URL lookup.

    A config key is matched if it is a substring of the project name, so
    'claude-web' matches both 'claude-web' and 'claude-web-frontend'.
    """
    config_path = tmp_path / "cfg.json"
    config_path.write_text(json.dumps({"projectUrls": {
        "claude-web": [{"url": "http://x", "label": "L", "type": "local"}],
    }}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_WEB_CONFIG", str(config_path))

    assert sessions_mod._app_urls_for_project("claude-web")[0]["url"] == "http://x"
    # Substring match: the frontend project also matches the 'claude-web' key.
    assert sessions_mod._app_urls_for_project("claude-web-frontend")[0]["url"] == "http://x"
    # No match -> empty list.
    assert sessions_mod._app_urls_for_project("unrelated") == []


# --------------------------------------------------------------------------- #
# PersistentSession lock release on abandoned stream
#
# session.send() holds self._lock across its yield loop. If a client disconnects
# mid-turn the consumer abandons the async generator; without an explicit
# aclose() the lock would only release on lazy GC finalization, so a fast
# follow-up request reusing the session could block forever on the held lock
# (asyncio.Lock has no acquire timeout). The chat route now calls gen.aclose()
# in a finally; this test pins that the lock is released deterministically.
# --------------------------------------------------------------------------- #
import server.session_manager as session_mod  # noqa: E402


class _FakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass


class _FakeStdout:
    """Yields a few JSONL event lines, slowly, then a result line."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        await asyncio.sleep(0)  # let the consumer interleave
        return self._lines.pop(0)


class _FakeProc:
    returncode = None  # mimics a live process (so .alive is True)

    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)


def _make_fake_session():
    s = session_mod.PersistentSession(session_id="sess-1")
    lines = [
        (json.dumps({"type": "system", "session_id": "sess-1"}) + "\n").encode(),
        (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n").encode(),
        (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": " there"}]}}) + "\n").encode(),
        (json.dumps({"type": "result", "is_error": False, "result": "hi there"}) + "\n").encode(),
    ]
    s._proc = _FakeProc(lines)
    s._started = True
    return s


async def test_send_releases_lock_when_consumer_finishes_normally():
    s = _make_fake_session()
    got = [evt async for evt in s.send("hello")]
    assert any(e.get("type") == "result" for e in got)
    assert not s._lock.locked()


async def test_send_releases_lock_on_aclose_after_abandon():
    """Abandon the stream mid-turn (like a client disconnect), then aclose().

    This is the regression guard: after aclose(), the lock MUST be free so the
    next request reusing the session can proceed. Without the chat route's
    finally: gen.aclose(), the lock would stay held until GC finalization.
    """
    s = _make_fake_session()
    gen = s.send("hello")
    got = []
    async for evt in gen:
        got.append(evt)
        if evt.get("type") == "assistant":
            break  # client disconnects mid-stream

    # The generator is suspended at a yield, still holding the lock.
    assert s._lock.locked()

    # The fix: explicitly close the abandoned generator.
    await gen.aclose()

    # Lock is now released deterministically — a second send can acquire it.
    assert not s._lock.locked()
    await asyncio.wait_for(s._lock.acquire(), timeout=1.0)
    s._lock.release()


# --------------------------------------------------------------------------- #
# Auth-error classification + recovery
#
# The claude subprocess routes through Bedrock; expired AWS/Midway creds surface
# as a 403 "security token ... invalid". We classify those distinctly (so the UI
# can show actionable guidance + a bounded retry) and support respawning the
# subprocess to pick up fresh credentials.
# --------------------------------------------------------------------------- #

def test_is_auth_error_matches_credential_failures():
    assert session_mod.is_auth_error("API Error: 403 The security token included in the request is invalid")
    assert session_mod.is_auth_error("ExpiredToken: token has expired")
    assert session_mod.is_auth_error("UnrecognizedClientException: invalid security token")
    assert session_mod.is_auth_error("403 Forbidden — credentials could not be verified")


def test_is_auth_error_ignores_generic_errors():
    # Generic tool/model errors must NOT be misclassified as auth.
    assert not session_mod.is_auth_error("Tool execution failed: file not found")
    assert not session_mod.is_auth_error("rate limit exceeded")
    assert not session_mod.is_auth_error("500 Internal Server Error")
    # A 403 with no credential/token/auth keyword (e.g. an S3 object ACL denial)
    # is not treated as a session-credential failure.
    assert not session_mod.is_auth_error("403: you lack permission to write that object")
    assert not session_mod.is_auth_error(None)
    assert not session_mod.is_auth_error("")


async def test_send_tags_auth_error_on_result(monkeypatch):
    """A result error matching auth patterns is tagged errorKind='auth'."""
    s = session_mod.PersistentSession(session_id="sess-auth")
    lines = [
        (json.dumps({"type": "system", "session_id": "sess-auth"}) + "\n").encode(),
        (json.dumps({"type": "result", "is_error": True,
                     "result": "API Error: 403 The security token included in the request is invalid"}) + "\n").encode(),
    ]
    s._proc = _FakeProc(lines)
    s._started = True

    got = [evt async for evt in s.send("hi")]
    result_evt = next(e for e in got if e.get("type") == "result")
    assert result_evt.get("errorKind") == "auth"


async def test_send_does_not_tag_generic_result_error():
    """A non-auth result error is left untagged (generic error path)."""
    s = session_mod.PersistentSession(session_id="sess-gen")
    lines = [
        (json.dumps({"type": "result", "is_error": True, "result": "tool failed: timeout"}) + "\n").encode(),
    ]
    s._proc = _FakeProc(lines)
    s._started = True

    got = [evt async for evt in s.send("hi")]
    result_evt = next(e for e in got if e.get("type") == "result")
    assert "errorKind" not in result_evt


async def test_manager_respawn_replaces_subprocess(monkeypatch):
    """respawn tears down the old session and starts a fresh one, same id."""
    mgr = session_mod.SessionManager()

    # Register a fake live session.
    old = session_mod.PersistentSession(session_id="sess-x", cwd="/tmp")
    old._proc = _FakeProc([])
    old._started = True
    stopped = {"v": False}
    async def fake_stop():
        stopped["v"] = True
        old._proc.returncode = 0
    old.stop = fake_stop
    mgr._sessions["sess-x"] = old

    # Patch start() so respawn doesn't actually exec `claude`.
    started = {"v": False}
    async def fake_start(self):
        self._proc = _FakeProc([])
        self._started = True
        started["v"] = True
    monkeypatch.setattr(session_mod.PersistentSession, "start", fake_start)

    fresh = await mgr.respawn("sess-x")
    assert stopped["v"] is True            # old subprocess torn down
    assert started["v"] is True            # new subprocess started
    assert fresh.session_id == "sess-x"    # same session id (history preserved)
    assert fresh.cwd == "/tmp"             # cwd carried over
    assert mgr._sessions["sess-x"] is fresh


async def test_restart_endpoint_respawns(client, tmp_path):
    """POST /api/chat/restart calls manager.respawn for the session."""
    fake_manager = AsyncMock()
    fake_manager.respawn = AsyncMock(return_value=None)
    with patch.dict(client.app, {"session_manager": fake_manager}):
        resp = await client.post("/api/chat/restart", json={"session_id": "sess-1", "cwd": str(tmp_path)})
    assert resp.status == 200
    assert (await resp.json())["restarted"] == "sess-1"
    fake_manager.respawn.assert_awaited_once()
    # Regression guard: a credential-recovered (respawned) session must keep the
    # app's permission mode, not silently fall back to 'default'. Otherwise the
    # process that comes back after re-auth would lose write ability.
    _, kwargs = fake_manager.respawn.await_args
    assert kwargs["permission_mode"] == "bypassPermissions"


async def test_restart_endpoint_requires_session_id(client):
    resp = await client.post("/api/chat/restart", json={})
    assert resp.status == 400


async def test_start_builds_permission_mode_arg(monkeypatch):
    """PersistentSession.start must pass its permission_mode straight through to
    the claude CLI as `--permission-mode <mode>`. We capture the exec argv via a
    stubbed create_subprocess_exec (reusing _FakeProc) rather than execing claude.
    """
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc([])

    monkeypatch.setattr(session_mod.asyncio, "create_subprocess_exec", fake_exec)

    s = session_mod.PersistentSession(permission_mode="bypassPermissions")
    await s.start()

    argv = list(captured["args"])
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


# --------------------------------------------------------------------------- #
# Memory routes (/api/memory/files)
#
# The memory route reads/writes ~/.claude/projects/<home-slug>/memory. The
# directory used to be a hardcoded personal slug (-local-home-xulaicao), which
# made the page silently empty for any other user. It now derives the slug from
# the resolved home path at call time. These tests point MEMORY_DIR at a
# tmp-backed dir and exercise the CRUD + etag-conflict paths.
# --------------------------------------------------------------------------- #
import server.routes.memory as memory_mod  # noqa: E402


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    monkeypatch.setattr(memory_mod, "MEMORY_DIR", d)
    # Seed an index + one principle file with frontmatter.
    (d / "MEMORY.md").write_text("# Index\n\n- [Foo](foo.md)\n", encoding="utf-8")
    (d / "feedback_principle_x.md").write_text(
        "---\nname: x\ndescription: a test principle\nmetadata:\n  type: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )
    return d


async def test_memory_list_returns_meta(client, memory_dir):
    resp = await client.get("/api/memory/files")
    assert resp.status == 200
    files = {f["name"]: f for f in await resp.json()}
    assert "MEMORY.md" in files and "feedback_principle_x.md" in files
    # Frontmatter description + type are surfaced.
    assert files["feedback_principle_x.md"]["description"] == "a test principle"
    assert files["feedback_principle_x.md"]["type"] == "feedback"


async def test_memory_get_returns_content_and_etag(client, memory_dir):
    resp = await client.get("/api/memory/files/feedback_principle_x.md")
    assert resp.status == 200
    data = await resp.json()
    assert "body" in data["content"]
    assert data["etag"]


async def test_memory_create_and_roundtrip(client, memory_dir):
    resp = await client.post("/api/memory/files", json={"name": "new_note", "content": "hello"})
    assert resp.status == 201
    # .md suffix is added by _safe_name.
    assert (memory_dir / "new_note.md").read_text() == "hello"

    # Duplicate create is a 409.
    dup = await client.post("/api/memory/files", json={"name": "new_note", "content": "x"})
    assert dup.status == 409


async def test_memory_put_etag_conflict(client, memory_dir):
    # Read current etag.
    got = await (await client.get("/api/memory/files/feedback_principle_x.md")).json()
    etag = got["etag"]

    # An external edit changes the file under us (bumps mtime/etag).
    import time as _t
    _t.sleep(0.01)
    (memory_dir / "feedback_principle_x.md").write_text("changed externally\n", encoding="utf-8")

    # Saving with the stale etag must 409, and return the current content.
    resp = await client.put(
        "/api/memory/files/feedback_principle_x.md",
        json={"content": "my edit", "etag": etag},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert "changed externally" in body["current"]


async def test_memory_put_succeeds_with_current_etag(client, memory_dir):
    got = await (await client.get("/api/memory/files/feedback_principle_x.md")).json()
    resp = await client.put(
        "/api/memory/files/feedback_principle_x.md",
        json={"content": "fresh edit", "etag": got["etag"]},
    )
    assert resp.status == 200
    assert (memory_dir / "feedback_principle_x.md").read_text() == "fresh edit"


async def test_memory_delete_protects_index(client, memory_dir):
    # MEMORY.md is the index and must not be deletable.
    resp = await client.delete("/api/memory/files/MEMORY.md")
    assert resp.status == 403
    assert (memory_dir / "MEMORY.md").exists()


async def test_memory_path_traversal_rejected(client, memory_dir):
    resp = await client.get("/api/memory/files/..%2f..%2fsecret")
    assert resp.status in (400, 404)


def test_home_memory_dir_uses_resolved_home_slug(monkeypatch, tmp_path):
    """Regression guard: the memory dir slug is derived from the RESOLVED home
    path, not a hardcoded personal slug. On a machine where $HOME differs, the
    computed dir must follow $HOME rather than '-local-home-xulaicao'.
    """
    fake_home = tmp_path / "home" / "alice"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(memory_mod.Path, "home", staticmethod(lambda: fake_home))
    got = memory_mod._home_memory_dir()
    expected_slug = "-" + str(fake_home.resolve()).lstrip("/").replace("/", "-")
    assert got == fake_home / ".claude" / "projects" / expected_slug / "memory"
    assert "local-home-xulaicao" not in str(got)


# --------------------------------------------------------------------------- #
# MCP routes (/api/mcp)
#
# MCP config used to be read from a MeshClaw leftover
# (~/.claude/agents/meshclaw.mcp.json). It now prefers the standard
# ~/.claude.json (top-level mcpServers) and only falls back to the legacy file
# when ~/.claude.json is absent. Writes must preserve all the OTHER keys in
# ~/.claude.json (it holds far more than mcpServers).
# --------------------------------------------------------------------------- #
import server.routes.mcp as mcp_mod  # noqa: E402


@pytest.fixture
def mcp_files(tmp_path: Path, monkeypatch):
    standard = tmp_path / ".claude.json"
    legacy = tmp_path / "meshclaw.mcp.json"
    monkeypatch.setattr(mcp_mod, "GLOBAL_STATE_PATH", standard)
    monkeypatch.setattr(mcp_mod, "LEGACY_AGENT_MCP_PATH", legacy)
    return {"standard": standard, "legacy": legacy}


async def test_mcp_prefers_standard_over_legacy(client, mcp_files):
    # Both files exist; the standard ~/.claude.json must win.
    mcp_files["standard"].write_text(json.dumps({
        "numStartups": 7,
        "mcpServers": {"std-server": {"command": "x", "args": []}},
    }), encoding="utf-8")
    mcp_files["legacy"].write_text(json.dumps({
        "mcpServers": {"legacy-server": {"command": "y", "args": []}},
    }), encoding="utf-8")

    resp = await client.get("/api/mcp")
    assert resp.status == 200
    names = {s["name"] for s in (await resp.json())["servers"]}
    assert names == {"std-server"}


async def test_mcp_falls_back_to_legacy_when_standard_absent(client, mcp_files):
    # Only the legacy file exists -> it is used.
    mcp_files["legacy"].write_text(json.dumps({
        "mcpServers": {"legacy-server": {"command": "y", "args": []}},
    }), encoding="utf-8")

    resp = await client.get("/api/mcp")
    names = {s["name"] for s in (await resp.json())["servers"]}
    assert names == {"legacy-server"}


async def test_mcp_masks_secret_env_values(client, mcp_files):
    mcp_files["standard"].write_text(json.dumps({
        "mcpServers": {"s": {"command": "x", "env": {"API_KEY": "supersecret", "REGION": "us-east-1"}}},
    }), encoding="utf-8")
    resp = await client.get("/api/mcp")
    server = (await resp.json())["servers"][0]
    assert server["env"]["API_KEY"] == "***"
    assert server["env"]["REGION"] == "us-east-1"


async def test_mcp_add_preserves_other_keys(client, mcp_files):
    # ~/.claude.json holds far more than mcpServers; adding a server must not
    # clobber the rest of the file.
    mcp_files["standard"].write_text(json.dumps({
        "numStartups": 42,
        "tipsHistory": {"a": 1},
        "mcpServers": {"existing": {"command": "x"}},
    }), encoding="utf-8")

    resp = await client.post("/api/mcp", json={"name": "newsrv", "config": {"command": "z"}})
    assert resp.status == 201

    on_disk = json.loads(mcp_files["standard"].read_text())
    assert on_disk["numStartups"] == 42          # untouched
    assert on_disk["tipsHistory"] == {"a": 1}    # untouched
    assert set(on_disk["mcpServers"]) == {"existing", "newsrv"}


async def test_mcp_add_duplicate_is_conflict(client, mcp_files):
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {"dup": {}}}), encoding="utf-8")
    resp = await client.post("/api/mcp", json={"name": "dup", "config": {}})
    assert resp.status == 409


async def test_mcp_update_and_delete(client, mcp_files):
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {"s": {"command": "old"}}}), encoding="utf-8")

    upd = await client.put("/api/mcp/s", json={"config": {"command": "new"}})
    assert upd.status == 200
    assert json.loads(mcp_files["standard"].read_text())["mcpServers"]["s"]["command"] == "new"

    dele = await client.delete("/api/mcp/s", json={})
    assert dele.status == 200
    assert json.loads(mcp_files["standard"].read_text())["mcpServers"] == {}


async def test_mcp_update_unknown_is_404(client, mcp_files):
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    resp = await client.put("/api/mcp/ghost", json={"config": {}})
    assert resp.status == 404


async def test_mcp_update_does_not_clobber_masked_secret(client, mcp_files):
    """Editing a server must not overwrite a real secret env value with the
    mask. GET returns API_KEY as '***'; if the edit form round-trips that, the
    backend must restore the real value rather than persisting '***'.
    """
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {"s": {
        "command": "run",
        "env": {"API_KEY": "real-secret-value", "REGION": "us-east-1"},
    }}}), encoding="utf-8")

    # Simulate the edit form: it received '***' for API_KEY and sends it back,
    # while genuinely changing the command and a non-secret value.
    resp = await client.put("/api/mcp/s", json={"config": {
        "command": "run-v2",
        "env": {"API_KEY": "***", "REGION": "eu-west-1"},
    }})
    assert resp.status == 200

    on_disk = json.loads(mcp_files["standard"].read_text())["mcpServers"]["s"]
    # The real secret is preserved...
    assert on_disk["env"]["API_KEY"] == "real-secret-value"
    # ...while the genuine edits land.
    assert on_disk["command"] == "run-v2"
    assert on_disk["env"]["REGION"] == "eu-west-1"


async def test_mcp_update_persists_new_secret_value(client, mcp_files):
    """A genuinely new secret value (not the mask) must be written through."""
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {"s": {
        "command": "run", "env": {"API_KEY": "old-secret"},
    }}}), encoding="utf-8")

    resp = await client.put("/api/mcp/s", json={"config": {
        "command": "run", "env": {"API_KEY": "brand-new-secret"},
    }})
    assert resp.status == 200
    on_disk = json.loads(mcp_files["standard"].read_text())["mcpServers"]["s"]
    assert on_disk["env"]["API_KEY"] == "brand-new-secret"


# --------------------------------------------------------------------------- #
# Skills routes (/api/skills)
# --------------------------------------------------------------------------- #
import server.routes.skills as skills_mod  # noqa: E402


@pytest.fixture
def skills_dirs(tmp_path: Path, monkeypatch):
    skills = tmp_path / "skills"
    commands = tmp_path / "commands"
    skills.mkdir()
    commands.mkdir()
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", skills)
    monkeypatch.setattr(skills_mod, "COMMANDS_DIR", commands)
    # One skill dir with SKILL.md, one command file.
    (skills / "deploy").mkdir()
    (skills / "deploy" / "SKILL.md").write_text(
        "---\ndescription: deploy things\ntags: ops\n---\n\nSteps\n", encoding="utf-8"
    )
    (commands / "greet.md").write_text("---\ndescription: say hi\n---\n\nHi\n", encoding="utf-8")
    return {"skills": skills, "commands": commands}


async def test_skills_list_includes_both_sources(client, skills_dirs):
    resp = await client.get("/api/skills")
    assert resp.status == 200
    by_name = {s["name"]: s for s in await resp.json()}
    assert by_name["deploy"]["source"] == "local"
    assert by_name["deploy"]["description"] == "deploy things"
    assert by_name["greet"]["source"] == "command"


async def test_skills_create_get_delete(client, skills_dirs):
    create = await client.post("/api/skills", json={"name": "newskill", "content": "body"})
    assert create.status == 201
    assert (skills_dirs["skills"] / "newskill" / "SKILL.md").read_text() == "body"

    got = await client.get("/api/skills/newskill")
    assert got.status == 200
    assert (await got.json())["content"] == "body"

    dele = await client.delete("/api/skills/newskill")
    assert dele.status == 200
    assert not (skills_dirs["skills"] / "newskill").exists()


async def test_skills_create_rejects_bad_name(client, skills_dirs):
    resp = await client.post("/api/skills", json={"name": "../evil", "content": "x"})
    assert resp.status == 400


async def test_skills_put_etag_conflict(client, skills_dirs):
    got = await (await client.get("/api/skills/deploy")).json()
    import time as _t
    _t.sleep(0.01)
    (skills_dirs["skills"] / "deploy" / "SKILL.md").write_text("external\n", encoding="utf-8")
    resp = await client.put("/api/skills/deploy", json={"content": "mine", "etag": got["etag"]})
    assert resp.status == 409


# --------------------------------------------------------------------------- #
# Settings routes (/api/settings)
# --------------------------------------------------------------------------- #
import server.routes.settings as settings_mod  # noqa: E402


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", p)
    return p


async def test_settings_get_and_put(client, settings_file):
    got = await (await client.get("/api/settings")).json()
    assert got["data"]["theme"] == "dark"
    assert got["etag"]

    resp = await client.put("/api/settings", json={"data": {"theme": "light"}, "etag": got["etag"]})
    assert resp.status == 200
    assert json.loads(settings_file.read_text())["theme"] == "light"


async def test_settings_put_requires_data(client, settings_file):
    resp = await client.put("/api/settings", json={"etag": "x"})
    assert resp.status == 400


async def test_settings_put_etag_conflict(client, settings_file):
    got = await (await client.get("/api/settings")).json()
    import time as _t
    _t.sleep(0.01)
    settings_file.write_text(json.dumps({"theme": "externally-set"}), encoding="utf-8")
    resp = await client.put("/api/settings", json={"data": {"theme": "mine"}, "etag": got["etag"]})
    assert resp.status == 409
    body = await resp.json()
    assert body["current"]["theme"] == "externally-set"


# --------------------------------------------------------------------------- #
# Crons routes (/api/crons)
# --------------------------------------------------------------------------- #
import server.routes.crons as crons_mod  # noqa: E402


@pytest.fixture
def crons_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "scheduled_tasks.json"
    monkeypatch.setattr(crons_mod, "TASKS_PATH", p)
    return p


async def test_crons_empty_when_no_file(client, crons_file):
    resp = await client.get("/api/crons")
    assert resp.status == 200
    assert (await resp.json())["jobs"] == []


async def test_crons_create_list_update_delete(client, crons_file):
    create = await client.post("/api/crons", json={"cron": "0 9 * * *", "prompt": "standup"})
    assert create.status == 201
    job = (await create.json())["job"]
    assert job["cron"] == "0 9 * * *" and job["prompt"] == "standup"
    job_id = job["id"]

    listed = (await (await client.get("/api/crons")).json())["jobs"]
    assert any(j["id"] == job_id for j in listed)

    upd = await client.put(f"/api/crons/{job_id}", json={"prompt": "daily standup"})
    assert upd.status == 200
    assert (await upd.json())["job"]["prompt"] == "daily standup"

    dele = await client.delete(f"/api/crons/{job_id}", json={})
    assert dele.status == 200
    assert (await (await client.get("/api/crons")).json())["jobs"] == []


async def test_crons_create_requires_fields(client, crons_file):
    resp = await client.post("/api/crons", json={"cron": "", "prompt": ""})
    assert resp.status == 400


async def test_crons_update_unknown_is_404(client, crons_file):
    resp = await client.put("/api/crons/nope", json={"prompt": "x"})
    assert resp.status == 404


def test_resolve_tasks_path_prefers_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom" / "scheduled_tasks.json"
    monkeypatch.setenv("CLAUDE_WEB_TASKS_PATH", str(override))
    assert crons_mod._resolve_tasks_path() == override


def test_resolve_tasks_path_defaults_to_cwd_relative(tmp_path, monkeypatch):
    # No override → cwd-relative .claude/scheduled_tasks.json (where the harness
    # scheduler writes durable tasks). This is the alignment the fix guarantees.
    monkeypatch.delenv("CLAUDE_WEB_TASKS_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert crons_mod._resolve_tasks_path() == tmp_path / ".claude" / "scheduled_tasks.json"


async def test_crons_reads_harness_written_file(client, crons_file):
    """A task written by the harness scheduler (richer schema, extra fields) must
    surface verbatim through GET /api/crons — the GUI is a view, not a parallel list.
    """
    crons_file.parent.mkdir(parents=True, exist_ok=True)
    crons_file.write_text(json.dumps({"tasks": [{
        "id": "abc12345",
        "cron": "37 23 * * *",
        "prompt": "daily health check",
        "recurring": True,
        "createdAt": 1781426694506,
        # Fields the harness writes that the GUI doesn't author:
        "createdBySessionId": "sess-1",
        "createdByPid": 184139,
    }]}), encoding="utf-8")

    jobs = (await (await client.get("/api/crons")).json())["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == "abc12345"
    # Harness-only fields are preserved, not dropped.
    assert jobs[0]["createdBySessionId"] == "sess-1"


async def test_crons_update_preserves_harness_fields(client, crons_file):
    """Editing a harness-created job from the GUI must not strip the scheduler's
    own fields (they live in the same dict that gets rewritten)."""
    crons_file.parent.mkdir(parents=True, exist_ok=True)
    crons_file.write_text(json.dumps({"tasks": [{
        "id": "abc12345", "cron": "37 23 * * *", "prompt": "old",
        "recurring": True, "createdAt": 1, "createdBySessionId": "sess-1",
    }]}), encoding="utf-8")

    upd = await client.put("/api/crons/abc12345", json={"prompt": "new"})
    assert upd.status == 200
    saved = json.loads(crons_file.read_text(encoding="utf-8"))["tasks"][0]
    assert saved["prompt"] == "new"
    assert saved["createdBySessionId"] == "sess-1"  # preserved


# --------------------------------------------------------------------------- #
# Cron run-history store (/api/crons/{id}/runs)
#
# Run history lives in a *separate sidecar* file (cron_runs.json) so the harness's
# scheduled_tasks.json is never polluted with GUI-authored fields. The store only
# ever displays runs that were actually recorded — an empty store yields an honest
# empty list, never fabricated history.
# --------------------------------------------------------------------------- #
@pytest.fixture
def cron_runs_file(tmp_path: Path, monkeypatch):
    """Point the run-history sidecar at a tmp file (mirrors crons_file for TASKS_PATH)."""
    p = tmp_path / "cron_runs.json"
    monkeypatch.setattr(crons_mod, "RUNS_PATH", p)
    return p


async def test_cron_runs_empty_returns_empty_list(client, cron_runs_file):
    """No runs file → honest empty state, not fabricated history."""
    resp = await client.get("/api/crons/abc12345/runs")
    assert resp.status == 200
    assert (await resp.json())["runs"] == []
    # Honest empty state must not have created the sidecar as a side effect of a read.
    assert not cron_runs_file.exists()


async def test_cron_run_append_then_list(client, cron_runs_file):
    """POST a run → 201 with id+ts; GET returns it; multiple runs are newest-first."""
    first = await client.post(
        "/api/crons/abc12345/runs", json={"outcome": "success", "result": "did the thing"}
    )
    assert first.status == 201
    run = (await first.json())["run"]
    assert run["id"]  # carries a generated id
    assert run["ts"]  # carries a timestamp
    assert run["outcome"] == "success"
    assert run["result"] == "did the thing"

    # A small gap so the second run sorts strictly after the first.
    import time as _t
    _t.sleep(0.01)
    second = await client.post(
        "/api/crons/abc12345/runs", json={"outcome": "failure", "result": "broke"}
    )
    assert second.status == 201

    runs = (await (await client.get("/api/crons/abc12345/runs")).json())["runs"]
    assert len(runs) == 2
    # Newest-first ordering: the failure (posted second) leads.
    assert runs[0]["outcome"] == "failure"
    assert runs[1]["outcome"] == "success"
    assert runs[0]["ts"] >= runs[1]["ts"]


async def test_cron_run_append_defaults_outcome_unknown(client, cron_runs_file):
    """POST with no outcome defaults to an allowlisted value (unknown)."""
    resp = await client.post("/api/crons/abc12345/runs", json={"result": "no outcome given"})
    assert resp.status == 201
    run = (await resp.json())["run"]
    assert run["outcome"] in {"success", "failure", "unknown"}
    assert run["outcome"] == "unknown"


async def test_cron_runs_scoped_per_job(client, cron_runs_file):
    """Runs are keyed by job id — one job's history never leaks into another's."""
    await client.post("/api/crons/job-a/runs", json={"outcome": "success", "result": "a"})
    await client.post("/api/crons/job-b/runs", json={"outcome": "failure", "result": "b"})

    runs_a = (await (await client.get("/api/crons/job-a/runs")).json())["runs"]
    runs_b = (await (await client.get("/api/crons/job-b/runs")).json())["runs"]
    assert [r["result"] for r in runs_a] == ["a"]
    assert [r["result"] for r in runs_b] == ["b"]


async def test_cron_runs_do_not_touch_scheduled_tasks(client, crons_file, cron_runs_file):
    """Appending a run must not pollute the harness's scheduled_tasks.json.

    The run store is a separate sidecar; recording a run must never write a `runs`
    key onto the task, set lastFiredAt, or otherwise mutate the harness file.
    """
    crons_file.parent.mkdir(parents=True, exist_ok=True)
    crons_file.write_text(json.dumps({"tasks": [{
        "id": "abc12345", "cron": "37 23 * * *", "prompt": "health check",
        "recurring": True, "createdAt": 1, "lastFiredAt": None,
    }]}), encoding="utf-8")
    before = crons_file.read_text(encoding="utf-8")

    resp = await client.post(
        "/api/crons/abc12345/runs", json={"outcome": "success", "result": "ran"}
    )
    assert resp.status == 201

    # The harness file is byte-for-byte unchanged — no runs key, no lastFiredAt edit.
    after = crons_file.read_text(encoding="utf-8")
    assert after == before
    task = json.loads(after)["tasks"][0]
    assert "runs" not in task
    assert task["lastFiredAt"] is None


async def test_cron_run_job_id_is_a_key_not_a_filename(client, cron_runs_file):
    """The job id is a JSON dict key in ONE fixed sidecar, never a path component.

    Because the id never touches the filesystem, a traversal-shaped id can't escape:
    it just becomes a key. We confirm the id is stored verbatim as a key and the only
    file written is the monkeypatched sidecar (nothing leaked to a sibling/parent).
    """
    weird_id = "..weird..id.."  # dots, but no slashes (slashes break URL routing)
    resp = await client.post(
        f"/api/crons/{weird_id}/runs", json={"outcome": "success", "result": "x"}
    )
    assert resp.status == 201
    stored = json.loads(cron_runs_file.read_text(encoding="utf-8"))
    assert weird_id in stored["runs"]  # stored as a key, not interpreted as a path
    assert list(cron_runs_file.parent.iterdir()) == [cron_runs_file]


def test_resolve_runs_path_prefers_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom" / "cron_runs.json"
    monkeypatch.setenv("CLAUDE_WEB_CRON_RUNS_PATH", str(override))
    assert crons_mod._resolve_runs_path() == override


def test_resolve_runs_path_defaults_beside_tasks(tmp_path, monkeypatch):
    # No override → the sidecar sits beside the harness scheduled_tasks.json, in the
    # same .claude dir. Derived from TASKS_PATH so the two files always stay aligned.
    monkeypatch.delenv("CLAUDE_WEB_CRON_RUNS_PATH", raising=False)
    monkeypatch.setattr(crons_mod, "TASKS_PATH", tmp_path / ".claude" / "scheduled_tasks.json")
    resolved = crons_mod._resolve_runs_path()
    assert resolved == tmp_path / ".claude" / "cron_runs.json"
    assert resolved == crons_mod.TASKS_PATH.parent / "cron_runs.json"


# --------------------------------------------------------------------------- #
# Tasks routes (/api/tasks)
#
# Read-only view of ~/.claude/tasks/<sessionId>/<taskId>.json, enriched with a
# project name (mapped via the session's transcript under ~/.claude/projects)
# and the file mtime. Dismissal is claude-web view-state in ~/.claude-web; the
# source task files are never modified.
# --------------------------------------------------------------------------- #
import time as _time  # noqa: E402

import server.routes.tasks as tasks_mod  # noqa: E402
import server.routes.sessions as sessions_mod  # noqa: E402


@pytest.fixture
def tasks_layout(tmp_path: Path, monkeypatch) -> Path:
    """Build a fake ~/.claude tree: a task dir + a matching project transcript.

    Two sessions:
      - sess-proj  → has a transcript under a real project slug (→ "myproj")
      - sess-orphan → no transcript anywhere (→ "(unknown)")
    """
    claude = tmp_path / ".claude"
    tasks_dir = claude / "tasks"
    projects = claude / "projects"
    tasks_dir.mkdir(parents=True)
    projects.mkdir(parents=True)

    # A real project dir on disk so _project_label can reconstruct the path.
    real_proj = tmp_path / "workspace" / "projects" / "myproj"
    real_proj.mkdir(parents=True)
    slug = "-" + str(real_proj).lstrip("/").replace("/", "-")
    proj_transcript_dir = projects / slug
    proj_transcript_dir.mkdir()
    (proj_transcript_dir / "sess-proj.jsonl").write_text('{"type":"user"}\n')

    # Task files for the attributed session.
    sp = tasks_dir / "sess-proj"
    sp.mkdir()
    _write_json(sp / "1.json", {"id": "1", "subject": "Build feature", "status": "in_progress"})
    _write_json(sp / "2.json", {"id": "2", "subject": "Old done task", "status": "completed"})

    # Orphan session: tasks exist but no transcript maps it to a project.
    so = tasks_dir / "sess-orphan"
    so.mkdir()
    _write_json(so / "1.json", {"id": "1", "subject": "Mystery", "status": "pending"})

    dismissed = tmp_path / ".claude-web" / "dismissed_tasks.json"
    user_tasks = tmp_path / ".claude-web" / "tasks.json"

    monkeypatch.setattr(tasks_mod, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(tasks_mod, "CLAUDE_PROJECTS_BASE", projects)
    monkeypatch.setattr(tasks_mod, "DISMISSED_PATH", dismissed)
    monkeypatch.setattr(tasks_mod, "USER_TASKS_PATH", user_tasks)
    # _known_projects() reads WORKSPACE_DIR (imported into tasks at load time);
    # point it at the fixture's workspace/projects so "myproj" is a valid target.
    monkeypatch.setattr(tasks_mod, "WORKSPACE_DIR", real_proj.parent)
    # _project_label scans the real filesystem from "/"; point its base at our
    # fake projects dir and clear its process-wide cache between tests.
    monkeypatch.setattr(sessions_mod, "_project_label_cache", {})
    # Reset the sessionId→project map cache so each test rebuilds it.
    monkeypatch.setattr(tasks_mod, "_map_cache", {})
    monkeypatch.setattr(tasks_mod, "_map_updated_at", 0.0)
    return tasks_dir


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


async def test_tasks_empty_when_no_dir(client, tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_mod, "TASKS_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(tasks_mod, "CLAUDE_PROJECTS_BASE", tmp_path / "noprojects")
    monkeypatch.setattr(tasks_mod, "_map_cache", {})
    monkeypatch.setattr(tasks_mod, "_map_updated_at", 0.0)
    resp = await client.get("/api/tasks")
    assert resp.status == 200
    data = await resp.json()
    assert data["tasks"] == []
    assert data["projects"] == []


async def test_tasks_project_attribution(client, tasks_layout):
    data = await (await client.get("/api/tasks")).json()
    by_subject = {t["subject"]: t for t in data["tasks"]}
    # Session with a transcript maps to its project name (dir basename).
    assert by_subject["Build feature"]["project"] == "myproj"
    # Orphan session (no transcript) falls back to a clear sentinel.
    assert by_subject["Mystery"]["project"] == "(unknown)"
    # Distinct projects surfaced for the filter dropdown.
    assert "myproj" in data["projects"]


async def test_tasks_project_filter(client, tasks_layout):
    data = await (await client.get("/api/tasks?project=myproj")).json()
    assert {t["subject"] for t in data["tasks"]} == {"Build feature", "Old done task"}
    assert all(t["project"] == "myproj" for t in data["tasks"])


async def test_tasks_time_filter_excludes_old(client, tasks_layout):
    # Backdate the orphan task file well beyond a 1-day window.
    old = tasks_layout / "sess-orphan" / "1.json"
    old_ts = _time.time() - 10 * 86400
    os.utime(old, (old_ts, old_ts))
    data = await (await client.get("/api/tasks?sinceDays=1")).json()
    subjects = {t["subject"] for t in data["tasks"]}
    assert "Mystery" not in subjects          # too old
    assert "Build feature" in subjects        # recent


async def test_tasks_dismiss_hides_and_persists(client, tasks_layout):
    # Dismiss the completed task.
    resp = await client.post("/api/tasks/dismiss", json={"sessionId": "sess-proj", "taskId": "2"})
    assert resp.status == 200

    # Default list excludes it; the count reflects it.
    data = await (await client.get("/api/tasks")).json()
    assert "Old done task" not in {t["subject"] for t in data["tasks"]}
    assert data["dismissedCount"] == 1

    # includeDismissed=1 brings it back, flagged dismissed.
    data = await (await client.get("/api/tasks?includeDismissed=1")).json()
    old = next(t for t in data["tasks"] if t["subject"] == "Old done task")
    assert old["dismissed"] is True

    # Undismiss restores it to the default view.
    resp = await client.post("/api/tasks/undismiss", json={"sessionId": "sess-proj", "taskId": "2"})
    assert resp.status == 200
    data = await (await client.get("/api/tasks")).json()
    assert "Old done task" in {t["subject"] for t in data["tasks"]}


async def test_tasks_dismiss_does_not_touch_source_file(client, tasks_layout):
    """Dismissal is view-state only — the ~/.claude/tasks JSON is untouched."""
    src = tasks_layout / "sess-proj" / "2.json"
    before = src.read_text()
    await client.post("/api/tasks/dismiss", json={"sessionId": "sess-proj", "taskId": "2"})
    assert src.read_text() == before  # unchanged on disk


async def test_tasks_dismiss_requires_fields(client, tasks_layout):
    resp = await client.post("/api/tasks/dismiss", json={"sessionId": "sess-proj"})
    assert resp.status == 400


async def test_tasks_complete_writes_source_file(client, tasks_layout):
    """Mark-complete writes status:'completed' to the source task JSON."""
    src = tasks_layout / "sess-proj" / "1.json"
    assert json.loads(src.read_text())["status"] == "in_progress"

    resp = await client.post("/api/tasks/complete", json={"sessionId": "sess-proj", "taskId": "1"})
    assert resp.status == 200

    # The on-disk file now reflects the new status, other fields preserved.
    written = json.loads(src.read_text())
    assert written["status"] == "completed"
    assert written["subject"] == "Build feature"


async def test_tasks_complete_unknown_is_404(client, tasks_layout):
    resp = await client.post("/api/tasks/complete", json={"sessionId": "sess-proj", "taskId": "999"})
    assert resp.status == 404


async def test_tasks_delete_removes_only_target_file(client, tasks_layout):
    """Delete removes exactly the named task file — no sibling, no directory."""
    target = tasks_layout / "sess-proj" / "2.json"
    sibling = tasks_layout / "sess-proj" / "1.json"
    assert target.is_file() and sibling.is_file()

    resp = await client.post("/api/tasks/delete", json={"sessionId": "sess-proj", "taskId": "2"})
    assert resp.status == 200

    assert not target.exists()          # gone
    assert sibling.is_file()            # untouched
    assert target.parent.is_dir()       # session dir preserved


async def test_tasks_delete_unknown_is_404(client, tasks_layout):
    resp = await client.post("/api/tasks/delete", json={"sessionId": "sess-proj", "taskId": "999"})
    assert resp.status == 404


async def test_tasks_complete_rejects_traversal(client, tasks_layout):
    """Path-traversal in ids is rejected before touching the filesystem."""
    resp = await client.post("/api/tasks/complete", json={"sessionId": "../../etc", "taskId": "1"})
    assert resp.status == 400
    resp = await client.post("/api/tasks/delete", json={"sessionId": "sess-proj", "taskId": "../1"})
    assert resp.status == 400


async def test_tasks_carry_project_path(client, tasks_layout):
    """Tasks expose projectPath so the UI can build a session slug for trigger-goal."""
    data = await (await client.get("/api/tasks")).json()
    bf = next(t for t in data["tasks"] if t["subject"] == "Build feature")
    assert bf["projectPath"] and bf["projectPath"].endswith("/myproj")


# --- Console-created (user) tasks ----------------------------------------- #

async def test_user_task_create_and_appears_in_list(client, tasks_layout):
    """Creating a user task stores it in ~/.claude-web and merges into /api/tasks."""
    resp = await client.post("/api/tasks/user", json={
        "subject": "My idea", "description": "flesh out later", "project": "myproj",
    })
    assert resp.status == 201
    created = (await resp.json())["task"]
    assert created["source"] == "user"
    assert created["project"] == "myproj"
    assert created["projectPath"].endswith("/myproj")
    assert created["id"].startswith("user:")
    assert created["status"] == "pending"

    # It shows up in the list, tagged source=user, and the Claude tasks remain.
    data = await (await client.get("/api/tasks")).json()
    subjects = {t["subject"] for t in data["tasks"]}
    assert "My idea" in subjects
    assert "Build feature" in subjects  # Claude tasks unaffected
    mine = next(t for t in data["tasks"] if t["subject"] == "My idea")
    assert mine["source"] == "user" and mine["_sessionId"] == tasks_mod.USER_SESSION_ID


async def test_user_task_not_written_to_claude_tasks(client, tasks_layout):
    """User tasks live in ~/.claude-web/tasks.json, never in ~/.claude/tasks."""
    await client.post("/api/tasks/user", json={"subject": "Idea", "project": "myproj"})
    # The store file exists; the Claude tasks dir gained no new session dir.
    assert tasks_mod.USER_TASKS_PATH.is_file()
    session_dirs = {p.name for p in tasks_layout.iterdir() if p.is_dir()}
    assert session_dirs == {"sess-proj", "sess-orphan"}  # unchanged


async def test_user_task_create_requires_subject_and_valid_project(client, tasks_layout):
    # Missing subject → 400.
    assert (await client.post("/api/tasks/user", json={"project": "myproj"})).status == 400
    # Unknown project → 400 (also rejects traversal-y names).
    assert (await client.post("/api/tasks/user", json={"subject": "x", "project": "../etc"})).status == 400


async def test_user_task_global_no_project(client, tasks_layout):
    """A task created with no project is global, labeled (global), path null."""
    # Omitted project.
    resp = await client.post("/api/tasks/user", json={"subject": "Loose idea"})
    assert resp.status == 201
    t = (await resp.json())["task"]
    assert t["project"] == "(global)" and t["projectPath"] is None

    # Explicit empty string also means global.
    resp = await client.post("/api/tasks/user", json={"subject": "Another", "project": ""})
    assert resp.status == 201
    assert (await resp.json())["task"]["project"] == "(global)"

    # Both appear in the list.
    data = await (await client.get("/api/tasks")).json()
    globals_ = [t for t in data["tasks"] if t.get("source") == "user" and t["project"] == "(global)"]
    assert len(globals_) == 2


async def test_user_task_complete_and_delete(client, tasks_layout):
    created = (await (await client.post("/api/tasks/user", json={"subject": "Idea", "project": "myproj"})).json())["task"]
    tid = created["id"]

    # Complete updates the store entry's status.
    resp = await client.post("/api/tasks/complete", json={"sessionId": tasks_mod.USER_SESSION_ID, "taskId": tid})
    assert resp.status == 200
    assert (await resp.json())["task"]["status"] == "completed"

    # Delete removes it from the store.
    resp = await client.post("/api/tasks/delete", json={"sessionId": tasks_mod.USER_SESSION_ID, "taskId": tid})
    assert resp.status == 200
    data = await (await client.get("/api/tasks?includeDismissed=1")).json()
    assert tid not in {t["id"] for t in data["tasks"]}


async def test_user_task_edit(client, tasks_layout):
    created = (await (await client.post("/api/tasks/user", json={"subject": "Old", "project": "myproj"})).json())["task"]
    tid = created["id"]
    resp = await client.put("/api/tasks/user", json={"id": tid, "subject": "New title", "description": "added"})
    assert resp.status == 200
    t = (await resp.json())["task"]
    assert t["subject"] == "New title" and t["description"] == "added"
    # Empty subject on edit is rejected.
    assert (await client.put("/api/tasks/user", json={"id": tid, "subject": "  "})).status == 400
    # Unknown id → 404.
    assert (await client.put("/api/tasks/user", json={"id": "user:nope", "subject": "x"})).status == 404


# --------------------------------------------------------------------------- #
# CLI bind safety
#
# The server has no auth and can run shell commands (POST /api/hooks/test), so
# binding to a non-loopback host without an explicit opt-in is unauthenticated
# RCE. cmd_start must refuse a routable host unless --allow-remote is given.
# --------------------------------------------------------------------------- #
import server.cli as cli_mod  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def test_is_loopback_classification():
    assert cli_mod._is_loopback("127.0.0.1")
    assert cli_mod._is_loopback("localhost")
    assert cli_mod._is_loopback("::1")
    assert cli_mod._is_loopback("LOCALHOST")  # case-insensitive
    assert not cli_mod._is_loopback("0.0.0.0")
    assert not cli_mod._is_loopback("192.168.1.5")


def test_cmd_start_refuses_non_loopback_without_flag(monkeypatch):
    monkeypatch.delenv("CLAUDE_WEB_ALLOW_REMOTE", raising=False)
    args = SimpleNamespace(host="0.0.0.0", port=7780, no_browser=True, allow_remote=False)
    # Must exit(2) BEFORE importing/creating the app or calling run_app.
    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_start(args)
    assert exc.value.code == 2


def test_cmd_start_allows_non_loopback_with_flag(monkeypatch):
    monkeypatch.delenv("CLAUDE_WEB_ALLOW_REMOTE", raising=False)
    started = {}
    # Stub run_app so the test doesn't actually block on a server.
    monkeypatch.setattr(cli_mod.web, "run_app", lambda app, **kw: started.update(kw))
    args = SimpleNamespace(host="0.0.0.0", port=7780, no_browser=True, allow_remote=True)
    cli_mod.cmd_start(args)
    assert started.get("host") == "0.0.0.0"


def test_cmd_start_allows_non_loopback_via_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_WEB_ALLOW_REMOTE", "1")
    started = {}
    monkeypatch.setattr(cli_mod.web, "run_app", lambda app, **kw: started.update(kw))
    args = SimpleNamespace(host="0.0.0.0", port=7780, no_browser=True, allow_remote=False)
    cli_mod.cmd_start(args)
    assert started.get("host") == "0.0.0.0"


def test_cmd_start_loopback_does_not_require_flag(monkeypatch):
    monkeypatch.delenv("CLAUDE_WEB_ALLOW_REMOTE", raising=False)
    started = {}
    monkeypatch.setattr(cli_mod.web, "run_app", lambda app, **kw: started.update(kw))
    # no_browser=True so webbrowser.open isn't invoked during the test.
    args = SimpleNamespace(host="127.0.0.1", port=7780, no_browser=True, allow_remote=False)
    cli_mod.cmd_start(args)
    assert started.get("host") == "127.0.0.1"


# --------------------------------------------------------------------------- #
# Permission-mode resolution (resolve_permission_mode)
#
# The web UI runs claude unattended, so it spawns with a permission mode rather
# than prompting. The mode is resolved from (in precedence order):
#   1. CLAUDE_WEB_PERMISSION_MODE env override
#   2. 'permissionMode' in ~/.claude-web/config.json (CLAUDE_WEB_CONFIG points
#      at it; honored at call time so tests can swap configs)
#   3. the safe default, 'bypassPermissions'
# Any value not in the CLI's accepted token set falls back to the default
# (we must never hand the claude CLI an invalid --permission-mode token).
# --------------------------------------------------------------------------- #

# The valid --permission-mode tokens accepted by the installed claude CLI.
_VALID_PERMISSION_MODES = (
    "acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan",
)


def _write_config(tmp_path, monkeypatch, payload):
    """Write a config.json and point cli at it.

    cli.CONFIG_PATH is captured from CLAUDE_WEB_CONFIG at import time, so setting
    the env var alone wouldn't be seen by the already-imported module. We set
    both: the env var (documents the supported override) and the module attr that
    load_config actually reads.
    """
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_WEB_CONFIG", str(config_path))
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", config_path)


def test_resolve_permission_mode_env_override_wins(monkeypatch, tmp_path):
    """The env override beats config.json."""
    _write_config(tmp_path, monkeypatch, {"permissionMode": "plan"})
    monkeypatch.setenv("CLAUDE_WEB_PERMISSION_MODE", "acceptEdits")
    assert cli_mod.resolve_permission_mode() == "acceptEdits"


def test_resolve_permission_mode_uses_config_when_no_env(monkeypatch, tmp_path):
    """With no env override, the config.json 'permissionMode' is used."""
    monkeypatch.delenv("CLAUDE_WEB_PERMISSION_MODE", raising=False)
    _write_config(tmp_path, monkeypatch, {"permissionMode": "plan"})
    assert cli_mod.resolve_permission_mode() == "plan"


def test_resolve_permission_mode_missing_config_falls_back(monkeypatch, tmp_path):
    """No env and no config file -> safe default 'bypassPermissions'."""
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.delenv("CLAUDE_WEB_PERMISSION_MODE", raising=False)
    monkeypatch.setenv("CLAUDE_WEB_CONFIG", str(missing))
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", missing)
    assert cli_mod.resolve_permission_mode() == "bypassPermissions"


def test_resolve_permission_mode_missing_key_falls_back(monkeypatch, tmp_path):
    """A config that exists but has no 'permissionMode' key -> default."""
    monkeypatch.delenv("CLAUDE_WEB_PERMISSION_MODE", raising=False)
    _write_config(tmp_path, monkeypatch, {"projectUrls": {}})
    assert cli_mod.resolve_permission_mode() == "bypassPermissions"


def test_resolve_permission_mode_invalid_config_value_falls_back(monkeypatch, tmp_path):
    """A garbage configured value must fall back to the default, NOT raise and
    NOT be passed through to the CLI (an invalid token would break the spawn)."""
    monkeypatch.delenv("CLAUDE_WEB_PERMISSION_MODE", raising=False)
    _write_config(tmp_path, monkeypatch, {"permissionMode": "yolo-mode"})
    assert cli_mod.resolve_permission_mode() == "bypassPermissions"


def test_resolve_permission_mode_invalid_env_value_falls_back(monkeypatch, tmp_path):
    """A garbage env override also falls back rather than reaching the CLI."""
    _write_config(tmp_path, monkeypatch, {"permissionMode": "plan"})
    monkeypatch.setenv("CLAUDE_WEB_PERMISSION_MODE", "definitely-not-valid")
    assert cli_mod.resolve_permission_mode() == "bypassPermissions"


@pytest.mark.parametrize("mode", _VALID_PERMISSION_MODES)
def test_resolve_permission_mode_accepts_each_valid_value(monkeypatch, tmp_path, mode):
    """Each of the 6 CLI-accepted tokens is passed through unchanged."""
    monkeypatch.delenv("CLAUDE_WEB_PERMISSION_MODE", raising=False)
    _write_config(tmp_path, monkeypatch, {"permissionMode": mode})
    assert cli_mod.resolve_permission_mode() == mode


# --------------------------------------------------------------------------- #
# Usage aggregation (/api/usage)
#
# Sums message.usage across assistant records in every project transcript and
# returns totals + breakdowns by model / project / agent / day with estimated
# cost. Subagent turns are marked isSidechain + attributionAgent.
# --------------------------------------------------------------------------- #
import server.routes.usage as usage_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_usage_cache(monkeypatch):
    """The usage endpoints share a module-level, time-cached response object.
    Reset it before every test so one test's scan can't serve another's
    request (e.g. a populated scan leaking into the empty-projects test)."""
    monkeypatch.setattr(usage_mod, "_cache", usage_mod._Cache())


@pytest.fixture
def usage_projects(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "claude_projects"
    base.mkdir()
    monkeypatch.setattr(usage_mod, "CLAUDE_PROJECTS_BASE", base)

    _uid = [0]

    def assistant(model, inp, out, cr, cw, *, sidechain=False, agent=None,
                  ts="2026-06-01T10:00:00.000Z", cwd="/home/alice/proj", uuid=None):
        _uid[0] += 1
        rec = {
            "type": "assistant",
            "uuid": uuid if uuid is not None else f"u{_uid[0]}",
            "timestamp": ts,
            "isSidechain": sidechain,
            "cwd": cwd,
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read_input_tokens": cr,
                    "cache_creation_input_tokens": cw,
                },
            },
        }
        if agent:
            rec["attributionAgent"] = agent
        return rec

    proj = base / "-home-alice-proj"
    proj.mkdir()
    _write_jsonl(proj / "s1.jsonl", [
        {"type": "user", "message": {"content": "hi"}},  # ignored (no usage)
        assistant("claude-opus-4-8", 1000, 500, 2000, 100, uuid="main-1"),
        {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 9}}},  # skipped
    ])
    # Subagent usage lives in a subagents/ subtree and is deduped globally by uuid.
    subdir = base / "subagents"
    subdir.mkdir()
    _write_jsonl(subdir / "a1.jsonl", [
        assistant("claude-haiku-4-5", 200, 100, 0, 0, sidechain=True, agent="Explore", uuid="sub-1"),
    ])
    # A transcripts/ export that re-writes the main record — must NOT double-count.
    transdir = base / "transcripts"
    transdir.mkdir()
    _write_jsonl(transdir / "t1.jsonl", [
        assistant("claude-opus-4-8", 1000, 500, 2000, 100, uuid="main-1"),  # dup of main-1
    ])
    return base


async def test_usage_totals_and_breakdowns(client, usage_projects):
    resp = await client.get("/api/usage")
    assert resp.status == 200
    data = await resp.json()

    # Total tokens sum the two real assistant records (synthetic skipped).
    t = data["total"]
    assert t["inputTokens"] == 1200
    assert t["outputTokens"] == 600
    assert t["cacheReadTokens"] == 2000
    assert t["messages"] == 2

    # Per-model split.
    assert set(data["byModel"]) == {"claude-opus-4-8", "claude-haiku-4-5"}
    assert data["byModel"]["claude-opus-4-8"]["inputTokens"] == 1000

    # Main vs subagent attribution.
    assert data["byAgent"]["main"]["messages"] == 1
    assert data["byAgent"]["Explore"]["messages"] == 1

    # Project attribution comes from the record's cwd basename.
    assert data["byProject"]["proj"]["messages"] == 2

    # The transcripts/ duplicate of main-1 must NOT be double-counted.
    assert data["byModel"]["claude-opus-4-8"]["messages"] == 1
    assert t["inputTokens"] == 1200  # 1000 (main, counted once) + 200 (subagent)

    # Cost is computed and positive (opus input 1000*$5/1M + output 500*$25/1M
    # + cacheRead 2000*$5*0.1/1M + cacheWrite 100*$5*1.25/1M).
    assert t["cost"] > 0


def test_usage_cost_formula():
    # Opus: input $5/1M, output $25/1M, cache read 0.1x, cache write 1.25x.
    cost = usage_mod._cost_for("claude-opus-4-8", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    expected = 5.0 + 25.0 + 5.0 * 0.1 + 5.0 * 1.25
    assert abs(cost - expected) < 1e-9


def test_usage_unknown_model_falls_back_to_opus_pricing():
    cost = usage_mod._cost_for("some-future-model", 1_000_000, 0, 0, 0)
    assert abs(cost - 5.0) < 1e-9


async def test_usage_empty_when_no_projects(client, tmp_path, monkeypatch):
    monkeypatch.setattr(usage_mod, "CLAUDE_PROJECTS_BASE", tmp_path / "nonexistent")
    resp = await client.get("/api/usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["total"]["messages"] == 0
    assert data["byModel"] == {}


def _independent_usage_totals(base: Path) -> dict:
    """Recompute deduped usage totals from scratch — the oracle the endpoint
    must match. Mirrors the production rules (dedupe by uuid across all files,
    skip <synthetic>, sum the four token fields) but is written independently
    so a bug in one is unlikely to be mirrored in the other.
    """
    seen: set = set()
    tin = tout = tcr = tcw = nmsg = 0
    for f in sorted(base.rglob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "assistant":
                continue
            m = r.get("message")
            if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                continue
            if m.get("model") == "<synthetic>":
                continue
            uid = r.get("uuid")
            if uid is not None:
                if uid in seen:
                    continue
                seen.add(uid)
            u = m["usage"]
            tin += u.get("input_tokens", 0) or 0
            tout += u.get("output_tokens", 0) or 0
            tcr += u.get("cache_read_input_tokens", 0) or 0
            tcw += u.get("cache_creation_input_tokens", 0) or 0
            nmsg += 1
    return {
        "messages": nmsg,
        "inputTokens": tin,
        "outputTokens": tout,
        "cacheReadTokens": tcr,
        "cacheWriteTokens": tcw,
    }


async def test_usage_endpoint_matches_independent_recompute(client, usage_projects):
    """Accuracy guard: the endpoint's totals must equal a from-scratch recompute,
    field by field. If aggregation, dedup, or skipping logic regresses, this fails
    rather than silently rendering wrong numbers.
    """
    oracle = _independent_usage_totals(usage_projects)
    resp = await client.get("/api/usage")
    data = await resp.json()
    t = data["total"]
    for field in ("messages", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
        assert t[field] == oracle[field], f"{field}: endpoint {t[field]} != oracle {oracle[field]}"

    # The per-breakdown message counts must also reconcile to the total (no
    # turn counted in a breakdown but missing from the total, or vice versa).
    assert sum(v["messages"] for v in data["byModel"].values()) == t["messages"]
    assert sum(v["messages"] for v in data["byAgent"].values()) == t["messages"]
    assert sum(v["messages"] for v in data["byProject"].values()) == t["messages"]
    assert sum(v["messages"] for v in data["daily"]) == t["messages"]


# --------------------------------------------------------------------------- #
# Previously-untested registered endpoints (added by the codebase review pass):
# /api/usage/heatmap, /api/usage/tools, /api/plugins, /api/settings/local,
# /api/chat/stop. These pin the contracts of endpoints that had zero coverage.
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone  # noqa: E402

import server.routes.plugins as plugins_mod  # noqa: E402


@pytest.fixture
def recent_usage(tmp_path: Path, monkeypatch) -> Path:
    """A projects tree with one recent assistant turn (inside the heatmap's
    12-week window) plus a tool_use, so heatmap + tools have data to aggregate.
    Reset the shared usage cache so the scan re-runs against this tree."""
    base = tmp_path / "claude_projects"
    proj = base / "-home-alice-proj"
    proj.mkdir(parents=True)
    # "Now" in UTC, ISO with Z — guaranteed within the 12-week cutoff.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _write_jsonl(proj / "s1.jsonl", [
        {
            "type": "assistant", "uuid": "h1", "timestamp": ts, "isSidechain": False,
            "cwd": "/home/alice/proj",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/y"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
    ])
    monkeypatch.setattr(usage_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(usage_mod, "_cache", usage_mod._Cache())
    return base


async def test_usage_heatmap_shape(client, recent_usage):
    resp = await client.get("/api/usage/heatmap")
    assert resp.status == 200
    data = await resp.json()
    # 7 day-of-week rows x 24 hours; days map + per-date hourly breakdown.
    assert len(data["grid"]) == 7 and all(len(row) == 24 for row in data["grid"])
    assert isinstance(data["days"], dict) and isinstance(data["dailyHours"], dict)
    # The one recent turn registers exactly one message somewhere in the grid.
    assert sum(sum(row) for row in data["grid"]) == 1
    assert sum(data["days"].values()) == 1


async def test_usage_tools_leaderboard(client, recent_usage):
    resp = await client.get("/api/usage/tools")
    assert resp.status == 200
    rows = (await resp.json())["tools"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["Read"]["calls"] == 2
    assert by_name["Bash"]["calls"] == 1
    # Sorted by calls descending (Read before Bash).
    assert rows[0]["calls"] >= rows[-1]["calls"]
    assert by_name["Read"]["avgInputSize"] > 0


async def test_plugins_listing(client, tmp_path, monkeypatch):
    plugins_path = tmp_path / "installed_plugins.json"
    settings_path = tmp_path / "settings.json"
    plugins_path.write_text(json.dumps({"plugins": {
        "alpha": [{"scope": "user", "version": "1.0", "installPath": "/a", "installedAt": 1}],
        "beta": [{"scope": "user", "version": "2.0", "installPath": "/b", "installedAt": 2}],
    }}))
    settings_path.write_text(json.dumps({"enabledPlugins": {"beta": False}}))
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", plugins_path)
    monkeypatch.setattr(plugins_mod, "SETTINGS_PATH", settings_path)

    rows = await (await client.get("/api/plugins")).json()
    by_name = {p["name"]: p for p in rows}
    assert by_name["alpha"]["enabled"] is True   # default-enabled when unlisted
    assert by_name["beta"]["enabled"] is False    # explicitly disabled
    assert by_name["alpha"]["version"] == "1.0"


async def test_plugins_tolerates_malformed_file(client, tmp_path, monkeypatch):
    """A malformed installed_plugins.json must not 500 the endpoint."""
    bad = tmp_path / "installed_plugins.json"
    bad.write_text(json.dumps({"plugins": "not-a-dict"}))
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", bad)
    monkeypatch.setattr(plugins_mod, "SETTINGS_PATH", tmp_path / "nope.json")
    resp = await client.get("/api/plugins")
    assert resp.status == 200
    assert await resp.json() == []


async def test_settings_local_endpoint(client, tmp_path, monkeypatch):
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps({"localKey": "v"}))
    monkeypatch.setattr(settings_mod, "SETTINGS_LOCAL_PATH", p)
    data = await (await client.get("/api/settings/local")).json()
    assert data["data"]["localKey"] == "v" and data["etag"]


async def test_settings_local_missing_is_empty(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_LOCAL_PATH", tmp_path / "absent.json")
    data = await (await client.get("/api/settings/local")).json()
    assert data["data"] == {} and data["etag"] is None


async def test_chat_stop_requires_session_id(client):
    assert (await client.post("/api/chat/stop", json={})).status == 400


async def test_chat_stop_calls_manager(client):
    fake_manager = AsyncMock()
    fake_manager.stop = AsyncMock()
    with patch.dict(client.app, {"session_manager": fake_manager}):
        resp = await client.post("/api/chat/stop", json={"session_id": "sess-1"})
    assert resp.status == 200
    assert (await resp.json())["stopped"] == "sess-1"
    fake_manager.stop.assert_awaited_once_with("sess-1")


# --------------------------------------------------------------------------- #
# Robustness hardening added by the codebase-review follow-up:
# - malformed JSON bodies → 400 (not 500) via read_json_body (B12)
# - MCP secret masking covers http/sse `headers`; config must be a dict (B2/B3)
# - complete_task conflict → 409 (B1)
# --------------------------------------------------------------------------- #

async def test_malformed_json_body_returns_400(client, settings_file):
    """A non-JSON body to a JSON endpoint is a clean 400, not a 500."""
    resp = await client.put("/api/settings", data="this is not json",
                            headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_non_object_json_body_returns_400(client, settings_file):
    """A JSON body that isn't an object (e.g. a list) is rejected with 400."""
    resp = await client.put("/api/settings", json=[1, 2, 3])
    assert resp.status == 400


async def test_mcp_masks_header_secrets(client, mcp_files):
    """http/sse server tokens in `headers` are masked in GET (B2)."""
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {
        "remote": {
            "type": "http", "url": "https://x",
            "headers": {"Authorization": "Bearer SECRET", "X-Trace": "ok"},
        },
    }}), encoding="utf-8")
    data = await (await client.get("/api/mcp")).json()
    srv = next(s for s in data["servers"] if s["name"] == "remote")
    assert srv["headers"]["Authorization"] == "***"   # secret masked
    assert srv["headers"]["X-Trace"] == "ok"           # non-secret kept


async def test_mcp_unmasks_header_on_write(client, mcp_files):
    """A round-tripped masked header is restored to the stored secret (B2)."""
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {
        "remote": {"type": "http", "url": "https://x",
                   "headers": {"Authorization": "Bearer REAL"}},
    }}), encoding="utf-8")
    etag = (await (await client.get("/api/mcp")).json())["etag"]
    # Client sends back the masked value — server must not clobber the secret.
    resp = await client.put("/api/mcp/remote", json={
        "config": {"type": "http", "url": "https://x",
                   "headers": {"Authorization": "***"}},
        "etag": etag,
    })
    assert resp.status == 200
    stored = json.loads(mcp_files["standard"].read_text())
    assert stored["mcpServers"]["remote"]["headers"]["Authorization"] == "Bearer REAL"


async def test_mcp_rejects_non_dict_config(client, mcp_files):
    """A non-object config is rejected with 400 (B3)."""
    mcp_files["standard"].write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    resp = await client.post("/api/mcp", json={"name": "x", "config": "not-a-dict"})
    assert resp.status == 400
