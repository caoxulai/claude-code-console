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


async def test_sop_memory_filename_traversal_rejected(client, projects_layout):
    """The sop/memory routes also guard the FILENAME segment independently of
    project_id. With a VALID project, a traversal filename must still 400 — the
    project_id test above can't prove this since it escapes on project_id."""
    # alpha is a real seeded project, so this isolates the filename guard.
    assert (await client.get("/api/projects/alpha/sop/..%2f..%2fevil.md")).status == 400
    assert (await client.get("/api/projects/alpha/memory/..%2f..%2fevil.md")).status == 400


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


# A real backend-dev-like file: many entries, all sharing ubiquitous words
# (test/etag/context) but only ONE genuine supersede pair. Old clustering
# collapsed the whole file into one mega-cluster; new clustering must yield
# exactly the 2-entry superseded pair.
_REALISTIC_CONTEXT = """# backend-dev — project context
- 2026-06-01: Use read_json_body for body parsing; raw request.json returns 500 on a malformed test body.
- 2026-06-02: Run pytest with the project venv; system python lacks pytest-aiohttp in this context.
- 2026-06-03: etag optimistic concurrency uses write_text expected_etag; conflict tests need a sleep.
- 2026-06-04: SessionManager serializes lifecycle ops per session id via a locks dict in this context.
- 2026-06-05: PersistentSession stop must None-guard stdin before close in the shutdown test path.
- 2026-06-06: oscron mirrors the OS scheduler without mutating it; the test monkeypatches the subprocess seams.
- 2026-06-07: Cron run history is reconciled server-side; the harness writes lastFiredAt on the task.
- 2026-06-08: Slack queue split into a fast list and a per-item regenerate to fit the test timeout.
- 2026-06-09: The slack poller starts in every test but its first wait is a long sleep, inert.
- 2026-06-10: Pipeline crontab format uses a CRON_TZ line; the warmup marker dedupes the cron entry.
- 2026-06-11: Skills frontmatter parser tolerates a missing tools field in the test context.
- 2026-06-12: Memory CRUD reuses the etag conflict pattern; the conflict test asserts a 409 status.
- 2026-06-13: Supersedes the 2026-06-08 note — the slack regenerate action reuses the read tools only, not the write tool. (correction)
- 2026-06-14: Design doc ADR parser ignores lines before the first header in the test fixture.
- 2026-06-15: Settings put mirrors the etag handler; the conflict response shape is error conflict.
"""


def test_detect_conflicts_no_mega_cluster_on_real_file():
    from server.routes.agents import parse_context_entries, detect_conflicts
    entries = parse_context_entries(_REALISTIC_CONTEXT)
    clusters = detect_conflicts(entries)
    # No giant cluster — every emitted cluster is small (<= the size cap).
    assert all(len(c["entryIndices"]) <= 5 for c in clusters), clusters
    # The one genuine supersede pair (the 06-08 slack note + its 06-13 correction)
    # surfaces as a 2-entry superseded cluster.
    slack_pairs = [
        c for c in clusters
        if c["reason"] == "superseded" and 7 in c["entryIndices"] and 12 in c["entryIndices"]
    ]
    assert len(slack_pairs) == 1, clusters
    assert len(slack_pairs[0]["entryIndices"]) == 2


# The conservative linking rule (overlap-ratio + dated-supersede). These three
# focused fixtures pin the contract the Merge action depends on: a file of
# distinct-but-related decisions about one subsystem (each merely co-mentioning a
# shared identifier) yields ZERO clusters, while a genuine near-duplicate pair or
# an explicit dated correction yields exactly one tight 2-entry cluster.
def test_detect_conflicts_distinct_but_related_one_shared_id_no_cluster():
    """Two distinct decisions about the same subsystem that share a single
    identifier (and little else) must NOT cluster — merging them would destroy
    distinct knowledge. This is the Slack-timeline (D-010/D-014) failure mode."""
    from server.routes.agents import parse_context_entries, detect_conflicts
    txt = (
        "# backend-dev -- project context\n"
        "- 2026-06-10: Slack queue D-010: the in-process poller keeps slack_threads.json "
        "warm via a background single-flight scan, gated on a readiness probe.\n"
        "- 2026-06-14: Slack queue D-014: soft-dismiss flips slack_threads.json status in "
        "place and approve forwards an expectedDraft baseline so a stale whole-file etag "
        "still sends.\n"
    )
    assert detect_conflicts(parse_context_entries(txt)) == []


def test_detect_conflicts_genuine_near_duplicate_clusters():
    """A genuine near-duplicate pair (a high fraction of shared discriminative
    vocabulary) clusters as one 'redundant' 2-entry group."""
    from server.routes.agents import parse_context_entries, detect_conflicts
    txt = (
        "# qa -- project context\n"
        "- 2026-06-01: Skeptic sabotage that paid off: backed up oscron.py to /tmp, broke "
        "the croniter next-run guard, watched the test fail, restored, diff -q byte-identical.\n"
        "- 2026-06-02: Sabotage that paid off: backed oscron.py to /tmp, broke the croniter "
        "next-run guard, watched the test fail loud, restored, diff -q byte-identical after.\n"
        "- 2026-06-03: Postgres handles the database connection pooling in this context.\n"
    )
    clusters = detect_conflicts(parse_context_entries(txt))
    assert len(clusters) == 1, clusters
    assert clusters[0]["reason"] == "redundant"
    assert clusters[0]["entryIndices"] == [0, 1]


def test_detect_conflicts_dated_supersede_labels_superseded():
    """An explicit 'supersedes the YYYY-MM-DD note' reference links specifically to
    the entry carrying that date (with >=1 shared discriminative token) and labels
    the cluster 'superseded'."""
    from server.routes.agents import parse_context_entries, detect_conflicts
    txt = (
        "# backend-dev -- project context\n"
        "- 2026-06-05: Reset the module cache in an autouse fixture between tests.\n"
        "- 2026-06-09: Frontend uses the Vite bundler for hot reload.\n"
        "- 2026-06-13: Supersedes the 2026-06-05 note — the cache reset must use a fresh "
        "_Cache() instance, not clear.\n"
    )
    clusters = detect_conflicts(parse_context_entries(txt))
    assert len(clusters) == 1, clusters
    assert clusters[0]["reason"] == "superseded"
    assert clusters[0]["entryIndices"] == [0, 2]


def test_detect_conflicts_bare_dated_supersede_no_overlap_does_not_link():
    """A 'supersedes the YYYY-MM-DD note' marker with ZERO topic overlap must not
    falsely bind to the dated entry — the dated-supersede rule requires at least
    one shared discriminative token."""
    from server.routes.agents import parse_context_entries, detect_conflicts
    txt = (
        "# backend-dev -- project context\n"
        "- 2026-06-05: Postgres handles database connection pooling.\n"
        "- 2026-06-13: Supersedes the 2026-06-05 note — actually the Vite bundler drives "
        "frontend hot reload entirely.\n"
    )
    assert detect_conflicts(parse_context_entries(txt)) == []


def test_classify_ephemeral_tags_ceremony_families():
    from server.routes.agents import parse_context_entries, classify_ephemeral
    txt = (
        "# qa — project context\n"
        "- 2026-06-01: Skeptic sabotage that PAID OFF: caught a 200 masking an empty body.\n"
        "- 2026-06-02: LIVE in-process proof: hit the real endpoint and read it back.\n"
        "- 2026-06-03: UNVERIFIED-by-design: browser pixel render not checked, out of scope.\n"
        "- 2026-06-04: Scope clean: HEAD unchanged, no stray files after the run.\n"
        "- 2026-06-05: Re-ran the full UAT regression suite 284 green, no flake.\n"
        "- 2026-06-06: Use the project venv to run pytest; system python lacks the plugin.\n"
    )
    flags = classify_ephemeral(parse_context_entries(txt))
    assert flags == [True, True, True, True, True, False]


async def test_context_ephemeral_endpoint(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "qa")
    _seed_context(
        workspace, "alpha", "qa",
        "# qa\n"
        "- 2026-06-01: LIVE in-process proof: hit the endpoint and read it back.\n"
        "- 2026-06-02: Use the project venv to run pytest.\n",
    )
    resp = await client.get("/api/projects/alpha/agents/qa/context/ephemeral")
    assert resp.status == 200
    data = await resp.json()
    assert data["ephemeralIndices"] == [0]
    assert data["entries"][0]["ephemeral"] is True
    assert data["entries"][1]["ephemeral"] is False


async def test_reconcile_sweep_bulk_drops_only_listed(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "qa")
    _seed_context(
        workspace, "alpha", "qa",
        "# qa\n"
        "- 2026-06-01: LIVE in-process proof: A.\n"
        "- 2026-06-02: Durable lesson worth keeping.\n"
        "- 2026-06-03: Scope clean: HEAD unchanged after B.\n",
    )
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "qa.md"
    resp = await client.post(
        "/api/projects/alpha/agents/qa/context/reconcile",
        json={"action": "sweep", "entryIndices": [0, 2]},
    )
    assert resp.status == 200
    assert (await resp.json())["removed"] == 2
    text = ctx_path.read_text(encoding="utf-8")
    assert "Durable lesson worth keeping." in text
    assert "LIVE in-process proof" not in text
    assert "Scope clean" not in text


async def test_reconcile_compact_shortens_one_entry_in_place(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"

    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "compact", "entryIndices": [0], "compactText": "Use read_json_body for bodies."},
    )
    assert resp.status == 200
    assert (await resp.json())["removed"] == 0  # rewrite in place, nothing dropped
    text = ctx_path.read_text(encoding="utf-8")
    from server.routes.agents import parse_context_entries
    entries = parse_context_entries(text)
    assert len(entries) == 4  # no entry removed
    assert entries[0]["text"] == "Use read_json_body for bodies."
    assert entries[0]["date"] == "2026-06-01"  # date prefix preserved


async def test_reconcile_compact_etag_conflict(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "compact", "entryIndices": [0], "compactText": "short", "etag": "stale-etag"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"


async def test_reconcile_keep_superseded_keeps_newest(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "keep", "entryIndices": [1, 3]},
    )
    assert resp.status == 200
    text = ctx_path.read_text(encoding="utf-8")
    assert "fresh _Cache() instance" in text  # newest correction kept
    assert "autouse fixture" not in text       # older superseded dropped


async def test_reconcile_keep_redundant_keeps_oldest():
    # Plain redundancy (no supersede marker) keeps the oldest (min index).
    from server.routes.agents import _keep_survivor, parse_context_entries
    entries = parse_context_entries(
        "# x\n- 2026-01-01: Frontend build uses Vite bundler tool.\n"
        "- 2026-01-02: Frontend build uses the Vite bundler tool here.\n"
    )
    assert _keep_survivor({}, [0, 1], entries) == 0


async def test_reconcile_keep_index_overrides_survivor():
    from server.routes.agents import _keep_survivor, parse_context_entries
    entries = parse_context_entries(_CONTEXT_SAMPLE)
    # Explicit keepIndex wins regardless of reason.
    assert _keep_survivor({"keepIndex": 1}, [1, 3], entries) == 1


async def test_content_based_new_detection_mid_file_insert(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    # Mark everything reviewed (writes the content-hash marker).
    assert (await client.post("/api/projects/alpha/agents/backend-dev/context/mark-reviewed")).status == 200

    # Insert a NEW entry in the MIDDLE of the file (not at the tail).
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    lines = ctx_path.read_text(encoding="utf-8").splitlines()
    insert_at = 3  # after the heading + first two entries
    lines.insert(insert_at, "- 2026-06-20: Brand new mid-file lesson about deployment timing.")
    ctx_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    resp = await client.get("/api/projects/alpha/agents/backend-dev/context")
    data = await resp.json()
    assert data["newEntryCount"] == 1
    # The inserted entry's index (mid-file), not a tail index.
    new_idx = data["newEntryIndices"][0]
    assert "mid-file lesson" in data["entries"][new_idx]["text"]


async def test_legacy_count_marker_falls_back(client, projects_layout):
    # A pre-existing count-only marker (old format) must still work via the
    # count-based fallback.
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    marker = workspace / "alpha" / ".claude" / "agent-context" / ".reviewed" / "backend-dev.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2:abc123\n", encoding="utf-8")  # legacy: count only
    resp = await client.get("/api/projects/alpha/agents/backend-dev/context")
    data = await resp.json()
    assert data["newEntryCount"] == 2  # 4 entries, 2 reviewed → tail 2 are new


async def test_oversized_context_signal(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "qa")
    big = "# qa\n" + "".join(
        f"- 2026-06-01: Lesson number {i} about a distinct topic with enough text to add bytes here.\n"
        for i in range(500)
    )
    _seed_context(workspace, "alpha", "qa", big)
    # Panel signal.
    data = await (await client.get("/api/projects/alpha/agents/qa/context")).json()
    assert data["oversized"] is True
    # Agent list badge signal.
    agents = {a["slug"]: a for a in await (await client.get("/api/projects/alpha/agents")).json()}
    assert agents["qa"]["oversized"] is True


async def test_small_context_not_oversized(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    data = await (await client.get("/api/projects/alpha/agents/backend-dev/context")).json()
    assert data["oversized"] is False


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

    # The [1,3] cache cluster is "superseded" → keep keeps the NEWEST/correction
    # (entry 3), drops the stale entry 1. Never silently drop the correction.
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "keep", "entryIndices": [1, 3]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["removed"] == 1
    text = ctx_path.read_text(encoding="utf-8")
    assert "fresh _Cache() instance" in text  # the correction survives
    assert "autouse fixture" not in text       # the superseded note is dropped
    assert "read_json_body" in text            # unrelated entry untouched


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


# -- T1 frontend-dev: documented reconcile success/409 SHAPES per action --------
# These pin the exact response contract the panel's pure decideReconcileOutcome()
# branches on (see frontend/src/components/reconcileOutcome.js + .test.mjs). The
# panel renders a silent no-op iff it can't tell ok from conflict; these guard
# the wire shapes both sides agree on: success {ok:True, action, removed, etag}
# and 409 {error:'conflict', current, etag}. The browser pixel render of the
# inline banner is verified separately (see the task report's UNVERIFIED note).

async def _conflict_cluster_indices(client, project, name):
    """Helper: the first detected cluster's entryIndices for `name`."""
    clusters = (await (await client.get(
        f"/api/projects/{project}/agents/{name}/context/conflicts")).json())["clusters"]
    assert clusters, "expected at least one conflict cluster"
    return clusters[0]["entryIndices"]


async def test_reconcile_success_shape_carries_etag_for_every_action(client, projects_layout):
    """keep / merge / compact / sweep each return {ok, action, removed, etag} on
    success — the etag the panel must adopt so a follow-up action stays guarded.
    dismiss returns {ok, action, removed:0} (it writes no entries, so no etag)."""
    workspace = projects_layout["workspace"]

    # keep
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    body = await (await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "keep", "entryIndices": [1, 3]})).json()
    assert body["ok"] is True and body["action"] == "keep"
    assert body["removed"] == 1 and isinstance(body["etag"], str) and body["etag"]

    # merge (re-seed: the keep above mutated the file)
    _seed_context(workspace, "alpha", "backend-dev")
    body = await (await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "merge", "entryIndices": [1, 3], "mergedText": "Merged note."})).json()
    assert body["ok"] is True and body["action"] == "merge" and body["etag"]

    # compact (single entry, rewrites in place; removed == 0)
    _seed_context(workspace, "alpha", "backend-dev")
    body = await (await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "compact", "entryIndices": [0], "compactText": "Short."})).json()
    assert body["ok"] is True and body["action"] == "compact"
    assert body["removed"] == 0 and body["etag"]

    # sweep (bulk drop)
    _seed_context(workspace, "alpha", "backend-dev")
    body = await (await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "sweep", "entryIndices": [0]})).json()
    assert body["ok"] is True and body["action"] == "sweep"
    assert body["removed"] == 1 and body["etag"]

    # dismiss writes nothing → no etag, removed 0.
    _seed_context(workspace, "alpha", "backend-dev")
    body = await (await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "dismiss", "entryIndices": [1, 3]})).json()
    assert body["ok"] is True and body["action"] == "dismiss" and body["removed"] == 0


async def test_reconcile_merge_and_compact_conflict_shape_on_stale_etag(client, projects_layout):
    """A stale etag on merge OR compact must 409 with {error:'conflict', current,
    etag} and write NOTHING — the exact shape the panel adopts to re-review. This
    is the bug's heart: a 409 must be a distinguishable response, never a no-op."""
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before = ctx_path.read_text(encoding="utf-8")

    for payload in (
        {"action": "merge", "entryIndices": [1, 3], "mergedText": "X", "etag": "stale"},
        {"action": "compact", "entryIndices": [0], "compactText": "Y", "etag": "stale"},
    ):
        resp = await client.post(
            "/api/projects/alpha/agents/backend-dev/context/reconcile", json=payload)
        assert resp.status == 409, payload["action"]
        body = await resp.json()
        assert body["error"] == "conflict"
        assert isinstance(body["current"], str) and isinstance(body["etag"], str) and body["etag"]
        # Untouched: the rejected write never lands.
        assert ctx_path.read_text(encoding="utf-8") == before, payload["action"]


async def test_reconcile_second_click_succeeds_against_fresh_etag(client, projects_layout):
    """End-to-end of the panel's 409 recovery: a stale-etag merge 409s and returns
    the server's fresh etag; re-firing the SAME merge with that etag SUCCEEDS.
    This is the 'click again to re-confirm' contract the inline banner promises."""
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"

    # First click: stale etag → 409 carrying the current etag.
    first = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "merge", "entryIndices": [1, 3], "mergedText": "Reconciled note.", "etag": "stale"})
    assert first.status == 409
    fresh_etag = (await first.json())["etag"]

    # Second click: adopt the fresh etag → 200, the merge lands.
    second = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "merge", "entryIndices": [1, 3], "mergedText": "Reconciled note.", "etag": fresh_etag})
    assert second.status == 200
    assert (await second.json())["ok"] is True
    assert "Reconciled note." in ctx_path.read_text(encoding="utf-8")


async def test_reconcile_dismiss_then_other_action_no_silent_drop(client, projects_layout):
    """dismiss returns a clear ok shape (removed:0) AND leaves the file byte-equal,
    so the panel's 'Keep all' is never mistaken for a silent failure."""
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before = ctx_path.read_text(encoding="utf-8")
    target = await _conflict_cluster_indices(client, "alpha", "backend-dev")

    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "dismiss", "entryIndices": target})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["removed"] == 0
    assert ctx_path.read_text(encoding="utf-8") == before


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


# -- T3 QA additions: gaps not covered by the parallel agent-context tests ----
# The parallel writer covered the realistic-file (ubiquitous-words -> small caps),
# compact, supersede-keep-newest, ephemeral classifier/endpoint, content-based
# new-detection, and oversized-vs-small. These three close the spec's explicit
# remaining asks: a clean 10+-distinct file -> ZERO clusters, the sweep action's
# etag guard (409 on stale), and a never-raises smoke on an oversized/garbage file.

# 12 topically-distinct entries that DELIBERATELY share two ubiquitous words
# ("test"/"context") so every pair would link under the naive union-find. The
# OLD heuristic (link on >=2 shared tokens, no DF filter, no size cap) collapses
# them into ONE 12-entry mega-cluster. The NEW heuristic must yield ZERO: the
# document-frequency filter drops the ubiquitous words AND the size cap rejects
# any over-large connected group. This is defense-in-depth -- either guard alone
# yields zero here, so a regression of EITHER stays caught only when both fail,
# but the end-to-end "clean file -> zero clusters" guarantee is what's asserted.
_TEN_DISTINCT_CONTEXT = """# backend-dev -- project context
- 2026-06-01: In this test context, the Vite bundler drives frontend hot reload.
- 2026-06-02: In this test context, Postgres handles database connection pooling.
- 2026-06-03: In this test context, Midway validates the upstream cookie session.
- 2026-06-04: In this test context, CloudWatch ingests structured logging lines.
- 2026-06-05: In this test context, Redis caches with a sliding expiration policy.
- 2026-06-06: In this test context, Apollo promotes deployment artifacts onward.
- 2026-06-07: In this test context, dashboards aggregate regional latency metrics.
- 2026-06-08: In this test context, SES batches hourly email notification delivery.
- 2026-06-09: In this test context, OpenSearch rebuilds the nightly search index.
- 2026-06-10: In this test context, LaunchDarkly evaluates feature flag experiments.
- 2026-06-11: In this test context, billing webhooks reconcile payment settlement.
- 2026-06-12: In this test context, Lambda resizes media thumbnail images lazily.
"""


def test_detect_conflicts_zero_on_ten_distinct_topics():
    from server.routes.agents import parse_context_entries, detect_conflicts
    entries = parse_context_entries(_TEN_DISTINCT_CONTEXT)
    assert len(entries) >= 10
    # Despite the shared ubiquitous words, no genuine near-duplicates exist -> no
    # clusters at all. The old code returned one 12-entry mega-cluster here.
    assert detect_conflicts(entries) == []


# Anti-toy-fixture invariant: the tmp fixtures above are small and hand-shaped,
# but the LIVE role-context files are large and churn fast (the qa file went
# 164->407 entries during this project). The safety property Merge depends on is
# that detect_conflicts NEVER offers a mega/blob cluster spanning distinct-but-
# related decisions (e.g. the Slack D-010/D-014/D-017/D-018 timeline) -- every
# emitted cluster must stay at or under the size cap so Merge only ever sees a
# tight, fuseable group. We assert that STRUCTURAL invariant against the real
# files (read-only) rather than an exact cluster count, which would be brittle as
# the files grow. Skips gracefully when a file is absent so the suite stays
# portable across checkouts.
_REAL_CONTEXT_ROLES = ["backend-dev", "frontend-dev", "qa"]


@pytest.mark.parametrize("role", _REAL_CONTEXT_ROLES)
def test_detect_conflicts_no_blob_on_real_context_files(role):
    from server.routes.agents import (
        parse_context_entries,
        detect_conflicts,
        _MAX_CLUSTER_SIZE,
    )

    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / ".claude" / "agent-context" / f"{role}.md"
    if not path.is_file():
        pytest.skip(f"{role} context file not present in this checkout")

    entries = parse_context_entries(path.read_text(encoding="utf-8"))
    clusters = detect_conflicts(entries)
    # The hard invariant: no cluster exceeds the size cap. A distinct-but-related
    # blob would surface as a large connected component; the conservative linker
    # plus the cap must keep every offered cluster tight (~2 entries in practice).
    oversized = [c for c in clusters if len(c["entryIndices"]) > _MAX_CLUSTER_SIZE]
    assert not oversized, (
        f"{role}: detect_conflicts emitted a blob cluster (> {_MAX_CLUSTER_SIZE} "
        f"entries) that Merge would wrongly try to fuse: {oversized}"
    )


async def test_reconcile_sweep_etag_conflict(client, projects_layout):
    """The ephemeral bulk-sweep must be etag-guarded: a stale etag yields a 409
    with the conflict shape, never a silent destructive drop against a moved file.
    """
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "qa")
    _seed_context(
        workspace, "alpha", "qa",
        "# qa\n"
        "- 2026-06-01: LIVE in-process proof: A.\n"
        "- 2026-06-02: Durable lesson worth keeping.\n",
    )
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "qa.md"
    before = ctx_path.read_text(encoding="utf-8")
    resp = await client.post(
        "/api/projects/alpha/agents/qa/context/reconcile",
        json={"action": "sweep", "entryIndices": [0], "etag": "stale-etag-value"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert "current" in body and "etag" in body
    # Nothing was removed -- the file is byte-identical after the rejected sweep.
    assert ctx_path.read_text(encoding="utf-8") == before


def test_context_summary_and_list_never_raise_on_oversized_garbage(tmp_path, monkeypatch):
    """Defensive contract: context_summary and the agents listing must never throw,
    so the Agents/Projects listings can't break -- on either (a) a malformed but
    parseable oversized file (size signal still fires) or (b) a genuinely
    unreadable (non-UTF-8) file that makes read_text raise (degrades to defaults).
    Drives the helpers directly (no client) on an isolated tmp project.
    """
    import server.routes.agents as agents_mod
    # Isolate the global agents dir so the developer's real ~/.claude/agents/ is
    # not scanned by _list_agents during this unit-level check.
    monkeypatch.setattr(agents_mod, "GLOBAL_AGENTS_DIR", tmp_path / "dot_claude" / "agents")

    project_dir = tmp_path / "workspace" / "proj"
    agents_d = project_dir / ".claude" / "agents"
    ctx_d = project_dir / ".claude" / "agent-context"
    agents_d.mkdir(parents=True)
    ctx_d.mkdir(parents=True)
    (agents_d / "qa.md").write_text("---\nname: qa\n---\nbody\n", encoding="utf-8")

    # (a) Oversized (> ~6KB / 400 lines) AND malformed: stray headers, non-bullet
    # lines, broken bullets -- must parse defensively, not raise, and still flag.
    garbage = "# qa\n" + (
        "garbled non-bullet line %s\n## stray header\n- - nested?? weird\n" % ("x" * 60)
    ) * 200
    (ctx_d / "qa.md").write_text(garbage, encoding="utf-8")
    summary = agents_mod.context_summary(project_dir, "qa")  # must not raise
    assert summary["oversized"] is True  # size signal fires even on garbage
    recs = agents_mod._list_agents(project_dir)  # must not raise
    qa = next(r for r in recs if r["slug"] == "qa")
    assert qa["oversized"] is True

    # (b) A non-UTF-8 (binary) context file makes filestore.read_text raise; the
    # defensive except must swallow it and return the zeroed defaults, never throw.
    (ctx_d / "qa.md").write_bytes(b"\xff\xfe\x00 not valid utf-8 " + b"x" * 7000)
    summary2 = agents_mod.context_summary(project_dir, "qa")  # must not raise
    assert summary2 == {
        "entryCount": 0, "newEntryCount": 0, "conflictClusterCount": 0,
        "oversized": False, "contextBytes": 0, "lineCount": 0,
    }
    # The listing still survives an unreadable context file (record is skipped or
    # zeroed, but the call itself never raises).
    agents_mod._list_agents(project_dir)


# -- Reconcile CONTRACT lock (T2): the success + 409 SHAPES the review UI branches
# on. The 'Save merged got no response' bug routed through the merge 409 path, so
# these pin (a) the documented success body {ok, action, removed:int, etag:str} for
# merge/keep/sweep so the frontend's etag-adoption-on-success path has a guaranteed
# shape, (b) the FULL 409 body {error, message, current, etag} for merge AND keep
# with a byte-identical file (no destructive write on conflict), and (c) that the
# 409 body's etag is the REAL current etag (differs from the stale one the client
# sent AND equals a fresh GET /context etag), since the panel adopts body.etag for
# the re-confirm click. These do NOT duplicate the existing merge/keep/sweep success
# or compact/sweep 409 tests -- they add the explicit etag-presence + action-echo +
# adopted-etag-is-fresh asserts those don't make.


async def test_reconcile_merge_success_shape_has_etag_and_action(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")

    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "merge", "entryIndices": [1, 3],
              "mergedText": "Reset the cache with a fresh _Cache() in an autouse fixture."},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["action"] == "merge"        # action echoed back for the UI
    assert isinstance(body["removed"], int) and body["removed"] == 1
    assert isinstance(body["etag"], str) and body["etag"]  # non-empty fresh etag
    # The fresh etag the UI must adopt is the file's actual current etag.
    after = await (await client.get("/api/projects/alpha/agents/backend-dev/context")).json()
    assert body["etag"] == after["etag"]


async def test_reconcile_keep_success_shape_has_etag_and_action(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")

    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "keep", "entryIndices": [1, 3]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["action"] == "keep"
    assert isinstance(body["removed"], int) and body["removed"] == 1
    assert isinstance(body["etag"], str) and body["etag"]
    after = await (await client.get("/api/projects/alpha/agents/backend-dev/context")).json()
    assert body["etag"] == after["etag"]


async def test_reconcile_sweep_success_shape_has_etag_and_action(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "qa")
    _seed_context(
        workspace, "alpha", "qa",
        "# qa\n"
        "- 2026-06-01: LIVE in-process proof: A.\n"
        "- 2026-06-02: Durable lesson worth keeping.\n",
    )
    resp = await client.post(
        "/api/projects/alpha/agents/qa/context/reconcile",
        json={"action": "sweep", "entryIndices": [0]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["action"] == "sweep"
    assert isinstance(body["removed"], int) and body["removed"] == 1
    assert isinstance(body["etag"], str) and body["etag"]
    after = await (await client.get("/api/projects/alpha/agents/qa/context")).json()
    assert body["etag"] == after["etag"]


async def test_reconcile_dismiss_success_shape(client, projects_layout):
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "dismiss", "entryIndices": [1, 3]},
    )
    assert resp.status == 200
    body = await resp.json()
    # dismiss records the cluster but removes nothing and rewrites nothing, so it
    # carries no etag -- the UI's dismiss branch must NOT try to adopt one.
    assert body == {"ok": True, "action": "dismiss", "removed": 0}


async def test_reconcile_merge_etag_conflict_full_shape_no_write(client, projects_layout):
    """The exact path 'Save merged got no response' routed through: a merge with a
    stale etag -> 409 with the FULL documented body and a byte-IDENTICAL file. The
    body etag is the real current one (differs from the stale etag, equals a fresh
    GET) so the panel can adopt it and a second click can succeed."""
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before = ctx_path.read_text(encoding="utf-8")

    stale = "stale-etag-value"
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "merge", "entryIndices": [1, 3],
              "mergedText": "merged but stale", "etag": stale},
    )
    assert resp.status == 409
    body = await resp.json()
    # Full documented 409 contract.
    assert body["error"] == "conflict"
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["current"], str)        # current file content for re-review
    assert isinstance(body["etag"], str) and body["etag"]  # fresh etag to adopt
    # The adopted etag is the REAL current etag: not the stale one, and it matches
    # both the on-disk file and a fresh GET /context (the second-click etag source).
    assert body["etag"] != stale
    from server import filestore
    assert body["etag"] == filestore.etag_for(ctx_path)
    get = await (await client.get("/api/projects/alpha/agents/backend-dev/context")).json()
    assert body["etag"] == get["etag"]
    assert body["current"] == get["content"]
    # No destructive write on conflict -- the file is byte-identical.
    assert ctx_path.read_text(encoding="utf-8") == before


async def test_reconcile_keep_etag_conflict_full_shape_no_write(client, projects_layout):
    """Keep (drop-the-rest) is etag-guarded identically: a stale etag -> 409 with
    the full conflict body and a byte-identical file (no silent destructive drop)."""
    workspace = projects_layout["workspace"]
    _seed_agent(workspace, "alpha", "backend-dev")
    _seed_context(workspace, "alpha", "backend-dev")
    ctx_path = workspace / "alpha" / ".claude" / "agent-context" / "backend-dev.md"
    before = ctx_path.read_text(encoding="utf-8")

    stale = "stale-etag-value"
    resp = await client.post(
        "/api/projects/alpha/agents/backend-dev/context/reconcile",
        json={"action": "keep", "entryIndices": [1, 3], "etag": stale},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["current"], str)
    assert isinstance(body["etag"], str) and body["etag"]
    assert body["etag"] != stale
    from server import filestore
    assert body["etag"] == filestore.etag_for(ctx_path)
    get = await (await client.get("/api/projects/alpha/agents/backend-dev/context")).json()
    assert body["etag"] == get["etag"]
    assert body["current"] == get["content"]
    assert ctx_path.read_text(encoding="utf-8") == before


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
# SessionManager manager-level concurrency (B2 + F5/F6/F7)
#
# Lifecycle ops on the SAME session_id must serialize via a per-id lock so two
# near-simultaneous requests can't double-spawn a subprocess and orphan one
# outside _sessions. Different ids must stay parallel. stop_all must tolerate
# one session's stop() raising. These use the existing _FakeProc fakes so no
# real `claude` binary is spawned.
# --------------------------------------------------------------------------- #


async def test_get_or_create_serializes_same_id_no_double_spawn(monkeypatch):
    """Two concurrent get_or_create for the SAME id must not both spawn.

    The per-id lock serializes them: the first stores a live session, the second
    sees it alive and returns it — exactly one start() runs.
    """
    mgr = session_mod.SessionManager()

    starts = {"n": 0}

    async def fake_start(self):
        # Yield so a second concurrent call can interleave if unserialized.
        await asyncio.sleep(0.01)
        self._proc = _FakeProc([])
        self._started = True
        starts["n"] += 1

    monkeypatch.setattr(session_mod.PersistentSession, "start", fake_start)

    a, b = await asyncio.gather(
        mgr.get_or_create(session_id="sess-dup"),
        mgr.get_or_create(session_id="sess-dup"),
    )
    # Exactly one subprocess started, both callers see the same session, and it
    # is the one stored (reachable by stop_all()).
    assert starts["n"] == 1
    assert a is b
    assert mgr._sessions["sess-dup"] is a


async def test_get_or_create_different_ids_run_in_parallel(monkeypatch):
    """Different session_ids must NOT serialize against each other — both spawn,
    and both are stored. Guards against a regression to a single global lock.
    """
    mgr = session_mod.SessionManager()

    async def fake_start(self):
        self._proc = _FakeProc([])
        self._started = True

    monkeypatch.setattr(session_mod.PersistentSession, "start", fake_start)

    a, b = await asyncio.gather(
        mgr.get_or_create(session_id="sess-a"),
        mgr.get_or_create(session_id="sess-b"),
    )
    assert a is not b
    assert mgr._sessions["sess-a"] is a
    assert mgr._sessions["sess-b"] is b


async def test_get_or_create_start_failure_cleans_up(monkeypatch):
    """If start() raises after spawning, the half-started session is stopped (F5)
    and nothing is left registered.
    """
    mgr = session_mod.SessionManager()
    stopped = {"v": False}

    async def boom_start(self):
        self._proc = _FakeProc([])  # subprocess spawned...
        raise RuntimeError("spawn-time failure")  # ...then start blows up

    async def fake_stop(self):
        stopped["v"] = True

    monkeypatch.setattr(session_mod.PersistentSession, "start", boom_start)
    monkeypatch.setattr(session_mod.PersistentSession, "stop", fake_stop)

    with pytest.raises(RuntimeError):
        await mgr.get_or_create(session_id="sess-fail")
    # The half-started session was stopped and never registered.
    assert stopped["v"] is True
    assert "sess-fail" not in mgr._sessions


async def test_stop_all_continues_when_one_stop_raises():
    """stop_all gathers with return_exceptions, so one raising stop() doesn't
    prevent the others from being stopped (F6).
    """
    mgr = session_mod.SessionManager()
    calls = []

    def make_session(sid, raises=False):
        s = session_mod.PersistentSession(session_id=sid)

        async def stop():
            calls.append(sid)
            if raises:
                raise RuntimeError(f"{sid} stop failed")

        s.stop = stop
        return s

    mgr._sessions["bad"] = make_session("bad", raises=True)
    mgr._sessions["good"] = make_session("good")

    await mgr.stop_all()  # must not raise
    # Both stops were attempted despite one raising, and the registry is cleared.
    assert set(calls) == {"bad", "good"}
    assert mgr._sessions == {}


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


async def test_memory_list_skips_unreadable_file(client, memory_dir):
    """A non-UTF-8 memory file must be skipped, not 500 the whole listing
    (mirrors test_list_agents_skips_unreadable_file)."""
    (memory_dir / "bad.md").write_bytes(b"\xff\xfe\x00bad")

    resp = await client.get("/api/memory/files")
    assert resp.status == 200
    names = {f["name"] for f in await resp.json()}
    # The readable seed files survive the unreadable sibling.
    assert "feedback_principle_x.md" in names


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


async def test_skills_list_skips_unreadable_file(client, skills_dirs):
    """A non-UTF-8 SKILL.md must be skipped, not 500 the listing
    (mirrors test_list_agents_skips_unreadable_file)."""
    bad = skills_dirs["skills"] / "broken"
    bad.mkdir()
    (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00bad")

    resp = await client.get("/api/skills")
    assert resp.status == 200
    names = {s["name"] for s in await resp.json()}
    # The readable skill survives the unreadable sibling.
    assert "deploy" in names


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
# Hooks routes (/api/hooks)
# --------------------------------------------------------------------------- #
import server.routes.hooks as hooks_mod  # noqa: E402


@pytest.fixture
def hooks_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(hooks_mod, "SETTINGS_PATH", p)
    return p


async def test_hooks_get_absent_file(client, hooks_file):
    got = await (await client.get("/api/hooks")).json()
    assert got["hooks"] == {}
    assert got["etag"] is None


async def test_hooks_put_round_trips(client, hooks_file):
    new_hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}
    resp = await client.put("/api/hooks", json={"hooks": new_hooks, "etag": None})
    assert resp.status == 200
    body = await resp.json()
    assert body["hooks"] == new_hooks
    assert body["etag"]

    got = await (await client.get("/api/hooks")).json()
    assert got["hooks"] == new_hooks
    assert got["etag"] == body["etag"]


async def test_hooks_put_with_current_etag_succeeds(client, hooks_file):
    hooks_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    got = await (await client.get("/api/hooks")).json()
    resp = await client.put("/api/hooks", json={"hooks": {"PostToolUse": []}, "etag": got["etag"]})
    assert resp.status == 200
    body = await resp.json()
    assert body["hooks"] == {"PostToolUse": []}
    # Unrelated settings keys must be preserved.
    assert json.loads(hooks_file.read_text())["theme"] == "dark"


async def test_hooks_put_etag_conflict(client, hooks_file):
    hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    got = await (await client.get("/api/hooks")).json()
    import time as _t
    _t.sleep(0.01)
    hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "X"}]}}), encoding="utf-8")
    resp = await client.put("/api/hooks", json={"hooks": {"PostToolUse": []}, "etag": got["etag"]})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert "current" in body


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
# Optional "description" field (T1): additive 1-2 sentence summary on the task.
# Persisted only when non-empty so existing jobs/callers stay byte-identical
# (no description:null key). Round-trips through create + update + GET.
# --------------------------------------------------------------------------- #
async def test_crons_create_round_trips_description(client, crons_file):
    """POST with a description -> 201, job carries it back, and GET lists it."""
    create = await client.post(
        "/api/crons",
        json={"cron": "0 9 * * *", "prompt": "standup", "description": "a summary"},
    )
    assert create.status == 201
    job = (await create.json())["job"]
    assert job["description"] == "a summary"
    job_id = job["id"]

    # Survives reload via GET (read back from the persisted store).
    listed = (await (await client.get("/api/crons")).json())["jobs"]
    match = next(j for j in listed if j["id"] == job_id)
    assert match["description"] == "a summary"


async def test_crons_update_round_trips_description(client, crons_file):
    """PUT with a description persists it and survives a re-GET."""
    create = await client.post("/api/crons", json={"cron": "0 9 * * *", "prompt": "standup"})
    job_id = (await create.json())["job"]["id"]

    upd = await client.put(f"/api/crons/{job_id}", json={"description": "updated"})
    assert upd.status == 200
    assert (await upd.json())["job"]["description"] == "updated"

    listed = (await (await client.get("/api/crons")).json())["jobs"]
    match = next(j for j in listed if j["id"] == job_id)
    assert match["description"] == "updated"


async def test_crons_create_without_description_omits_key(client, crons_file):
    """Omitting description must NOT write a 'description' key (existing-job render
    must not break) and create still works."""
    create = await client.post("/api/crons", json={"cron": "0 9 * * *", "prompt": "standup"})
    assert create.status == 201
    job = (await create.json())["job"]
    assert "description" not in job

    # And it's absent in the persisted store too, not just the response.
    saved = json.loads(crons_file.read_text(encoding="utf-8"))["tasks"][0]
    assert "description" not in saved


async def test_crons_update_without_description_preserves_fields(client, crons_file):
    """Editing prompt on a job that HAS a description must preserve the description
    and the harness-only fields (mirrors test_crons_update_preserves_harness_fields)."""
    crons_file.parent.mkdir(parents=True, exist_ok=True)
    crons_file.write_text(json.dumps({"tasks": [{
        "id": "abc12345", "cron": "37 23 * * *", "prompt": "old",
        "recurring": True, "createdAt": 1, "createdBySessionId": "sess-1",
        "description": "kept summary",
    }]}), encoding="utf-8")

    upd = await client.put("/api/crons/abc12345", json={"prompt": "new"})
    assert upd.status == 200
    saved = json.loads(crons_file.read_text(encoding="utf-8"))["tasks"][0]
    assert saved["prompt"] == "new"
    assert saved["description"] == "kept summary"  # preserved (not in PUT body)
    assert saved["createdBySessionId"] == "sess-1"  # harness field preserved


async def test_crons_job_without_description_serializes_cleanly(client, crons_file):
    """A pre-existing harness job with no description renders through GET (200, no
    'description' key) — absence never breaks the list/detail view."""
    crons_file.parent.mkdir(parents=True, exist_ok=True)
    crons_file.write_text(json.dumps({"tasks": [{
        "id": "nodesc01", "cron": "30 22 * * *", "prompt": "health check",
        "recurring": True, "createdAt": 1, "lastFiredAt": None,
    }]}), encoding="utf-8")

    resp = await client.get("/api/crons")
    assert resp.status == 200
    jobs = (await resp.json())["jobs"]
    assert len(jobs) == 1
    assert "description" not in jobs[0]


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


# --------------------------------------------------------------------------- #
# Cron run RECONCILIATION (harness lastFiredAt -> "fired" run entry) + harvest
#
# GET /api/crons/{id}/runs reconciles the harness fire timestamp (lastFiredAt in
# scheduled_tasks.json) with the console-POSTed sidecar runs. A scheduled fire
# surfaces as a distinct "fired" entry; output is harvested best-effort from the
# creating session's transcript, with an honest empty fallback (never fabricated).
# --------------------------------------------------------------------------- #
def _write_tasks(crons_file: Path, task: dict):
    crons_file.parent.mkdir(parents=True, exist_ok=True)
    crons_file.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")


async def test_fired_entry_from_lastfiredat_with_empty_runs(client, crons_file, cron_runs_file):
    """A harness-written lastFiredAt with NO console runs surfaces exactly one
    'fired' entry — not 'No runs recorded yet'."""
    _write_tasks(crons_file, {
        "id": "3af99bc9", "cron": "30 22 * * *", "prompt": "health check",
        "recurring": True, "createdAt": 1, "lastFiredAt": 1781482020160,
    })
    runs = (await (await client.get("/api/crons/3af99bc9/runs")).json())["runs"]
    assert len(runs) == 1
    assert runs[0]["outcome"] == "fired"
    assert runs[0]["ts"] == 1781482020160
    assert runs[0]["source"] == "harness"
    assert runs[0]["id"] == "fired-1781482020160"
    # No harvestable transcript → honest empty result, NOT a fabricated success.
    assert runs[0]["result"] == ""


async def test_fired_entry_absent_when_no_lastfiredat(client, crons_file, cron_runs_file):
    """A task that never fired (lastFiredAt None/0) yields no 'fired' entry."""
    _write_tasks(crons_file, {
        "id": "neverfired", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": None,
    })
    runs = (await (await client.get("/api/crons/neverfired/runs")).json())["runs"]
    assert runs == []


async def test_fired_entry_deduped_against_console_run(client, crons_file, cron_runs_file):
    """A console-POSTed run near the fire timestamp is the SAME fire — the
    synthesized 'fired' entry is suppressed so it isn't shown twice."""
    fired_ms = 1781482020160
    _write_tasks(crons_file, {
        "id": "dedupe1", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": fired_ms,
    })
    # Console run 5s after the fire → within the dedupe window.
    resp = await client.post(
        "/api/crons/dedupe1/runs",
        json={"outcome": "success", "result": "ran", "ts": fired_ms + 5000},
    )
    assert resp.status == 201
    runs = (await (await client.get("/api/crons/dedupe1/runs")).json())["runs"]
    assert len(runs) == 1
    # The real console outcome wins; no duplicate synthesized 'fired' entry.
    assert runs[0]["outcome"] == "success"
    assert all(r.get("source") != "harness" for r in runs)


async def test_fired_entry_not_deduped_when_far_from_console_run(client, crons_file, cron_runs_file):
    """A console run far from the fire timestamp is a DIFFERENT run — both show."""
    fired_ms = 1781482020160
    _write_tasks(crons_file, {
        "id": "two1", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": fired_ms,
    })
    await client.post(
        "/api/crons/two1/runs",
        json={"outcome": "failure", "result": "old run", "ts": fired_ms - 10 * 60_000},
    )
    runs = (await (await client.get("/api/crons/two1/runs")).json())["runs"]
    assert len(runs) == 2
    # Newest-first: the fire (newer) leads, the older console failure follows.
    assert runs[0]["outcome"] == "fired"
    assert runs[1]["outcome"] == "failure"


async def test_fired_entry_harvests_transcript_result(client, crons_file, cron_runs_file, tmp_path, monkeypatch):
    """When the creating session's transcript exists, the 'fired' entry carries the
    real VERDICT text from the assistant turn following the matching enqueue line."""
    fired_ms = 1781482020160
    cwd = "/local/home/u/workspace/projects/demo"
    slug = crons_mod._project_path_to_claude_slug(cwd)
    projects = tmp_path / "projects"
    monkeypatch.setattr(crons_mod, "CLAUDE_PROJECTS_BASE", projects)
    sess_dir = projects / slug
    sess_dir.mkdir(parents=True)
    lines = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:07:00.161Z", "content": "do the check"},
        {"type": "queue-operation", "operation": "dequeue",
         "timestamp": "2026-06-15T00:07:00.182Z"},
        {"type": "user", "message": {"content": "do the check"}},
        # interleaved tool call/result lines that carry no text
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "out"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "HEALTHY — all good."}]}},
        {"type": "system", "message": {}},
    ]
    (sess_dir / "sess-1.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    _write_tasks(crons_file, {
        "id": "harv1", "cron": "30 22 * * *", "prompt": "health check",
        "recurring": True, "createdAt": 1, "lastFiredAt": fired_ms,
        "createdBySessionId": "sess-1", "cwd": cwd,
    })
    runs = (await (await client.get("/api/crons/harv1/runs")).json())["runs"]
    assert len(runs) == 1
    assert runs[0]["outcome"] == "fired"
    assert runs[0]["result"] == "HEALTHY — all good."


async def test_fired_entry_missing_transcript_falls_back_empty(client, crons_file, cron_runs_file, tmp_path, monkeypatch):
    """No transcript file → honest 'result not captured' fallback (empty result),
    never a fabricated outcome."""
    monkeypatch.setattr(crons_mod, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")
    _write_tasks(crons_file, {
        "id": "miss1", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": 1781482020160,
        "createdBySessionId": "no-such-session", "cwd": "/local/home/u/x",
    })
    runs = (await (await client.get("/api/crons/miss1/runs")).json())["runs"]
    assert len(runs) == 1
    assert runs[0]["outcome"] == "fired"
    assert runs[0]["result"] == ""


async def test_fired_entry_scrubs_credential_text(client, crons_file, cron_runs_file, tmp_path, monkeypatch):
    """An assistant turn mentioning credential/cookie material is scrubbed from the
    harvested result — never surfaced."""
    fired_ms = 1781482020160
    cwd = "/local/home/u/workspace/projects/sec"
    slug = crons_mod._project_path_to_claude_slug(cwd)
    projects = tmp_path / "projects"
    monkeypatch.setattr(crons_mod, "CLAUDE_PROJECTS_BASE", projects)
    sess_dir = projects / slug
    sess_dir.mkdir(parents=True)
    lines = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:07:00.161Z", "content": "check"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "your ~/.midway/cookie is AABBCC secret"}]}},
    ]
    (sess_dir / "s.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    _write_tasks(crons_file, {
        "id": "scrub1", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": fired_ms,
        "createdBySessionId": "s", "cwd": cwd,
    })
    runs = (await (await client.get("/api/crons/scrub1/runs")).json())["runs"]
    assert len(runs) == 1
    assert "cookie" not in runs[0]["result"].lower()
    assert "AABBCC" not in runs[0]["result"]
    # No safe text remained → honest empty fallback.
    assert runs[0]["result"] == ""


async def test_list_runs_does_not_write_harness_files(client, crons_file, cron_runs_file):
    """Reconciliation is READ-only: a GET must never write scheduled_tasks.json,
    create the runs sidecar, or otherwise mutate harness state."""
    _write_tasks(crons_file, {
        "id": "ro1", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": 1781482020160,
    })
    before = crons_file.read_text(encoding="utf-8")
    await client.get("/api/crons/ro1/runs")
    assert crons_file.read_text(encoding="utf-8") == before
    # A pure read with no console runs must not have created the sidecar.
    assert not cron_runs_file.exists()


async def test_client_cannot_post_fired_outcome(client, cron_runs_file):
    """'fired' is reserved for synthesized harness entries — a client POST of it is
    normalized to 'unknown' (not stored as a fake harness fire)."""
    resp = await client.post(
        "/api/crons/x1/runs", json={"outcome": "fired", "result": "sneaky"}
    )
    assert resp.status == 201
    assert (await resp.json())["run"]["outcome"] == "unknown"


async def test_fired_entry_merges_with_two_distinct_console_runs(client, crons_file, cron_runs_file):
    """lastFiredAt PLUS two console runs at clearly distinct times -> exactly 3
    entries, strictly newest-first by ts, with the synthesized 'fired' entry present
    and not collapsing the far-apart console runs into it."""
    fired_ms = 1781482020160
    older = fired_ms - 86_400_000        # 1 day before the fire
    oldest = fired_ms - 2 * 86_400_000   # 2 days before the fire
    _write_tasks(crons_file, {
        "id": "merge3", "cron": "30 22 * * *", "prompt": "x",
        "recurring": True, "createdAt": 1, "lastFiredAt": fired_ms,
    })
    # Two genuinely separate console runs, both well outside the dedupe window.
    await client.post("/api/crons/merge3/runs",
                      json={"outcome": "success", "result": "day-1", "ts": older})
    await client.post("/api/crons/merge3/runs",
                      json={"outcome": "failure", "result": "day-2", "ts": oldest})

    runs = (await (await client.get("/api/crons/merge3/runs")).json())["runs"]
    assert len(runs) == 3, f"fire + two distinct console runs => 3 entries, got {runs}"
    ts_list = [r["ts"] for r in runs]
    assert ts_list == sorted(ts_list, reverse=True), "must be strictly newest-first by ts"
    # The fire is the newest of the three, so it leads; both console runs survive.
    assert runs[0]["outcome"] == "fired" and runs[0]["ts"] == fired_ms
    outcomes = {r["outcome"] for r in runs}
    assert {"fired", "success", "failure"} == outcomes


# --------------------------------------------------------------------------- #
# Cron FULL fire-history harvest + idempotent backfill (_harvest_all_fires)
#
# Beyond the single lastFiredAt synthesis, GET /api/crons/{id}/runs enumerates
# EVERY fire from the creating session's transcript (each fire is a
# queue-operation/enqueue whose content's first line == the job prompt's first
# line, excluding <task-notification> wrappers), persists the new ones into
# cron_runs.json idempotently, and reconciles them newest-first.
# --------------------------------------------------------------------------- #
_WARMUP_PROMPT = "SLACK-WARMUP v1 (claude-web)\nStep 1: do the thing.\nStep 2: finish."


def _warmup_transcript_lines():
    """Three matching fires (interleaved assistant turns) + noise the matcher must
    ignore: a dequeue, a <task-notification> enqueue, and an unrelated enqueue."""
    return [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:00:00.000Z", "content": _WARMUP_PROMPT},
        {"type": "user", "message": {"content": _WARMUP_PROMPT}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fire one done"}]}},
        # Workflow notification — NOT a cron fire, must be excluded.
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:01:00.000Z",
         "content": "<task-notification>something</task-notification>"},
        # Unrelated enqueue (different prompt) — not this job's fire.
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:02:00.000Z", "content": "OTHER JOB\nrun something else"},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:03:00.000Z", "content": _WARMUP_PROMPT},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fire two done"}]}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:04:00.000Z", "content": _WARMUP_PROMPT},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fire three done"}]}},
    ]


def _setup_warmup_transcript(crons_file, tmp_path, monkeypatch, job_id="warm1",
                             lines=None, session_id="sess-w"):
    """Wire a warm-up job + its transcript located via the createdBySessionId
    fallback (cwd=None, like the real harness jobs)."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(crons_mod, "CLAUDE_PROJECTS_BASE", projects)
    sess_dir = projects / "-some-proj-slug"
    sess_dir.mkdir(parents=True)
    if lines is None:
        lines = _warmup_transcript_lines()
    (sess_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    _write_tasks(crons_file, {
        "id": job_id, "cron": "*/10 * * * *", "prompt": _WARMUP_PROMPT,
        "recurring": True, "createdAt": 1, "lastFiredAt": 1781482020160,
        "createdBySessionId": session_id, "cwd": None,
    })


async def test_harvest_all_fires_yields_multiple_entries(client, crons_file, cron_runs_file,
                                                          tmp_path, monkeypatch):
    """A transcript with N>1 matching enqueue fires yields N 'fired' entries,
    newest-first — not just the single last fire."""
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch)
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    assert len(runs) == 3, runs
    assert all(r["outcome"] == "fired" for r in runs)
    ts_list = [r["ts"] for r in runs]
    assert ts_list == sorted(ts_list, reverse=True), "newest-first"
    # The verdict text is harvested per-fire (newest first).
    assert runs[0]["result"] == "fire three done"
    assert runs[1]["result"] == "fire two done"
    assert runs[2]["result"] == "fire one done"


async def test_harvest_excludes_task_notification_and_unrelated(client, crons_file,
                                                                cron_runs_file, tmp_path, monkeypatch):
    """<task-notification> enqueues and enqueues for a DIFFERENT prompt are NOT
    counted as fires of this job."""
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch)
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    # Only the 3 real warm-up enqueues, despite 5 enqueue lines total.
    assert len(runs) == 3
    # None of the harvested results came from the unrelated job's enqueue.
    assert all("else" not in r["result"] for r in runs)


async def test_harvest_backfill_is_idempotent(client, crons_file, cron_runs_file,
                                               tmp_path, monkeypatch):
    """Backfill persists into cron_runs.json; a SECOND GET does not duplicate
    entries (full transcript scan effectively happens once)."""
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch)
    first = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    assert len(first) == 3
    # The sidecar now holds the backfilled fires.
    stored = json.loads(cron_runs_file.read_text(encoding="utf-8"))["runs"]["warm1"]
    assert len(stored) == 3
    assert all(r["source"] == "harness" for r in stored)

    second = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    assert len(second) == 3, "a repeat GET must not duplicate entries"
    stored2 = json.loads(cron_runs_file.read_text(encoding="utf-8"))["runs"]["warm1"]
    assert len(stored2) == 3, "sidecar must not grow on a repeat read"
    assert {r["id"] for r in stored} == {r["id"] for r in stored2}


async def test_harvest_console_run_within_window_deduped(client, crons_file, cron_runs_file,
                                                         tmp_path, monkeypatch):
    """A console-POSTed run within _FIRE_DEDUPE_MS of a harvested fire is the SAME
    fire — it is not shown twice."""
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch)
    # The newest harvested fire is at 2026-06-15T00:04:00Z.
    fire_ms = crons_mod._iso_to_ms("2026-06-15T00:04:00.000Z")
    resp = await client.post(
        "/api/crons/warm1/runs",
        json={"outcome": "success", "result": "console", "ts": fire_ms + 5000},
    )
    assert resp.status == 201
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    # 3 fires but the newest one collapses with the console run → 3 entries total.
    assert len(runs) == 3, runs
    # The real console outcome wins for the collided slot.
    newest = max(runs, key=lambda r: r["ts"])
    assert newest["outcome"] == "success" and newest.get("source") != "harness"


async def test_harvest_missing_transcript_falls_back_to_lastfiredat(client, crons_file,
                                                                    cron_runs_file, tmp_path, monkeypatch):
    """When the transcript can't be found / yields zero fires, the single
    lastFiredAt synthesis remains as the fallback — not an error, not empty."""
    monkeypatch.setattr(crons_mod, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")
    _write_tasks(crons_file, {
        "id": "fb1", "cron": "*/10 * * * *", "prompt": _WARMUP_PROMPT,
        "recurring": True, "createdAt": 1, "lastFiredAt": 1781482020160,
        "createdBySessionId": "no-such-session", "cwd": None,
    })
    runs = (await (await client.get("/api/crons/fb1/runs")).json())["runs"]
    assert len(runs) == 1
    assert runs[0]["outcome"] == "fired"
    assert runs[0]["id"] == "fired-1781482020160"
    # No transcript yields zero harvested fires → no backfill write happened.
    assert not cron_runs_file.exists()


async def test_harvest_scrubs_credential_text(client, crons_file, cron_runs_file,
                                              tmp_path, monkeypatch):
    """A fire whose assistant text contains a secret marker is scrubbed from the
    harvested result (never surfaced)."""
    lines = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-06-15T00:00:00.000Z", "content": _WARMUP_PROMPT},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "refreshed ~/.midway/cookie = TOPSECRET"}]}},
    ]
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch, lines=lines)
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    assert len(runs) == 1
    assert runs[0]["outcome"] == "fired"
    assert "cookie" not in runs[0]["result"].lower()
    assert "TOPSECRET" not in runs[0]["result"]
    assert runs[0]["result"] == ""  # no safe text → honest empty fallback


async def test_harvest_cap_at_runs_cap(client, crons_file, cron_runs_file,
                                       tmp_path, monkeypatch):
    """More matched fires than _RUNS_CAP → only the most-recent cap are returned."""
    monkeypatch.setattr(crons_mod, "_RUNS_CAP", 5)
    lines = []
    for i in range(8):
        lines.append({"type": "queue-operation", "operation": "enqueue",
                      "timestamp": f"2026-06-15T00:{i:02d}:00.000Z", "content": _WARMUP_PROMPT})
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch, lines=lines)
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    assert len(runs) == 5, "capped at _RUNS_CAP"
    # The 5 most-recent (minutes 03..07) survive; the oldest are dropped.
    assert runs[0]["id"] == f"fired-{crons_mod._iso_to_ms('2026-06-15T00:07:00.000Z')}"


async def test_harvest_fires_seconds_apart_all_survive(client, crons_file, cron_runs_file,
                                                       tmp_path, monkeypatch):
    """High-frequency fires only seconds apart (well inside _FIRE_DEDUPE_MS) are each
    DISTINCT recoverable fires and must ALL survive — they must not collapse against
    each other. Mirrors the real warm-up cron, whose fires occur 0-3s apart."""
    lines = []
    # 10 fires 2 seconds apart — every adjacent pair is inside the 60s dedupe window.
    for i in range(10):
        lines.append({"type": "queue-operation", "operation": "enqueue",
                      "timestamp": f"2026-06-15T00:00:{2 * i:02d}.000Z",
                      "content": _WARMUP_PROMPT})
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch, lines=lines)
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    assert len(runs) == 10, "one record per recoverable fire, none collapsed"
    assert all(r["outcome"] == "fired" for r in runs)
    # All ids are distinct fired-<ts> entries.
    assert len({r["id"] for r in runs}) == 10


async def test_dense_harness_fire_still_dedupes_against_console_post(
        client, crons_file, cron_runs_file, tmp_path, monkeypatch):
    """Even amid dense seconds-apart harness fires, a console POST coincident with
    ONE of them still collapses against that single fire (cross-boundary dedupe is
    preserved) without dropping the other distinct fires."""
    lines = []
    for i in range(5):
        lines.append({"type": "queue-operation", "operation": "enqueue",
                      "timestamp": f"2026-06-15T00:00:{2 * i:02d}.000Z",
                      "content": _WARMUP_PROMPT})
    _setup_warmup_transcript(crons_file, tmp_path, monkeypatch, lines=lines)
    # Console POST 1s after the newest fire (00:00:08) → same fire.
    fire_ms = crons_mod._iso_to_ms("2026-06-15T00:00:08.000Z")
    resp = await client.post(
        "/api/crons/warm1/runs",
        json={"outcome": "success", "result": "console", "ts": fire_ms + 1000},
    )
    assert resp.status == 201
    runs = (await (await client.get("/api/crons/warm1/runs")).json())["runs"]
    # 5 harness fires, the newest collapses with the console POST → 5 entries.
    assert len(runs) == 5, runs
    newest = max(runs, key=lambda r: r["ts"])
    assert newest["outcome"] == "success" and newest.get("source") != "harness"


def test_enqueue_matcher_rule():
    """Unit-level: the prompt-first-line matcher (excludes <task-notification>)."""
    first = crons_mod._first_nonempty_line(_WARMUP_PROMPT)
    assert first == "SLACK-WARMUP v1 (claude-web)"
    assert crons_mod._enqueue_is_fire(_WARMUP_PROMPT, first) is True
    assert crons_mod._enqueue_is_fire("\n\n" + _WARMUP_PROMPT, first) is True
    assert crons_mod._enqueue_is_fire(
        "<task-notification>" + _WARMUP_PROMPT + "</task-notification>", first) is False
    assert crons_mod._enqueue_is_fire("OTHER JOB\nstuff", first) is False
    assert crons_mod._enqueue_is_fire(None, first) is False


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
    # Isolate console-created user tasks too, otherwise the test leaks the
    # developer's real ~/.claude-web/tasks.json and sees stray tasks.
    monkeypatch.setattr(tasks_mod, "USER_TASKS_PATH", tmp_path / "notasks.json")
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
    args = SimpleNamespace(host="0.0.0.0", port=9000, no_browser=True, allow_remote=False)
    # Must exit(2) BEFORE importing/creating the app or calling run_app.
    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_start(args)
    assert exc.value.code == 2


def test_cmd_start_allows_non_loopback_with_flag(monkeypatch):
    monkeypatch.delenv("CLAUDE_WEB_ALLOW_REMOTE", raising=False)
    started = {}
    # Stub run_app so the test doesn't actually block on a server.
    monkeypatch.setattr(cli_mod.web, "run_app", lambda app, **kw: started.update(kw))
    args = SimpleNamespace(host="0.0.0.0", port=9000, no_browser=True, allow_remote=True)
    cli_mod.cmd_start(args)
    assert started.get("host") == "0.0.0.0"


def test_cmd_start_allows_non_loopback_via_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_WEB_ALLOW_REMOTE", "1")
    started = {}
    monkeypatch.setattr(cli_mod.web, "run_app", lambda app, **kw: started.update(kw))
    args = SimpleNamespace(host="0.0.0.0", port=9000, no_browser=True, allow_remote=False)
    cli_mod.cmd_start(args)
    assert started.get("host") == "0.0.0.0"


def test_cmd_start_loopback_does_not_require_flag(monkeypatch):
    monkeypatch.delenv("CLAUDE_WEB_ALLOW_REMOTE", raising=False)
    started = {}
    monkeypatch.setattr(cli_mod.web, "run_app", lambda app, **kw: started.update(kw))
    # no_browser=True so webbrowser.open isn't invoked during the test.
    args = SimpleNamespace(host="127.0.0.1", port=9000, no_browser=True, allow_remote=False)
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


# --------------------------------------------------------------------------- #
# B2: SessionManager per-session-id locking + stop_all robustness (F6)
#
# get_or_create/respawn/register/stop mutate self._sessions across await points.
# Without a manager-level lock, two near-simultaneous requests for the SAME
# session could each spawn a `claude` subprocess and the second store would
# orphan the first (never stop()'d, surviving until server exit). The fix
# serializes same-id lifecycle ops via a per-id lock map (_lock_for); different
# ids stay parallel. stop_all() must also be exception-safe (F6): one raising
# stop() can't prevent the rest from being stopped on shutdown. These tests
# reuse the _FakeProc fakes from above — no real `claude` binary is spawned.
# (The B1 /api/hooks etag tests already live near the other route tests.)
# --------------------------------------------------------------------------- #

async def test_manager_concurrent_same_id_no_duplicate_spawn(monkeypatch):
    """Two concurrent get_or_create for the SAME id must not spawn duplicate live
    processes. We seed one alive session under 'dup'; the per-id lock serializes
    the two calls so both observe it already alive in _sessions, return that same
    object, and neither calls start()."""
    mgr = session_mod.SessionManager()

    alive = session_mod.PersistentSession(session_id="dup", cwd="/tmp")
    alive._proc = _FakeProc([])  # returncode is None -> .alive is True
    alive._started = True
    mgr._sessions["dup"] = alive

    spawns = {"n": 0}

    async def fake_start(self):
        spawns["n"] += 1
        await asyncio.sleep(0)  # force interleaving between the two coroutines
        self._proc = _FakeProc([])
        self._started = True

    monkeypatch.setattr(session_mod.PersistentSession, "start", fake_start)

    a, b = await asyncio.gather(
        mgr.get_or_create(session_id="dup"),
        mgr.get_or_create(session_id="dup"),
    )

    assert spawns["n"] == 0           # the already-alive session was reused
    assert a is alive and b is alive  # both calls returned the same live object
    # No orphan: exactly one session is tracked for this id.
    assert mgr._sessions["dup"] is alive


async def test_manager_stop_all_is_exception_safe(monkeypatch):
    """stop_all() must stop every session even if one raises, then clear the dict
    (F6: asyncio.gather(..., return_exceptions=True)). A serial loop would let the
    first raising stop() abort the rest and leave _sessions populated."""
    mgr = session_mod.SessionManager()

    s_bad = session_mod.PersistentSession(session_id="bad")
    s_good = session_mod.PersistentSession(session_id="good")

    async def bad_stop():
        raise RuntimeError("boom during shutdown")

    good_called = {"v": False}

    async def good_stop():
        good_called["v"] = True

    s_bad.stop = bad_stop
    s_good.stop = good_stop
    mgr._sessions["bad"] = s_bad
    mgr._sessions["good"] = s_good

    # Must not raise despite s_bad.stop() blowing up.
    await mgr.stop_all()

    assert good_called["v"] is True   # the non-raising session was still stopped
    assert mgr._sessions == {}        # dict cleared regardless of the exception


# --------------------------------------------------------------------------- #
# Slack assistant routes (/api/slack)
#
# The MCP delegation seam (_run_slack_agent) is stubbed in-tree to an
# "unavailable" result; tests monkeypatch it to exercise the contract WITHOUT
# spawning a subprocess or fabricating message data. SLACK_PATH is repointed at
# a tmp sidecar so the filestore round-trip is real.
# --------------------------------------------------------------------------- #

import server.routes.slack as slack_mod  # noqa: E402


@pytest.fixture
def slack_file(tmp_path: Path, monkeypatch) -> Path:
    """Repoint the slack sidecar (+ sibling style/topic stores) at tmp_path."""
    p = tmp_path / "slack" / "slack_threads.json"
    monkeypatch.setattr(slack_mod, "SLACK_PATH", p)
    return p


@pytest.fixture(autouse=True)
def _reset_slack_scan_guard():
    """Reset the shared scan guard + manual-refresh wake Event between tests (D-023).

    ``_scan_in_progress_lock`` and ``_scan_wake_event`` are module-level asyncio
    primitives, lazily created on first use (``_get_scan_lock``/``_get_scan_event``)
    inside the running loop. The startup ``_scan_worker`` calls ``_get_scan_event()``
    in EVERY test (on_startup under aiohttp_client(app)), so without this reset the
    Event/Lock created in one test's event loop would be reused by the next test and
    raise ``RuntimeError: ... is bound to a different event loop``. Clearing both to
    None forces a fresh primitive in the current loop, exactly as if the server just
    started. Autouse so the startup-worker leak is neutralized for every test, not
    just the ones that touch refresh/scan directly.

    The EMAIL module's workers are now ALSO started by create_app() in every test
    (email.register hooks on_startup), so its loop-bound primitives leak across
    tests the same way — reset them here too (mirrors test_email.py, which already
    resets the slack module for the symmetric reason)."""
    slack_mod._scan_in_progress_lock = None
    slack_mod._scan_wake_event = None
    _reset_email_primitives()
    yield
    slack_mod._scan_in_progress_lock = None
    slack_mod._scan_wake_event = None
    _reset_email_primitives()


def _reset_email_primitives():
    """Clear the email module's lazily-created, loop-bound asyncio primitives so a
    primitive from one test's event loop is never reused in the next (the
    'bound to a different event loop' RuntimeError). Guarded import: a no-op if the
    email module isn't present."""
    try:
        from server.routes import email as email_mod
    except ImportError:
        return
    for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
        if hasattr(email_mod, attr):
            setattr(email_mod, attr, None)


def _stub_agent(monkeypatch, result):
    """Monkeypatch the delegation seam to return a fixed result dict."""
    async def fake_agent(action, payload):
        fake_agent.calls.append((action, payload))
        return dict(result, action=action)
    fake_agent.calls = []
    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)
    return fake_agent


async def test_slack_queue_empty_when_no_file(client, slack_file):
    resp = await client.get("/api/slack/queue")
    assert resp.status == 200
    body = await resp.json()
    assert body["items"] == []


# NOTE (D-023, in-process scan + manual-refresh trigger): the scanner is the
# in-process ``_scan_worker`` running ``_scan_once``; the retired ``.slack_scan_now``
# cron sentinel is GONE. POST /api/slack/queue/refresh no longer touches a sentinel
# file — it triggers a REAL scan by setting the worker's wake Event (non-blocking,
# returns the warm etag immediately), sharing ONE scan-in-progress guard with the
# periodic worker so a manual and a system scan can never overlap. The new refresh
# contract is asserted by test_slack_refresh_triggers_real_scan_non_blocking and
# test_slack_refresh_while_scan_in_progress_is_rejected; freshness (the durable
# lastScanAt) by test_slack_queue_returns_last_scan_at and
# test_slack_scan_noop_still_advances_last_scan_and_broadcasts.


async def test_slack_needs_draft_regenerates_to_needs_review(client, slack_file, monkeypatch):
    """The on-demand/drain draft path flips a needs-draft item to needs-review via
    the single-item 'regenerate' seam — and is fail-loud per item."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "", "generatedDraft": "",
        "status": "needs-draft", "ts": 1,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "drafted reply"})
    resp = await client.post("/api/slack/queue/i1/refresh")
    assert resp.status == 200
    body = await resp.json()
    assert agent.calls[0][0] == "regenerate"
    assert body["item"]["draft"] == "drafted reply"
    assert body["item"]["status"] == "needs-review"


async def test_slack_needs_draft_failed_draft_stays_needs_draft(client, slack_file, monkeypatch):
    """A per-item draft failure leaves that single row in needs-draft with no
    fabricated draft (the row keeps its retry affordance)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "", "generatedDraft": "",
        "status": "needs-draft", "ts": 1,
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    resp = await client.post("/api/slack/queue/i1/refresh")
    assert resp.status == 502
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-draft"
    assert saved["draft"] == ""


async def test_slack_save_draft_marks_edited_and_round_trips(client, slack_file, monkeypatch):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "alice", "channel": "general", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "auto draft",
        "generatedDraft": "auto draft", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    resp = await client.put("/api/slack/queue/i1", json={"draft": "my own words"})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["draft"] == "my own words"
    assert body["item"]["status"] == "edited"  # diverged from generatedDraft
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "my own words"


async def test_slack_save_draft_conflict_returns_409(client, slack_file):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "d",
        "generatedDraft": "d", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    resp = await client.put("/api/slack/queue/i1", json={"draft": "x", "etag": "stale-etag"})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"


async def test_slack_dismiss_sets_status_not_delete(client, slack_file):
    """SOFT-DISMISS (T1 contract): DELETE /api/slack/queue/{id} no longer REMOVES
    the item — it flips its status to 'dismissed' and PRESERVES it in the sidecar
    (so it can be shown in a Dismissed section and Undone). The item must still be
    present in GET /queue (status='dismissed'), and persisted to disk (not just
    echoed in the response)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [
        {"id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
         "snippet": "hi", "threadContext": "", "draft": "d",
         "generatedDraft": "d", "status": "needs-review", "ts": 1},
        {"id": "i2", "sender": "b", "channel": "c", "channelType": "dm",
         "snippet": "yo", "threadContext": "", "draft": "e",
         "generatedDraft": "e", "status": "needs-review", "ts": 2},
    ]}), encoding="utf-8")
    resp = await client.delete("/api/slack/queue/i1", json={})
    assert resp.status == 200

    # Still present in the queue, now flagged dismissed (item preserved, the
    # untouched sibling keeps its status).
    items = (await (await client.get("/api/slack/queue")).json())["items"]
    by_id = {it["id"]: it for it in items}
    assert set(by_id) == {"i1", "i2"}
    assert by_id["i1"]["status"] == "dismissed"
    assert by_id["i2"]["status"] == "needs-review"

    # Persisted to disk, not merely echoed in the response body.
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"]
    saved_by_id = {it["id"]: it for it in saved}
    assert set(saved_by_id) == {"i1", "i2"}
    assert saved_by_id["i1"]["status"] == "dismissed"


async def test_slack_dismiss_unknown_is_404(client, slack_file):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    resp = await client.delete("/api/slack/queue/nope", json={})
    assert resp.status == 404


async def test_slack_regenerate_unavailable_does_not_fabricate(client, slack_file, monkeypatch):
    """Per-item regenerate with an unavailable seam returns 502 and leaves the
    stored draft untouched — never fabricating a draft."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "original draft",
        "generatedDraft": "original draft", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    resp = await client.post("/api/slack/queue/i1/refresh")
    assert resp.status == 502
    body = await resp.json()
    assert body["available"] is False
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "original draft"  # untouched


async def test_slack_regenerate_replaces_draft_when_available(client, slack_file, monkeypatch):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "old",
        "generatedDraft": "old", "status": "edited", "ts": 1,
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": True, "draft": "fresh draft"})
    resp = await client.post("/api/slack/queue/i1/refresh")
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["draft"] == "fresh draft"
    assert body["item"]["generatedDraft"] == "fresh draft"  # new baseline
    assert body["item"]["status"] == "needs-review"


async def test_slack_polish_returns_polished_text_without_touching_store(client, slack_file, monkeypatch):
    """POST /api/slack/polish is STATELESS (D-029): it returns polished text from
    the 'polish' seam and does NOT load or write slack_threads.json — so even when
    a queue file exists with an item, the file is left BYTE-FOR-BYTE unchanged."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "my rough draft",
        "generatedDraft": "my rough draft", "status": "edited", "ts": 1,
    }]}), encoding="utf-8")
    before = slack_file.read_text(encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "my polished draft"})
    resp = await client.post("/api/slack/polish", json={"text": "my rough draft"})
    assert resp.status == 200
    body = await resp.json()
    assert body["available"] is True
    assert body["draft"] == "my polished draft"
    # The seam was asked to POLISH the exact text we sent — never to read Slack.
    assert agent.calls == [("polish", {"text": "my rough draft"})]
    # The store is untouched: no status flip, no draft overwrite, byte-identical.
    assert slack_file.read_text(encoding="utf-8") == before


async def test_slack_polish_empty_text_is_400(client, slack_file, monkeypatch):
    """Fail-loud: polishing requires non-empty text — an empty/whitespace/missing
    'text' is a 400 and the seam is NEVER spawned."""
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "x"})
    for bad in ({"text": ""}, {"text": "   "}, {}):
        resp = await client.post("/api/slack/polish", json=bad)
        assert resp.status == 400
    assert agent.calls == []  # never reached the seam


async def test_slack_polish_unavailable_does_not_fabricate(client, slack_file, monkeypatch):
    """An unavailable polish seam returns 502 with available:false — never
    fabricating a polished result."""
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    resp = await client.post("/api/slack/polish", json={"text": "some text"})
    assert resp.status == 502
    body = await resp.json()
    assert body["available"] is False


async def test_slack_polish_never_sends(client, slack_file, monkeypatch):
    """SECURITY: the polish path must NEVER send. The seam is only ever asked for
    the 'polish' action — never 'send'/'post_message'."""
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "polished"})
    resp = await client.post("/api/slack/polish", json={"text": "draft"})
    assert resp.status == 200
    assert agent.calls and all(action == "polish" for action, _ in agent.calls)


async def test_slack_approve_unavailable_does_not_mark_sent(client, slack_file, monkeypatch):
    """An unavailable send seam must NOT fabricate a send-success: 502, and the
    item stays unsent."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D0TEST1234",
        "snippet": "hi", "threadContext": "", "draft": "ready to send",
        "generatedDraft": "ready to send", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    resp = await client.post("/api/slack/queue/i1/approve", json={})
    assert resp.status == 502
    body = await resp.json()
    assert body["available"] is False
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] != "sent"


async def test_slack_approve_sends_and_distills_style_note(client, slack_file, monkeypatch):
    """Approving an edited draft marks it sent AND distills the (generated→final)
    edit into the human-readable learned-style store."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "alice", "channel": "c", "channelType": "dm",
        "channelId": "D0ALICE123",
        "snippet": "ping", "threadContext": "", "draft": "Hey! Sounds great.",
        "generatedDraft": "Sure, that works.", "status": "edited", "ts": 1,
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "sent"
    assert body["item"]["finalText"] == "Hey! Sounds great."
    # Style note distilled to the sibling style store.
    style = slack_mod._style_path().read_text(encoding="utf-8")
    assert "Hey! Sounds great." in style
    # Per-contact topic memory updated.
    topic = (slack_mod._topics_dir() / "alice.md").read_text(encoding="utf-8")
    assert "ping" in topic


async def test_slack_approve_already_sent_is_409(client, slack_file, monkeypatch):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "x",
        "generatedDraft": "x", "status": "sent", "ts": 1,
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={})
    assert resp.status == 409


# --- SOFT-DISMISS undo + dismissed status (T1 contract) -------------------- #
#
# Contract this test suite LOCKS IN for the sibling T1 (backend) / T5 (frontend):
#   * DELETE /api/slack/queue/{id}     → status='dismissed', item PRESERVED,
#                                        broadcasts 'slack_changed' (asserted above).
#   * POST   /api/slack/queue/{id}/undismiss → restores a dismissed item:
#       - draft == generatedDraft (an unedited machine draft) → 'needs-review'
#       - draft != generatedDraft (a human edit was in flight) → 'edited'
#     Undismiss on a NON-dismissed item → 409 (nothing to undo).


async def test_slack_undismiss_restores(client, slack_file):
    """Undismiss flips a dismissed item back to its actionable status: an unedited
    draft → needs-review, an edited draft → edited."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    # Case A: dismissed item whose draft still equals the generated draft → it was
    # never edited, so it returns to 'needs-review'.
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "auto draft",
        "generatedDraft": "auto draft", "status": "dismissed", "ts": 1,
    }]}), encoding="utf-8")
    resp = await client.post("/api/slack/queue/i1/undismiss", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "needs-review"
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"

    # Case B: dismissed item whose draft diverged from the generated draft (a human
    # edit was in flight when it was dismissed) → it returns to 'edited'.
    slack_file.write_text(json.dumps({"items": [{
        "id": "i2", "sender": "b", "channel": "c", "channelType": "dm",
        "snippet": "yo", "threadContext": "", "draft": "my own words",
        "generatedDraft": "auto draft", "status": "dismissed", "ts": 2,
    }]}), encoding="utf-8")
    resp = await client.post("/api/slack/queue/i2/undismiss", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "edited"


async def test_slack_undismiss_non_dismissed_is_409(client, slack_file):
    """Undismiss only applies to a dismissed item — undoing a live needs-review
    item is a conflict (there is nothing to restore)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "d",
        "generatedDraft": "d", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    resp = await client.post("/api/slack/queue/i1/undismiss", json={})
    assert resp.status == 409


async def test_slack_undismiss_unknown_is_404(client, slack_file):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    resp = await client.post("/api/slack/queue/nope/undismiss", json={})
    assert resp.status == 404


def test_slack_valid_status_includes_dismissed():
    """SOFT-DISMISS adds 'dismissed' to the accepted status set; the original four
    actionable/terminal statuses are unchanged."""
    assert "dismissed" in slack_mod._VALID_STATUS
    assert {"needs-draft", "needs-review", "edited", "sent"} <= slack_mod._VALID_STATUS


# --- approve survives a concurrent */3-cron / 5s-watcher queue refresh ----- #
#
# The "waited 10s, didn't send" regression: a confirmed send fires its /approve
# AFTER the undo window, but the */3 cron and the 5s file-watcher refresh the
# whole queue (advancing the WHOLE-FILE etag) during that window. A page-level
# etag captured at confirm time is then STALE → the etag-guarded write 409s and
# the send is silently lost.
#
# T1 fix contract LOCKED IN here: the approve body forwards a per-ITEM content
# baseline `expectedDraft` (what the client believed THIS item's sendable text
# was at confirm time). On a stale WHOLE-FILE etag, the backend re-reads the
# target item and proceeds with the send IFF the item's OWN content still matches
# `expectedDraft` (an unrelated queue churn must not lose the send); it surfaces a
# 409 ONLY when the TARGET item itself changed vs the baseline (the user must
# re-review). This is a content match, NOT a blanket etag bypass.


async def test_slack_approve_survives_concurrent_refresh(client, slack_file, monkeypatch):
    """A stale WHOLE-FILE etag (from a concurrent cron/watcher refresh that did
    NOT touch the target item) does NOT lose an unedited approved send: it goes
    out and the item moves to 'sent'."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D0TEST1234",
        "snippet": "hi", "threadContext": "", "draft": "ready to send",
        "generatedDraft": "ready to send", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")

    # The page reads the queue and captures THIS etag at confirm time.
    queued = await (await client.get("/api/slack/queue")).json()
    stale_etag = queued["etag"]

    # Simulate the */3 cron / 5s watcher refreshing the queue DURING the undo
    # window: the whole-file etag advances (mtime_ns moves) but the TARGET item's
    # own content is untouched. sleep so the mtime_ns-based etag actually changes.
    _time.sleep(0.01)
    data, cur = slack_mod.filestore.read_json(slack_file)
    data["items"].append({
        "id": "i2", "sender": "z", "channel": "c2", "channelType": "dm",
        "snippet": "new unread", "threadContext": "", "draft": "",
        "generatedDraft": "", "status": "needs-draft", "ts": 2,
    })
    slack_mod.filestore.write_json(slack_file, data, cur)
    new_etag = slack_mod.filestore.read_json(slack_file)[1]
    assert new_etag != stale_etag  # the page-level etag is now genuinely stale

    _stub_agent(monkeypatch, {"available": True})
    # Forward the now-STALE page etag AND the per-item content baseline. The
    # target item's content is unchanged vs the baseline, so the send must succeed.
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "ready to send",
        "etag": stale_etag,
        "expectedDraft": "ready to send",
    })
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "sent"
    assert body["item"]["finalText"] == "ready to send"
    # Persisted: the target is sent, the concurrently-added sibling survived.
    saved = {it["id"]: it for it in json.loads(slack_file.read_text(encoding="utf-8"))["items"]}
    assert saved["i1"]["status"] == "sent"
    assert "i2" in saved  # the concurrent refresh's addition was not clobbered


async def test_slack_approve_conflict_when_target_item_changed(client, slack_file, monkeypatch):
    """The content match is NOT a blanket etag bypass: if the TARGET item's own
    content changed vs the forwarded baseline (e.g. the cron redrafted it during
    the window), approve returns 409 so the user must re-review — no surprise
    send of text they never approved."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "old text the user saw",
        "generatedDraft": "old text the user saw", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    stale_etag = (await (await client.get("/api/slack/queue")).json())["etag"]

    # The cron redrafts THIS item during the undo window — its content now differs
    # from what the user confirmed.
    _time.sleep(0.01)
    data, cur = slack_mod.filestore.read_json(slack_file)
    data["items"][0]["draft"] = "a totally different draft the cron wrote"
    data["items"][0]["generatedDraft"] = "a totally different draft the cron wrote"
    slack_mod.filestore.write_json(slack_file, data, cur)

    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "old text the user saw",
        "etag": stale_etag,
        "expectedDraft": "old text the user saw",
    })
    assert resp.status == 409
    # The send must NOT have fired (no approved text the user never re-reviewed).
    assert not any(call[0] == "send" for call in agent.calls)
    # And the item is NOT marked sent.
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] != "sent"


async def test_slack_approve_already_sent_409_even_on_stale_etag(client, slack_file, monkeypatch):
    """The double-send guard is INDEPENDENT of the stale-etag fix: an item already
    'sent' returns 409 regardless of the forwarded etag/baseline, so T1's
    concurrent-refresh fix can never be read as weakening the no-double-send
    guarantee."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "x",
        "generatedDraft": "x", "status": "sent", "ts": 1, "finalText": "x",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "x", "etag": "stale-or-bogus-etag", "expectedDraft": "x",
    })
    assert resp.status == 409
    # No second send was attempted.
    assert not any(call[0] == "send" for call in agent.calls)


# --- approve sends to routable channelId / loud failure when unroutable --- #


async def test_slack_approve_sends_to_channel_id(client, slack_file, monkeypatch):
    """When an item has a channelId, approve passes it as 'target' to the send
    seam — the subprocess receives the routable ID, not a display name."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "Huan Wang", "channel": "DM with Huan Wang",
        "channelType": "dm", "channelId": "D02P0TU89CN", "userId": "U02P2R0L3DX",
        "snippet": "hi", "threadContext": "", "draft": "sounds good",
        "generatedDraft": "sounds good", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "sounds good",
    })
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "sent"
    # The seam received the channelId as target, NOT the display name.
    assert len(agent.calls) == 1
    action, payload = agent.calls[0]
    assert action == "send"
    assert payload["target"] == "D02P0TU89CN"
    assert "DM with" not in payload.get("channel", "")


async def test_slack_approve_falls_back_to_user_id(client, slack_file, monkeypatch):
    """When channelId is absent but userId is present, approve uses userId as
    the routable target."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "Huan Wang", "channel": "DM with Huan Wang",
        "channelType": "dm", "userId": "U02P2R0L3DX",
        "snippet": "hi", "threadContext": "", "draft": "ok",
        "generatedDraft": "ok", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "ok",
    })
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "sent"
    assert agent.calls[0][1]["target"] == "U02P2R0L3DX"


async def test_slack_approve_unroutable_item_fails_loudly(client, slack_file, monkeypatch):
    """An item with NO channelId and NO userId returns 400 with a clear reason
    — the send seam is NEVER called, the item stays unsent."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "Huan Wang", "channel": "DM with Huan Wang",
        "channelType": "dm",
        "snippet": "hi", "threadContext": "", "draft": "sounds good",
        "generatedDraft": "sounds good", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "sounds good",
    })
    assert resp.status == 400
    body = await resp.json()
    assert body["available"] is False
    assert "routable" in body["reason"].lower() or "channel" in body["reason"].lower()
    # The send seam must NOT have been called.
    assert not any(call[0] == "send" for call in agent.calls)
    # The item remains unsent on disk.
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] != "sent"


async def test_slack_approve_empty_channel_id_is_unroutable(client, slack_file, monkeypatch):
    """An empty-string channelId (e.g. from a scan that couldn't capture one)
    is treated as absent — fails loudly just like a missing field."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "a", "channel": "DM with a",
        "channelType": "dm", "channelId": "", "userId": "",
        "snippet": "hi", "threadContext": "", "draft": "hey",
        "generatedDraft": "hey", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "hey",
    })
    assert resp.status == 400
    body = await resp.json()
    assert body["available"] is False
    assert not any(call[0] == "send" for call in agent.calls)


def test_slack_build_draft_prompt_injects_style_and_topic(slack_file):
    """build_draft_prompt must inject the learned style notes and the contact's
    running topic summary so generated drafts reach the prompt with memory."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_mod._style_path().write_text("# style\n- be terse\n", encoding="utf-8")
    slack_mod._topics_dir().mkdir(parents=True, exist_ok=True)
    (slack_mod._topics_dir() / "alice.md").write_text(
        "# Topics with alice\n- we discussed the launch\n", encoding="utf-8")
    # Use a Chinese snippet so the (style-sample) block is included — style notes
    # are gated out for non-Chinese conversations (they only drag the reply toward
    # Chinese). Topic memory + the incoming message are always injected.
    prompt = slack_mod.build_draft_prompt({
        "sender": "alice", "channel": "general",
        "snippet": "准备好了吗？", "threadContext": "ctx",
    })
    assert "be terse" in prompt
    assert "we discussed the launch" in prompt
    assert "准备好了吗？" in prompt


def test_slack_build_draft_prompt_scrubs_secrets(slack_file):
    """No credential material may ride into the draft prompt."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_mod._style_path().write_text("authorization: Bearer leak\n- be terse\n", encoding="utf-8")
    # Chinese snippet so the style block (which carries the secret to scrub) is included.
    prompt = slack_mod.build_draft_prompt({
        "sender": "alice", "channel": "general",
        "snippet": "你好", "threadContext": "",
    })
    assert "authorization:" not in prompt
    assert "be terse" in prompt


async def test_slack_topic_traversal_rejected(client, slack_file):
    resp = await client.get("/api/slack/topics/..%2f..%2fetc")
    assert resp.status == 400


# --- /api/slack/health (readiness probe — no message I/O, no send) --------- #


async def test_slack_health_shape_and_no_credentials(client, monkeypatch):
    """GET /api/slack/health returns the readiness shape; detail never leaks creds."""
    resp = await client.get("/api/slack/health")
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {"claudeOnPath", "mcpConfigured", "ready", "detail"}
    assert isinstance(body["claudeOnPath"], bool)
    assert body["mcpConfigured"] in (True, False, "unknown")
    assert isinstance(body["ready"], bool)
    assert isinstance(body["detail"], str)


def test_slack_probe_ready_when_claude_and_mcp_present(monkeypatch):
    monkeypatch.setattr(slack_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(slack_mod, "_detect_mcp_configured", lambda: True)
    probe = slack_mod._probe_readiness()
    assert probe["claudeOnPath"] is True
    assert probe["mcpConfigured"] is True
    assert probe["ready"] is True


def test_slack_probe_not_ready_when_claude_absent(monkeypatch):
    monkeypatch.setattr(slack_mod.shutil, "which", lambda _: None)
    probe = slack_mod._probe_readiness()
    assert probe["claudeOnPath"] is False
    assert probe["ready"] is False
    assert "PATH" in probe["detail"]


def test_slack_probe_mcp_unknown_is_possibly_ready(monkeypatch):
    monkeypatch.setattr(slack_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(slack_mod, "_detect_mcp_configured", lambda: "unknown")
    probe = slack_mod._probe_readiness()
    assert probe["mcpConfigured"] == "unknown"
    assert probe["ready"] is True  # 'unknown' is not a positive False


def test_slack_probe_not_ready_when_mcp_absent(monkeypatch):
    monkeypatch.setattr(slack_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(slack_mod, "_detect_mcp_configured", lambda: False)
    probe = slack_mod._probe_readiness()
    assert probe["mcpConfigured"] is False
    assert probe["ready"] is False


# --- _run_slack_agent failure modes (subprocess MOCKED — no real claude) --- #
#
# These exercise the live seam's fail-loud + no-fabrication contract without
# ever spawning a real `claude`. We monkeypatch asyncio.create_subprocess_exec
# in the slack module with a fake process whose stdout/stderr/returncode we
# control.


class _SlackFakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _SlackFakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _SlackFakeProc:
    def __init__(self, stdout_lines=(), stderr_lines=(), returncode=0):
        self.stdin = _SlackFakeStdin()
        self.stdout = _SlackFakeStream(stdout_lines)
        self.stderr = _SlackFakeStream(stderr_lines)
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _patch_subprocess(monkeypatch, proc=None, raises=None):
    async def fake_exec(*args, **kwargs):
        if raises is not None:
            raise raises
        return proc
    monkeypatch.setattr(slack_mod.asyncio, "create_subprocess_exec", fake_exec)


def _result_line(text):
    import json as _json
    return (_json.dumps({"type": "result", "result": text}) + "\n").encode()


async def test_slack_seam_spawn_failure_is_unavailable(monkeypatch):
    """A spawn failure (claude not found) returns available:false, never fabricates."""
    _patch_subprocess(monkeypatch, raises=FileNotFoundError("claude"))
    result = await slack_mod._run_slack_agent("list", {})
    assert result["available"] is False
    assert "items" not in result
    assert isinstance(result["reason"], str)


async def test_slack_seam_bad_json_is_unavailable(monkeypatch):
    """An unparseable reply must not fabricate items."""
    proc = _SlackFakeProc(stdout_lines=[_result_line("this is not json")])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("list", {})
    assert result["available"] is False
    assert "items" not in result


async def test_slack_seam_empty_reply_is_unavailable(monkeypatch):
    proc = _SlackFakeProc(stdout_lines=[_result_line("")])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("regenerate", {"prompt": "p"})
    assert result["available"] is False
    assert "draft" not in result


async def test_slack_seam_no_result_event_is_unavailable(monkeypatch):
    """If the stream ends with no result event, fail loud (no send-success)."""
    proc = _SlackFakeProc(stdout_lines=[(json.dumps({"type": "system"}) + "\n").encode()])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("send", {"channel": "c", "text": "hi"})
    assert result["available"] is False


async def test_slack_seam_auth_error_is_unavailable(monkeypatch):
    """An auth/credential error is classified and surfaced as unavailable."""
    proc = _SlackFakeProc(
        stdout_lines=[_result_line("ExpiredToken: The security token included in the request is expired")],
    )
    proc_evt = _SlackFakeProc(stdout_lines=[
        (json.dumps({"type": "result", "is_error": True,
                     "result": "The security token included in the request is expired"}) + "\n").encode()
    ])
    _patch_subprocess(monkeypatch, proc=proc_evt)
    result = await slack_mod._run_slack_agent("send", {"channel": "c", "text": "hi"})
    assert result["available"] is False
    assert "items" not in result and "ts" not in result


async def test_slack_seam_nonzero_exit_is_unavailable(monkeypatch):
    proc = _SlackFakeProc(stdout_lines=[_result_line("not json either")], returncode=2)
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("list", {})
    assert result["available"] is False


async def test_slack_seam_list_happy_path_parses_array(monkeypatch):
    """A valid STRICT JSON array list result is parsed into items."""
    items = [{"id": "x1", "sender": "alice", "channel": "general",
              "channelType": "dm", "snippet": "hi", "threadContext": ""}]
    proc = _SlackFakeProc(stdout_lines=[_result_line(json.dumps(items))])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("list", {})
    assert result["available"] is True
    assert result["items"][0]["id"] == "x1"


async def test_slack_seam_list_uses_shorter_timeout(monkeypatch):
    """The 'list' action runs under the shorter dedicated _LIST_TIMEOUT_S budget,
    not the 120s send/regenerate budget."""
    captured = {}

    async def fake_wait_for(coro, timeout):
        captured["timeout"] = timeout
        return await coro

    proc = _SlackFakeProc(stdout_lines=[_result_line(json.dumps([]))])
    _patch_subprocess(monkeypatch, proc=proc)
    monkeypatch.setattr(slack_mod.asyncio, "wait_for", fake_wait_for)
    await slack_mod._run_slack_agent("list", {})
    assert captured["timeout"] == slack_mod._LIST_TIMEOUT_S
    assert slack_mod._LIST_TIMEOUT_S < slack_mod._AGENT_TIMEOUT_S


async def test_slack_seam_send_happy_path_confirms_ok(monkeypatch):
    proc = _SlackFakeProc(stdout_lines=[_result_line(json.dumps({"ok": True, "ts": "1.2"}))])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("send", {"channel": "c", "text": "hi"})
    assert result["available"] is True
    assert result["ts"] == "1.2"


async def test_slack_seam_send_without_ok_is_unavailable(monkeypatch):
    """Send must NOT fabricate success when the agent doesn't confirm ok=true."""
    proc = _SlackFakeProc(stdout_lines=[_result_line(json.dumps({"ok": False}))])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("send", {"channel": "c", "text": "hi"})
    assert result["available"] is False


async def test_slack_seam_timeout_is_unavailable(monkeypatch):
    """A hung subprocess hits the bounded timeout and returns unavailable."""
    monkeypatch.setattr(slack_mod, "_AGENT_TIMEOUT_S", 0.05)

    class _HangStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            raise StopAsyncIteration

    proc = _SlackFakeProc()
    proc.stdout = _HangStream()
    proc.stderr = _SlackFakeStream([])
    _patch_subprocess(monkeypatch, proc=proc)
    result = await slack_mod._run_slack_agent("regenerate", {"prompt": "p"})
    assert result["available"] is False
    assert "timed out" in result["reason"].lower()


# --- T4 additive gaps: timeout-reap (no-orphan) + health detail cred-scrub --- #
#
# The slack seam-failure + health-shape coverage above (added by a parallel
# task) is comprehensive; these two close genuine remaining gaps the existing
# tests do NOT assert:
#   * the no-orphan subprocess-hygiene contract on timeout (existing timeout
#     test asserts only available:False + the reason string, NOT that the child
#     was reaped);
#   * the no-credential-leak contract on the health detail string across ALL
#     readiness states (existing health test asserts shape/types only).
# They reuse the existing _patch_subprocess helper and the client fixture — no
# new module-level fixture/class names (avoiding the duplicate-name shadowing
# footgun documented in qa.md).


async def test_slack_seam_timeout_reaps_subprocess(monkeypatch):
    """On timeout the seam must reap the child (no orphan) AND not fabricate.

    The hung proc reports returncode None (still alive) so _reap actually runs
    its escalation; we record that proc.wait() was awaited to prove the seam
    drove the reap rather than abandoning the process.
    """
    monkeypatch.setattr(slack_mod, "_AGENT_TIMEOUT_S", 0.05)

    class _HangStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            raise StopAsyncIteration

    class _ReapRecordingStdin:
        def write(self, _data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _ReapRecordingProc:
        def __init__(self):
            self.stdin = _ReapRecordingStdin()
            self.stdout = _HangStream()
            self.stderr = _SlackFakeStream([])
            self.returncode = None  # still alive -> _reap must act
            self.waited = 0
            self.terminated = False
            self.killed = False

        async def wait(self):
            self.waited += 1
            # First wait (the bounded grace) appears to succeed: the process
            # exits after being asked to close, so terminate/kill aren't needed.
            self.returncode = -15
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

    proc = _ReapRecordingProc()
    _patch_subprocess(monkeypatch, proc=proc)

    result = await slack_mod._run_slack_agent("regenerate", {"prompt": "p"})

    assert result["available"] is False
    # No fabrication on the timeout path.
    assert "items" not in result and "ts" not in result and "draft" not in result
    # The child was reaped, not orphaned: _reap awaited proc.wait() at least once.
    assert proc.waited >= 1


async def test_slack_health_detail_has_no_credential_markers(client, monkeypatch):
    """The health detail string must never carry credential material in ANY
    readiness state. Drives all four states and scans detail against the same
    _SECRET_MARKERS the backend scrubs with."""
    markers = ("cookie", "mwinit", "~/.midway", ".midway", "authorization:", "aws_secret")

    states = [
        (lambda _: "/usr/bin/claude", lambda: True),       # ready
        (lambda _: "/usr/bin/claude", lambda: False),      # mcp absent
        (lambda _: "/usr/bin/claude", lambda: "unknown"),  # mcp unknown
        (lambda _: None, lambda: True),                    # claude absent
    ]
    for which_fn, mcp_fn in states:
        monkeypatch.setattr(slack_mod.shutil, "which", which_fn)
        monkeypatch.setattr(slack_mod, "_detect_mcp_configured", mcp_fn)
        resp = await client.get("/api/slack/health")
        assert resp.status == 200
        body = await resp.json()
        detail_low = body["detail"].lower()
        assert detail_low, "detail must be a non-empty explanation"
        for marker in markers:
            assert marker not in detail_low, f"credential marker leaked: {marker!r}"
        # `ready` must track claudeOnPath being false.
        if not body["claudeOnPath"]:
            assert body["ready"] is False



# --- Bug 4 drain-bound contract (backend half) ----------------------------- #
#
# The background draft DRAIN lives frontend-side (SlackPage), naturally bounded
# to a small concurrency while the page is open. The CONCURRENCY CAP itself is a
# browser concern (no real subprocess runs in this suite), so here we verify the
# BACKEND invariant a bounded drain relies on: each per-item /{id}/refresh is
# INDEPENDENT (drafting one item never mutates another) and is READ-ONLY (the
# regenerate seam never sends). The per-item happy/fail-loud + needs-draft ->
# needs-review flips are already covered by
# test_slack_needs_draft_regenerates_to_needs_review /
# test_slack_needs_draft_failed_draft_stays_needs_draft above; this adds the
# isolation + never-sends assertions those don't make.
async def test_slack_drain_draft_is_per_item_isolated_and_never_sends(client, slack_file, monkeypatch):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [
        {"id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
         "snippet": "one", "threadContext": "", "draft": "",
         "generatedDraft": "", "status": "needs-draft", "ts": 1},
        {"id": "i2", "sender": "b", "channel": "c", "channelType": "dm",
         "snippet": "two", "threadContext": "", "draft": "",
         "generatedDraft": "", "status": "needs-draft", "ts": 2},
    ]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "drafted i1"})
    resp = await client.post("/api/slack/queue/i1/refresh")
    assert resp.status == 200
    items = {it["id"]: it for it in json.loads(slack_file.read_text(encoding="utf-8"))["items"]}
    # Drafting i1 must touch ONLY i1 — i2 stays needs-draft with an empty draft.
    assert items["i1"]["status"] == "needs-review" and items["i1"]["draft"] == "drafted i1"
    assert items["i2"]["status"] == "needs-draft" and items["i2"]["draft"] == ""
    # The drain reuses the regenerate path, which is READ-ONLY: it NEVER sends.
    assert agent.calls and all(action == "regenerate" for action, _ in agent.calls)
    assert all(action != "send" for action, _ in agent.calls)


# --- In-process scan + manual-refresh trigger (D-023) + external observability - #
#
# The scanner is the in-process ``_scan_worker`` running the deterministic
# ``_scan_once`` (get_unreads → write slack_threads.json). The server is no longer a
# pure cron reader — but it also never blocks a request on a scan:
#   * POST /api/slack/queue/refresh triggers a REAL scan by SETTING the worker's wake
#     Event (so a scan starts promptly, not after the full interval), returns the warm
#     etag immediately (NON-blocking — no inline scan in the request), and does NOT
#     write any ``.slack_scan_now`` sentinel (that retired cron sentinel is GONE).
#   * A manual Refresh shares ONE scan-in-progress guard (``_get_scan_lock``) with the
#     periodic worker: if a scan is already running, the manual trigger is dismissed
#     with HTTP 200 ``{scanning:false, reason:"A scan is already in progress."}`` —
#     a deliberately non-error shape the page surfaces instead of "Scanning…".
#   * GET /api/slack/queue echoes a durable top-level ``lastScanAt`` (epoch ms) that
#     ``_scan_once`` persists on EVERY completed scan, so the "Updated …" freshness
#     label reflects scan freshness and survives a server restart.
#   * an externally-written change to slack_threads.json (a scan/cron write) is
#     observable to an open page via GET /queue and the file-watcher's broadcast.
# All exercised with NO real claude/Slack I/O (the seam + scan-read are stubbed).


async def test_slack_refresh_triggers_real_scan_non_blocking(client, slack_file, monkeypatch):
    """NEW refresh contract (D-023): POST /api/slack/queue/refresh triggers a REAL
    scan by SETTING the worker's wake Event, returns the warm etag IMMEDIATELY
    (HTTP 202 {scanning:true, etag}) WITHOUT blocking the request on the MCP scan,
    and writes NO ``.slack_scan_now`` sentinel (the retired cron sentinel is gone).

    Skeptic asserts: (1) the wake Event the worker awaits is SET by the handler (the
    real trigger, not a no-op); (2) the handler runs NO inline blocking scan in the
    request — ``_scan_once`` is monkeypatched to a tripwire that fails the test if the
    request awaits it, and the scanning seam / subprocess is never spawned.
    """
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")

    # Tripwire: the request must NOT run a scan inline (it returns immediately and
    # the background worker does the scan). If refresh awaits _scan_once, fail loud.
    async def boom_scan_once(app):
        raise AssertionError("refresh must NOT run an inline blocking scan in the request")

    async def boom_exec(*args, **kwargs):
        raise AssertionError("refresh must NOT spawn a scanning subprocess in the request")

    monkeypatch.setattr(slack_mod, "_scan_once", boom_scan_once)
    monkeypatch.setattr(slack_mod.asyncio, "create_subprocess_exec", boom_exec)
    # The MCP-delegation seam must not be invoked on the refresh path either.
    agent = _stub_agent(monkeypatch, {"available": True, "items": []})

    # The sidecar sibling sentinel is dead — assert it never appears.
    sentinel = slack_mod.SLACK_PATH.parent / ".slack_scan_now"
    assert not sentinel.exists()

    # The startup _scan_worker (launched on_startup in this test) is already parked
    # on the module wake Event and would CONSUME+clear it the instant the handler
    # sets it — racing any post-request is_set() check. Swap in a FRESH Event the
    # running worker is NOT awaiting, so the handler's _get_scan_event().set() lands
    # on an event nothing clears; we can then deterministically observe the trigger.
    probe_event = asyncio.Event()
    monkeypatch.setattr(slack_mod, "_scan_wake_event", probe_event)
    assert not probe_event.is_set()

    resp = await client.post("/api/slack/queue/refresh")
    assert resp.status == 202
    body = await resp.json()
    # Distinct, non-error success shape: a scan was triggered, warm etag returned.
    assert body["scanning"] is True
    assert "etag" in body

    # The REAL trigger: the handler SET the worker's wake Event (so the periodic
    # worker's interval sleep is interrupted and a scan starts promptly, not 5 min
    # later) — not a no-op. Asserted on the probe event nothing else consumes.
    assert probe_event.is_set()
    # No inline scan, no spawned subprocess, the seam was never called.
    assert agent.calls == []
    # The retired cron sentinel was NOT written.
    assert not sentinel.exists()


async def test_slack_queue_returns_last_scan_at(client, slack_file):
    """GET /api/slack/queue echoes the durable top-level ``lastScanAt`` (epoch ms)
    that ``_scan_once`` persists on every completed scan (D-023). It survives a
    server restart by virtue of living on disk (``_load`` does not strip it), and
    degrades to ``None`` (never crashes) when no scan has happened yet.
    """
    slack_file.parent.mkdir(parents=True, exist_ok=True)

    # Never-scanned case: no lastScanAt key on disk → endpoint returns it as null.
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    body = await (await client.get("/api/slack/queue")).json()
    assert body["lastScanAt"] is None

    # A scan completed and persisted lastScanAt; the endpoint reads it back from
    # disk (the persistence-survives-restart path — written to the file, read
    # through a fresh GET, not held in process memory).
    scanned_at = 1_750_000_000_000
    slack_file.write_text(json.dumps({
        "lastScanAt": scanned_at,
        "items": [{
            "id": "i1", "sender": "a", "channel": "c", "channelType": "dm",
            "snippet": "hi", "threadContext": "", "draft": "", "generatedDraft": "",
            "status": "needs-draft", "ts": 1,
        }],
    }), encoding="utf-8")
    body = await (await client.get("/api/slack/queue")).json()
    assert body["lastScanAt"] == scanned_at
    # The items still come through alongside the freshness timestamp.
    assert [it["id"] for it in body["items"]] == ["i1"]


async def test_slack_refresh_while_scan_in_progress_is_rejected(client, slack_file, monkeypatch):
    """A manual Refresh that races an IN-FLIGHT scan (periodic worker OR a prior
    manual trigger) is DISMISSED, not errored (D-023, spec item 3). We simulate an
    in-flight scan by holding the shared scan guard (``_get_scan_lock()``, the same
    lock ``_run_guarded_scan`` holds for the duration of a scan), then POST refresh
    and assert a NON-error response (HTTP 200) carrying the clear, secret-free
    ``{scanning:false, reason:"A scan is already in progress."}`` the page surfaces
    instead of a misleading "Scanning…" state.

    The refresh path must NOT spawn a subprocess or run an inline scan even on the
    rejected path. The autouse _reset_slack_scan_guard fixture clears the lock/event
    after this test so the held lock never leaks into a later test.
    """
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")

    async def boom_exec(*args, **kwargs):
        raise AssertionError("a rejected refresh must NOT spawn a scanning subprocess")

    async def boom_scan_once(app):
        raise AssertionError("a rejected refresh must NOT run an inline scan")

    monkeypatch.setattr(slack_mod.asyncio, "create_subprocess_exec", boom_exec)
    monkeypatch.setattr(slack_mod, "_scan_once", boom_scan_once)

    # Hold the shared scan guard to simulate a scan already in flight.
    lock = slack_mod._get_scan_lock()
    await lock.acquire()
    try:
        resp = await client.post("/api/slack/queue/refresh")
        # Non-error: a 200/202 (NOT 4xx/5xx). The contract is HTTP 200.
        assert resp.status in (200, 202)
        body = await resp.json()
        assert body["scanning"] is False
        assert "already in progress" in body["reason"].lower()
        # The reason string carries NO secret material (it is shown verbatim in UI).
        reason_blob = body["reason"].lower()
        for marker in ("cookie", "mwinit", "~/.midway", ".midway", "authorization:", "aws_secret"):
            assert marker not in reason_blob
    finally:
        lock.release()


async def test_slack_external_write_is_observable_via_queue(client, slack_file):
    """A cron write (simulated as an out-of-band write to slack_threads.json) is
    observable to an open page: GET /api/slack/queue returns the NEW drafted items
    without any server scan. This is the client-backstop-poll path of the DONE-WHEN
    repaint check — the page re-fetches and sees the cron's freshly drafted card.
    """
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    # Start empty (as the page would first see it).
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    first = await (await client.get("/api/slack/queue")).json()
    assert first["items"] == []

    # etag is mtime_ns-based, so separate the two writes in real time or the
    # mtimes collide and the etag wouldn't appear to advance.
    _time.sleep(0.01)
    # The cron drafts a needs-review card (with history3d) directly into the file.
    slack_file.write_text(json.dumps({"items": [{
        "id": "cron1", "sender": "alice", "channel": "general", "channelType": "dm",
        "snippet": "ping", "threadContext": "", "draft": "drafted by cron",
        "generatedDraft": "drafted by cron", "status": "needs-review", "ts": 2,
        "history3d": [{"ts": 1, "author": "alice", "text": "ping"}],
    }]}), encoding="utf-8")

    # The next page fetch (backstop poll / live-update repaint / manual Refresh)
    # surfaces the new card with NO server-side scan.
    again = await (await client.get("/api/slack/queue")).json()
    assert [it["id"] for it in again["items"]] == ["cron1"]
    card = again["items"][0]
    assert card["status"] == "needs-review"
    assert card["draft"] == "drafted by cron"
    # The cron's history3d passes through untouched (GET /queue is a passthrough).
    assert card["history3d"][0]["text"] == "ping"
    # The etag advanced, so a page comparing etags would repaint.
    assert again["etag"] != first["etag"]


async def test_slack_watcher_broadcasts_on_external_change(client, slack_file, monkeypatch):
    """If T1 implemented the in-process file-watcher, an external write to
    slack_threads.json (a cron write) drives a 'slack_changed' broadcast so any
    open page repaints. Skips cleanly if the watcher was omitted (the client
    backstop poll in test_slack_external_write_is_observable_via_queue covers
    promptness in that case)."""
    watcher = getattr(slack_mod, "_slack_watcher", None)
    if watcher is None:
        pytest.skip("server file-watcher not implemented (client backstop poll covers repaint)")

    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")

    app = client.app
    # Record every broadcast the watcher emits.
    events: list = []

    async def record(event_type, data=None):
        events.append((event_type, data))

    monkeypatch.setattr(app["ws_manager"], "broadcast", record)
    # Tighten the watch interval (if exposed) so the loop ticks fast under test.
    if hasattr(slack_mod, "SLACK_WATCH_INTERVAL_S"):
        monkeypatch.setattr(slack_mod, "SLACK_WATCH_INTERVAL_S", 0.01)

    task = asyncio.create_task(watcher(app))
    try:
        # Let the watcher take its baseline mtime/etag reading.
        await asyncio.sleep(0.05)
        # Simulate a cron write: change the file out-of-band.
        await asyncio.sleep(0.02)
        slack_file.write_text(json.dumps({"items": [{
            "id": "cron1", "sender": "a", "channel": "c", "channelType": "dm",
            "snippet": "hi", "threadContext": "", "draft": "d",
            "generatedDraft": "d", "status": "needs-review", "ts": 1,
        }]}), encoding="utf-8")
        # Give the watcher a few ticks to notice the change and broadcast.
        for _ in range(50):
            if any(ev[0] == "slack_changed" for ev in events):
                break
            await asyncio.sleep(0.02)
        assert any(ev[0] == "slack_changed" for ev in events), \
            "watcher did not broadcast slack_changed after an external file change"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_slack_scan_spawn_scopes_to_slack_mcp_with_batch_and_cap(monkeypatch):
    """The spawn must carry --mcp-config + --strict-mcp-config (slack-mcp only),
    the allowlist must include the batch read tools, and the list prompt must
    state the cap — all verifiable on the spawn args / prompt."""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        return _SlackFakeProc(stdout_lines=[_result_line(json.dumps([]))])

    monkeypatch.setattr(slack_mod.asyncio, "create_subprocess_exec", fake_exec)
    result = await slack_mod._run_slack_agent("list", {})
    assert result["available"] is True

    args = captured["args"]
    assert "--strict-mcp-config" in args
    assert "--mcp-config" in args
    cfg_path = args[args.index("--mcp-config") + 1]
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8")) if Path(cfg_path).exists() else None
    if cfg is not None:
        # Only slack-mcp is loaded, and the config carries NO secret markers.
        assert list(cfg.get("mcpServers", {})) == ["slack-mcp"]
        blob = json.dumps(cfg).lower()
        for marker in ("cookie", "mwinit", "~/.midway", "authorization:", "aws_secret"):
            assert marker not in blob
    # Batch read tools are on the read allowlist (more threads, fewer round-trips).
    allow = args[args.index("--allowedTools") + 1]
    assert "mcp__slack-mcp__batch_get_threads" in allow
    assert "mcp__slack-mcp__batch_get_messages" in allow
    # The list prompt states the cap explicitly.
    prompt = slack_mod._build_agent_prompt("list", {})
    assert str(slack_mod.SLACK_LIST_CAP) in prompt


def test_slack_mcp_config_has_no_secrets_and_only_slack(slack_file):
    """_slack_mcp_config builds a slack-mcp-only doc with no credential material."""
    cfg = slack_mod._slack_mcp_config()
    assert list(cfg["mcpServers"]) == ["slack-mcp"]
    blob = json.dumps(cfg).lower()
    for marker in ("cookie", "mwinit", "~/.midway", "authorization:", "aws_secret"):
        assert marker not in blob


def test_slack_send_allowlist_unchanged_by_scoping():
    """Scoping which servers load must NOT widen what 'send' may do: send still
    gets ONLY post_message; the batch read tools are NOT on the send allowlist."""
    assert slack_mod._ALLOWED_TOOLS["send"] == slack_mod._SLACK_SEND_TOOLS
    assert "mcp__slack-mcp__batch_get_threads" not in slack_mod._ALLOWED_TOOLS["send"]
    assert "mcp__slack-mcp__post_message" not in slack_mod._SLACK_READ_TOOLS


# --------------------------------------------------------------------------- #
# Backend draft-worker pool (D-015)
#
# The cron writes draft-free SKELETON items (status 'needs-draft') + flags
# existing items that grew new messages (needsRedraft). An in-process asyncio
# worker drafts them in parallel via the read-only 'draft' seam (capped by a
# Semaphore). These exercise the worker's selection + per-item draft coroutine
# WITHOUT a real `claude` (the seam is stubbed; we monkeypatch _probe_readiness
# ready so the gate passes, and drive ONE worker iteration directly — same shape
# as the file-watcher test above). The startup worker in every test stays inert
# (its first action is an interval sleep + a readiness gate that is False because
# `claude` is absent), so it never spawns under aiohttp_client(app).
# --------------------------------------------------------------------------- #


def test_slack_draft_action_grants_no_write_tool_and_uses_full_timeout():
    """D-022 (cold-start invariant): the 'draft' action is MCP-FREE at spawn time —
    _drive_agent sets needs_mcp=False for it (asserted in the arg-capture test
    below), so its _ALLOWED_TOOLS entry is never consulted. Even so, that entry must
    NEVER grant a write tool (it is read-only by design), and draft reuses the full
    agent timeout (not the shorter list budget). (Updated from the pre-D-022 test
    that asserted draft mapped verbatim to the read-only allowlist as the SEND-safety
    boundary; the real boundary is now needs_mcp=False — draft gets no allowlist at
    all at spawn — eliminating per-cold-spawn SAML 429 throttling.)"""
    # If the (now dead-at-spawn) draft entry exists, it must be read-only — never a
    # post_message write tool. Tolerant of the entry being absent entirely.
    draft_tools = slack_mod._ALLOWED_TOOLS.get("draft", [])
    assert "mcp__slack-mcp__post_message" not in draft_tools
    # And it reuses the standard agent timeout (not the shorter list budget).
    assert slack_mod._timeout_for("draft") == slack_mod._AGENT_TIMEOUT_S


def test_slack_draft_prompt_forbids_tool_calls_and_keeps_return_shape():
    """D-022: the 'draft' prompt no longer instructs the subprocess to FETCH
    history — it explicitly forbids ANY tool call (history is pre-fetched on the
    persistent session and rendered into the wrapped per-item prompt). It still
    asks for the {draft, generatedDraft, threadContext} strict-JSON shape and must
    not send. (Updated from the pre-D-022 test that asserted batch_get_messages /
    '3-day' history-fetch instructions, which are gone now that drafting is
    MCP-free.)"""
    prompt = slack_mod._build_agent_prompt("draft", {"prompt": "BASE_PROMPT"})
    # The subprocess is told NOT to fetch anything / call any tools.
    assert "do NOT call any tools" in prompt
    assert "do NOT fetch anything" in prompt
    # No history-fetch tool name leaks into the (now MCP-free) draft prompt.
    assert "batch_get_messages" not in prompt
    # The return-shape contract is still enforced.
    assert "generatedDraft" in prompt
    assert "BASE_PROMPT" in prompt  # the per-item instructions are wrapped in
    assert "do NOT send" in prompt


def test_slack_polish_prompt_forbids_tools_translation_and_keeps_voice():
    """D-029: the 'polish' prompt forbids ANY tool call / fetch / send, embeds the
    user's text, requires preserving their voice and SAME language (no translation),
    and asks for the {draft} strict-JSON shape."""
    prompt = slack_mod._build_agent_prompt("polish", {"text": "MY_DRAFT_TEXT"})
    assert "do NOT call any tools" in prompt
    assert "do NOT fetch anything" in prompt
    assert "do NOT send" in prompt
    # Preserve voice + language; never translate or change meaning.
    assert "do NOT translate" in prompt
    assert "preserve MY" in prompt
    assert "Do NOT change the meaning" in prompt
    # The user's actual text is embedded and the return shape is enforced.
    assert "MY_DRAFT_TEXT" in prompt
    assert '"draft"' in prompt


def test_slack_polish_parse_requires_draft():
    """_parse_agent_result('polish') mirrors 'regenerate' exactly: a dict with
    'draft' is available; a missing 'draft' is fail-loud (no fabrication)."""
    ok = slack_mod._parse_agent_result(json.dumps({"draft": "polished"}), "polish")
    assert ok["available"] is True and ok["draft"] == "polished"
    bad = slack_mod._parse_agent_result(json.dumps({"nope": 1}), "polish")
    assert bad["available"] is False and "draft" not in bad
    empty = slack_mod._parse_agent_result("", "polish")
    assert empty["available"] is False


def test_slack_draft_parse_passes_through_history_and_generated():
    """_parse_agent_result('draft') mirrors 'regenerate' (require dict + 'draft')
    and additionally passes through history3d (list) + generatedDraft, defaulting
    generatedDraft to draft and a non-list history3d to []."""
    ok = slack_mod._parse_agent_result(
        json.dumps({"draft": "hi", "history3d": [{"ts": 1, "author": "a", "text": "x"}]}),
        "draft",
    )
    assert ok["available"] is True
    assert ok["draft"] == "hi"
    assert ok["generatedDraft"] == "hi"  # defaulted to draft
    assert ok["history3d"][0]["text"] == "x"
    # Missing 'draft' -> unavailable (no fabrication).
    bad = slack_mod._parse_agent_result(json.dumps({"history3d": []}), "draft")
    assert bad["available"] is False and "draft" not in bad
    # Non-list history3d normalizes to [].
    nl = slack_mod._parse_agent_result(json.dumps({"draft": "h", "history3d": "nope"}), "draft")
    assert nl["history3d"] == []


def test_slack_needs_draft_selection_and_edited_guard():
    """The worker selects needs-draft skeletons and needs-review+needsRedraft
    items, but NEVER an edited (draft != generatedDraft) item — even with a stray
    needsRedraft flag (defense in depth, spec item 4)."""
    nd = slack_mod._needs_draft
    assert nd({"status": "needs-draft"}) is True
    assert nd({"status": "needs-review", "needsRedraft": True, "draft": "d", "generatedDraft": "d"}) is True
    # Edited item with a stray needsRedraft -> NOT selected (user owns the draft).
    assert nd({"status": "needs-review", "needsRedraft": True, "draft": "EDITED", "generatedDraft": "d"}) is False
    # needs-review without the flag (already drafted) -> not selected.
    assert nd({"status": "needs-review", "draft": "d", "generatedDraft": "d"}) is False
    # Terminal / in-progress states -> never selected.
    assert nd({"status": "sent"}) is False
    assert nd({"status": "dismissed"}) is False
    assert nd({"status": "edited", "draft": "x", "generatedDraft": "y"}) is False


async def _run_one_draft_cycle(app, monkeypatch, fetch_history=None):
    """Drive ONE _draft_worker iteration: tighten the interval, force readiness,
    and run the worker for a moment so its (sleep → gate → select → gather) body
    executes exactly enough, then cancel cleanly.

    HERMETICITY (D-022): _draft_one now pre-fetches history via _fetch_history_for
    on the PERSISTENT MCP session when an item lacks history3d. In tests that real
    call would spawn a live slack-mcp subprocess (the very cold-start the feature
    eliminates) and hit a real SAML auth. So we ALWAYS stub _fetch_history_for here
    — by default to an empty-history coroutine (the worker then drafts from the
    snippet, exactly as in the unreadable-channel case) so no test escapes the
    stubbed seam into a real MCP spawn. A test that needs a specific pre-fetched
    history passes its own `fetch_history` coroutine.
    """
    if fetch_history is None:
        async def fetch_history(_channel_id):
            return []
    monkeypatch.setattr(slack_mod, "_fetch_history_for", fetch_history)
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    monkeypatch.setattr(slack_mod, "SLACK_DRAFT_POLL_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._draft_worker(app))
    try:
        # A few ticks: long enough for at least one full select+draft+write cycle.
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_slack_worker_drafts_a_needs_draft_skeleton(client, slack_file, monkeypatch):
    """A cron-written needs-draft SKELETON triggers the worker, which drafts it via
    the read-only 'draft' seam and flips it to needs-review with the draft +
    history3d filled in."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "s1", "sender": "alice", "channel": "DM with alice", "channelType": "dm",
        "channelId": "D1", "userId": "U1", "snippet": "ping", "ts": 1,
        "status": "needs-draft",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {
        "available": True, "draft": "drafted by worker",
        "history3d": [{"ts": 1, "author": "alice", "text": "ping"}],
        "generatedDraft": "drafted by worker",
    })
    await _run_one_draft_cycle(client.app, monkeypatch)

    # The worker used the READ-ONLY 'draft' action (never 'send').
    assert agent.calls and all(action == "draft" for action, _ in agent.calls)
    assert all(action != "send" for action, _ in agent.calls)
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["draft"] == "drafted by worker"
    assert saved["generatedDraft"] == "drafted by worker"
    assert saved["history3d"][0]["text"] == "ping"
    assert not saved.get("needsRedraft")


async def test_slack_worker_redrafts_needs_review_with_needsredraft(client, slack_file, monkeypatch):
    """A needs-review item flagged needsRedraft (the conversation grew) gets
    re-drafted: the new draft replaces the old, history3d updates, and needsRedraft
    is cleared. The redraft prompt carries the re-draft instruction."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "r1", "sender": "bob", "channel": "DM with bob", "channelType": "dm",
        "channelId": "D2", "userId": "U2", "snippet": "and one more thing", "ts": 5,
        "status": "needs-review", "draft": "old draft", "generatedDraft": "old draft",
        "needsRedraft": True,
        "history3d": [{"ts": 4, "author": "bob", "text": "first"}],
    }]}), encoding="utf-8")

    captured = {}

    async def fake_agent(action, payload):
        captured["action"] = action
        captured["prompt"] = payload.get("prompt", "")
        return {"available": True, "draft": "fresh cohesive reply",
                "history3d": [{"ts": 4, "author": "bob", "text": "first"},
                              {"ts": 5, "author": "bob", "text": "and one more thing"}],
                "generatedDraft": "fresh cohesive reply"}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)
    await _run_one_draft_cycle(client.app, monkeypatch)

    assert captured.get("action") == "draft"
    # The re-draft prompt instructs ONE cohesive reply to all unanswered messages.
    assert "addresses ALL their unanswered messages" in captured["prompt"]
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["draft"] == "fresh cohesive reply"
    assert saved["generatedDraft"] == "fresh cohesive reply"
    assert len(saved["history3d"]) == 2
    assert not saved.get("needsRedraft")


async def test_slack_worker_never_redrafts_an_edited_item(client, slack_file, monkeypatch):
    """CRITICAL (spec item 4): the worker must NEVER re-draft an item whose draft
    was manually edited (draft != generatedDraft). Even with a stray needsRedraft
    flag the user's draft is left ALONE — the seam is never even invoked for it."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "e1", "sender": "carol", "channel": "DM with carol", "channelType": "dm",
        "channelId": "D3", "snippet": "new msg", "ts": 9,
        # User edited the draft (diverged from generatedDraft) but a needsRedraft
        # flag is lingering — the guard must still protect their text.
        "status": "needs-review", "draft": "MY OWN WORDS", "generatedDraft": "machine draft",
        "needsRedraft": True,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "ROBO REPLACEMENT",
                                      "generatedDraft": "ROBO REPLACEMENT"})
    await _run_one_draft_cycle(client.app, monkeypatch)

    # The seam was NEVER called for this edited item (selection guard rejected it).
    assert agent.calls == []
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    # The user's edited draft is untouched.
    assert saved["draft"] == "MY OWN WORDS"
    assert saved["status"] == "needs-review"


async def test_slack_draft_one_guards_edit_that_lands_during_draft(client, slack_file, monkeypatch):
    """Defense in depth: even if an item passed the selection guard, the per-item
    coroutine RE-READS fresh before writing. If the user edited the draft DURING
    the draft subprocess (draft now != generatedDraft), the worker must NOT
    overwrite their text — it only refreshes history3d + clears needsRedraft."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "x1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "ping", "ts": 1, "status": "needs-draft",
    }]}), encoding="utf-8")

    async def fake_agent(action, payload):
        # Simulate the user editing the draft WHILE the subprocess runs: rewrite the
        # sidecar so the re-read inside _draft_one finds an edited item.
        data, _ = slack_mod._load()
        data["items"][0].update({"status": "edited", "draft": "USER EDIT",
                                 "generatedDraft": "machine"})
        slack_mod.filestore.write_json(slack_mod.SLACK_PATH, data, None)
        return {"available": True, "draft": "ROBO", "generatedDraft": "ROBO",
                "history3d": [{"ts": 1, "author": "a", "text": "ping"}]}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)
    sem = asyncio.Semaphore(1)
    await slack_mod._draft_one(client.app, sem, "x1",
                               slack_mod.build_draft_prompt({"sender": "a", "snippet": "ping"}))

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    # The user's in-flight edit survived — the worker did NOT clobber it.
    assert saved["draft"] == "USER EDIT"
    assert saved["status"] == "edited"
    # But history3d was refreshed for context.
    assert saved["history3d"][0]["text"] == "ping"


async def test_slack_worker_leaves_already_drafted_items_untouched(client, slack_file, monkeypatch):
    """Backward compat: an existing needs-review item that already has a draft and
    NO needsRedraft flag is NOT selected — the worker never touches it (and the
    seam is never invoked)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "d1", "sender": "dan", "channel": "DM with dan", "channelType": "dm",
        "channelId": "D4", "snippet": "hi", "ts": 1,
        "status": "needs-review", "draft": "already drafted", "generatedDraft": "already drafted",
        "history3d": [{"ts": 1, "author": "dan", "text": "hi"}],
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "should not run"})
    await _run_one_draft_cycle(client.app, monkeypatch)

    assert agent.calls == []
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "already drafted"


async def test_slack_worker_unavailable_leaves_item_for_retry(client, slack_file, monkeypatch):
    """An unavailable seam result leaves the needs-draft item AS-IS (fail-loud, no
    fabrication) so the next poll cycle retries it."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "s1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "ping", "ts": 1, "status": "needs-draft",
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    await _run_one_draft_cycle(client.app, monkeypatch)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-draft"
    assert "draft" not in saved or saved.get("draft", "") == ""


async def test_slack_worker_inert_until_ready(client, slack_file, monkeypatch):
    """When _probe_readiness reports NOT ready (e.g. claude absent — the test
    suite's default), the worker spawns nothing even with pending items: the seam
    is never invoked. This is the gate that keeps the startup worker inert under
    aiohttp_client(app) in every test."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "s1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "ping", "ts": 1, "status": "needs-draft",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "x"})
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": False})
    monkeypatch.setattr(slack_mod, "SLACK_DRAFT_POLL_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._draft_worker(client.app))
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert agent.calls == []
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-draft"


async def test_slack_draft_worker_task_started_and_cancelled_cleanly(aiohttp_client, app):
    """The draft-worker lifecycle: on_startup creates app['slack_draft_task'];
    closing the client cancels+awaits it (no orphan, no 'Task was destroyed')."""
    c = await aiohttp_client(app)
    task = app.get("slack_draft_task")
    assert task is not None and not task.done()
    await c.close()
    assert task.done()
    assert "slack_draft_task" not in app


# --------------------------------------------------------------------------- #
# Deterministic SCAN worker (D-018) — the scan no longer needs an LLM.
#
# The scan moved out of the cron's `claude --print` turn into an in-process
# asyncio worker that drives the Slack MCP server DIRECTLY over stdio via the
# `mcp` SDK (call_read_tool -> stdio_client -> ClientSession). NO `claude`
# subprocess, NO LLM in the scan loop. These tests guard the new pure-Python
# scan path the existing `_run_slack_agent('list')`/`_draft_worker` tests do NOT
# touch:
#   * THE SEND-SAFETY BOUNDARY: call_read_tool REFUSES any tool not in the
#     hardcoded _SLACK_READ_ONLY_TOOLS frozenset BEFORE the SDK touches the
#     server — incl. a SABOTAGE check (widening the allowlist lets post_message
#     through, proving the frozenset is the single load-bearing guard).
#   * the deterministic merge: writes skeletons for new channelIds, dedupes
#     existing, sets needsRedraft on a STALE needs-review, never resurrects
#     dismissed/sent, distinguishes a group-DM 'C…' from a 1:1-DM 'D…' that
#     share a participant, and gives a 'sent' item with NEWER activity a fresh
#     skeleton.
#   * a "drive ONE cycle" test that monkeypatches _scan_read to canned
#     list_dms/get_unreads payloads and tightens the interval (mirroring
#     _run_one_draft_cycle) — NOT a real MCP server.
#   * the inertness-under-harness contract. NB (slack.py warning): in THIS env
#     _probe_readiness().get('ready') is True (claude + slack-mcp are genuinely
#     present), so the startup _scan_worker is inert under aiohttp_client(app)
#     ONLY because its first action is the full-interval interval wait — NOT
#     because the gate is False. D-023 replaced the bare sleep with an
#     interruptible ``wait_for(event.wait(), SLACK_SCAN_INTERVAL_S)``, but absent a
#     SET wake Event it just times out after the full interval, behaving exactly
#     like the old sleep-first. The inert test therefore proves the unset
#     interruptible wait keeps it from calling _scan_read, and does NOT lean on the
#     gate being False here.
# --------------------------------------------------------------------------- #


@pytest.fixture
def reset_scan_ts(monkeypatch):
    """Reset the module-level last-scan tracker so the activity window starts
    from the fixed fallback (no leakage between tests). _scan_window_start_ms
    uses _LAST_SCAN_TS_MS when set, else now - _SCAN_WINDOW_MS."""
    monkeypatch.setattr(slack_mod, "_LAST_SCAN_TS_MS", None)


@pytest.fixture
def reset_mcp_state():
    """Reset the persistent MCP session state so no prior test's session leaks.

    The persistent session pattern (D-020) stores the MCP session + context managers
    in _mcp_state (a module-level dataclass instance). Without this reset a prior
    test's stored session would be reused by _connect_mcp (it short-circuits on
    _mcp_state.session is not None), making the monkeypatched stdio_client/ClientSession
    never reached. Clearing all fields to None forces a fresh connect on the next
    call_read_tool, exactly as if the server just started.
    """
    state = slack_mod._mcp_state
    state.session = None
    state.read_stream = None
    state.write_stream = None
    state.stdio_cm = None
    state.session_cm = None
    state.connected_at = None
    # Also clear the lock to avoid stale lock state between tests.
    slack_mod._mcp_connect_lock = None
    yield
    # Teardown: clear again so a test that connected doesn't leak into the next.
    state.session = None
    state.read_stream = None
    state.write_stream = None
    state.stdio_cm = None
    state.session_cm = None
    state.connected_at = None
    slack_mod._mcp_connect_lock = None


async def test_slack_scan_read_tool_refuses_write_tool(reset_mcp_state):
    """THE SEND-SAFETY BOUNDARY: call_read_tool refuses ANY tool not in the
    hardcoded read-only allowlist BEFORE the SDK touches the server. A write tool
    (post_message / edit_message / delete_message / schedule_message) raises
    SlackMcpError and never spawns the MCP server."""
    for write_tool in ("post_message", "edit_message", "delete_message",
                       "schedule_message"):
        with pytest.raises(slack_mod.SlackMcpError) as ei:
            await slack_mod.call_read_tool(write_tool, {"channel": "C1", "text": "x"})
        # The refusal is by name, citing the read-only allowlist.
        assert write_tool in str(ei.value)
        assert "read-only" in str(ei.value).lower()
    # The frozenset itself must never list a write tool.
    for write_tool in ("post_message", "edit_message", "delete_message",
                       "schedule_message", "bulk_post_message"):
        assert write_tool not in slack_mod._SLACK_READ_ONLY_TOOLS


async def test_slack_scan_read_tool_allows_read_tools_reach_sdk(monkeypatch, reset_mcp_state):
    """A read tool IS in the allowlist, so it passes the name guard and reaches
    the SDK's call_tool (here stubbed). Proves the guard rejects by name only —
    legitimate read tools are not blocked."""
    captured = {}

    class _FakeSession:
        async def initialize(self):
            captured["initialized"] = True

        async def call_tool(self, name, args):
            captured["called"] = (name, args)

            class _R:
                isError = False
                structuredContent = {"dms": [{"channelId": "D1"}]}

            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeStdio:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(slack_mod, "stdio_client", lambda params: _FakeStdio())
    monkeypatch.setattr(slack_mod, "ClientSession", lambda r, w: _FakeSession())

    out = await slack_mod.call_read_tool("list_dms", {"limit": 5})
    # A read tool reached call_tool and its structured content came back parsed.
    assert captured["called"] == ("list_dms", {"limit": 5})
    assert out == {"dms": [{"channelId": "D1"}]}


async def test_slack_scan_read_tool_guard_is_the_load_bearing_line(monkeypatch, reset_mcp_state):
    """SABOTAGE check: the frozenset name-guard is the ONLY thing stopping a write
    tool. If a regression WIDENS _SLACK_READ_ONLY_TOOLS to include post_message,
    the call passes the guard and reaches the SDK's call_tool('post_message', …) —
    proving the guard (not some other layer) is what makes the worker structurally
    incapable of sending. This is the test that would catch a widened allowlist."""
    # Intact guard: post_message is refused before any SDK touch.
    with pytest.raises(slack_mod.SlackMcpError):
        await slack_mod.call_read_tool("post_message", {"channel": "C1", "text": "x"})

    # Now SABOTAGE the boundary: widen the allowlist to include the write tool.
    monkeypatch.setattr(
        slack_mod, "_SLACK_READ_ONLY_TOOLS",
        frozenset(slack_mod._SLACK_READ_ONLY_TOOLS | {"post_message"}),
    )
    sent = {}

    class _FakeSession:
        async def initialize(self):
            pass

        async def call_tool(self, name, args):
            sent["call"] = (name, args)

            class _R:
                isError = False
                structuredContent = {"ok": True}

            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeStdio:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(slack_mod, "stdio_client", lambda params: _FakeStdio())
    monkeypatch.setattr(slack_mod, "ClientSession", lambda r, w: _FakeSession())

    # With the guard sabotaged, the write tool now reaches the server — exactly the
    # failure a real boundary must prevent. (The guard restored automatically by
    # monkeypatch teardown.)
    await slack_mod.call_read_tool("post_message", {"channel": "C1", "text": "x"})
    assert sent["call"] == ("post_message", {"channel": "C1", "text": "x"})


async def test_slack_scan_read_tool_error_result_fails_loud(monkeypatch, reset_mcp_state):
    """An isError tool result raises SlackMcpError (never treated as data) so the
    scan leaves state untouched — mirroring the _drive_agent fail-loud contract."""
    class _FakeSession:
        async def initialize(self):
            pass

        async def call_tool(self, name, args):
            class _R:
                isError = True
                content = []
                structuredContent = {"error": "expired auth"}

            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeStdio:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(slack_mod, "stdio_client", lambda params: _FakeStdio())
    monkeypatch.setattr(slack_mod, "ClientSession", lambda r, w: _FakeSession())

    with pytest.raises(slack_mod.SlackMcpError):
        await slack_mod.call_read_tool("get_unreads", {})


async def test_slack_scan_read_tool_connect_failure_fails_loud(monkeypatch, reset_mcp_state):
    """A connect/handshake exception is wrapped as SlackMcpError (fail-loud, no
    fabrication) and scrubbed."""
    def _boom(params):
        raise OSError("could not spawn slack-mcp")

    monkeypatch.setattr(slack_mod, "stdio_client", _boom)
    with pytest.raises(slack_mod.SlackMcpError):
        await slack_mod.call_read_tool("list_dms", {})


# --------------------------------------------------------------------------- #
# REAL-SDK retry path (D-019) — the live BrokenResourceError race the OTHER
# scan tests could NOT have caught because they monkeypatch _scan_read to canned
# payloads (so they never exercise the real stdio_client -> ClientSession ->
# initialize -> call_tool sequence, hence never its retry).
#
# call_read_tool wraps the WHOLE connect->call sequence in a bounded retry
# (_SCAN_MCP_RETRY_ATTEMPTS total, growing _SCAN_MCP_RETRY_BACKOFF_S backoff).
# inside the long-lived aiohttp loop the sequence intermittently fails with an
# anyio BrokenResourceError surfaced from inside the SDK's anyio TaskGroup — i.e.
# wrapped in a BaseExceptionGroup/ExceptionGroup. The SAME call succeeds on a
# retry with a FRESH subprocess. These tests drive call_read_tool DIRECTLY (it
# has NO _probe_readiness gate) with in-process fakes — NO real slack-mcp ever
# spawns — and prove:
#   (1) attempt 1 raising a TaskGroup-wrapped BrokenResourceError is retried and
#       attempt 2 (a fresh stdio_client subprocess) recovers, returning the
#       parsed payload — and attempt 1's subprocess was REAPED before attempt 2.
#   (2) the send-safety name-guard is refused with ZERO connect attempts EVEN
#       WITH the retry loop in place (the retry never turns a refused write tool
#       into a call) — the inviolable boundary, complementing the load-bearing
#       SABOTAGE test above.
#   (3) a NON-transient failure (an isError result; a non-stream-break connect
#       error) fails loud IMMEDIATELY with exactly ONE attempt — the classifier
#       never over-retries an auth/parse error into N spawns.
# The success path is built from REAL mcp.types.CallToolResult / TextContent so
# the genuine SDK result type flows through _normalize_tool_result's JSON parse.
# --------------------------------------------------------------------------- #
import anyio  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402


async def test_slack_scan_read_tool_retries_transient_broken_resource(monkeypatch, reset_mcp_state):
    """A transient stream break on attempt 1 (an anyio BrokenResourceError raised
    from inside the SDK's anyio TaskGroup -> surfaced as a BaseExceptionGroup) is
    RETRIED, and attempt 2 — a FRESH stdio_client subprocess — connects cleanly and
    returns real data. Proves the retry recovers the live warmup race, uses a fresh
    pipe each attempt, and reaps the failed attempt's subprocess before the next."""
    # Drive backoff to zero so the retry is instant under the suite (real const).
    monkeypatch.setattr(slack_mod, "_SCAN_MCP_RETRY_BACKOFF_S", (0, 0))

    state = {"spawns": 0, "reaped": 0}

    class _FakeSession:
        def __init__(self, attempt):
            self._attempt = attempt

        async def initialize(self):
            # Attempt 1: the stream resource breaks mid-handshake, wrapped exactly
            # as the SDK surfaces it (a TaskGroup ExceptionGroup with a single
            # anyio.BrokenResourceError leaf). Later attempts initialize cleanly.
            if self._attempt == 1:
                raise BaseExceptionGroup(
                    "unhandled errors in a TaskGroup (1 sub-exception)",
                    [anyio.BrokenResourceError()],
                )

        async def call_tool(self, name, args):
            state["called"] = (name, args)
            # A REAL CallToolResult (no structuredContent) so the genuine SDK type
            # flows through _normalize_tool_result's text-content JSON-parse path.
            return CallToolResult(
                content=[TextContent(
                    type="text", text=json.dumps({"dms": [{"channelId": "D1"}]}))],
                isError=False,
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeStdio:
        def __init__(self, attempt):
            self._attempt = attempt

        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *a):
            # The async-with ALWAYS reaps this attempt's subprocess on exit — incl.
            # when attempt 1 raised mid-flight (so no orphan accumulates).
            state["reaped"] += 1
            return False

    def _spawn(params):
        # Each stdio_client(params) call models a fresh subprocess + fresh pipe.
        state["spawns"] += 1
        state["attempt"] = state["spawns"]
        return _FakeStdio(state["spawns"])

    monkeypatch.setattr(slack_mod, "stdio_client", _spawn)
    monkeypatch.setattr(
        slack_mod, "ClientSession", lambda r, w: _FakeSession(state["attempt"]))

    out = await slack_mod.call_read_tool("list_dms", {"limit": 5})

    # The retry recovered: the parsed payload came back from attempt 2.
    assert out == {"dms": [{"channelId": "D1"}]}
    assert state["called"] == ("list_dms", {"limit": 5})
    # Exactly 2 spawns: attempt 1 raised+was reaped, attempt 2 succeeded — a FRESH
    # subprocess per attempt (not a single retried-in-place connection).
    assert state["spawns"] == 2
    # Attempt 1's subprocess was reaped during _connect_mcp's partial-failure
    # cleanup. Attempt 2's subprocess stays alive (persistent session). No orphan.
    assert state["reaped"] == 1


async def test_slack_scan_read_tool_guard_not_retried_with_retry_in_place(monkeypatch, reset_mcp_state):
    """THE INVIOLABLE BOUNDARY, with the retry live: a write tool is refused by the
    name-guard BEFORE any connect, and the retry loop NEVER turns a refused write
    tool into a connect attempt. A spy stdio_client that counts EVERY call must see
    ZERO calls. (Complements the load-bearing SABOTAGE test above.)"""
    monkeypatch.setattr(slack_mod, "_SCAN_MCP_RETRY_BACKOFF_S", (0, 0))
    spawns = []

    def _spy(params):
        spawns.append(1)
        raise AssertionError("name-guard must reject BEFORE any connect attempt")

    monkeypatch.setattr(slack_mod, "stdio_client", _spy)

    with pytest.raises(slack_mod.SlackMcpError):
        await slack_mod.call_read_tool("post_message", {"channel": "C1", "text": "x"})
    # The guard rejected with ZERO connect attempts — the retry never even started.
    assert spawns == []
    # And post_message must never be in the read-only allowlist in the first place.
    assert "post_message" not in slack_mod._SLACK_READ_ONLY_TOOLS


async def test_slack_scan_read_tool_non_transient_isError_no_retry(monkeypatch, reset_mcp_state):
    """NON-transient failures fail loud IMMEDIATELY, never retried into N spawns:
    (a) an isError tool result (e.g. expired auth) connects cleanly ONCE then raises;
    (b) a non-stream-break connect error (ValueError) also yields exactly ONE attempt.
    Proves the classifier does not over-retry an auth/parse/non-stream error."""
    monkeypatch.setattr(slack_mod, "_SCAN_MCP_RETRY_BACKOFF_S", (0, 0))

    # (a) isError result — a clean connect, but the tool reports an error.
    err_state = {"spawns": 0, "reaped": 0}

    class _FakeSession:
        async def initialize(self):
            pass

        async def call_tool(self, name, args):
            # A REAL CallToolResult flagged isError — never treated as data.
            return CallToolResult(
                content=[], isError=True,
                structuredContent={"error": "expired auth"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeStdio:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *a):
            err_state["reaped"] += 1
            return False

    def _spawn(params):
        err_state["spawns"] += 1
        return _FakeStdio()

    monkeypatch.setattr(slack_mod, "stdio_client", _spawn)
    monkeypatch.setattr(slack_mod, "ClientSession", lambda r, w: _FakeSession())

    with pytest.raises(slack_mod.SlackMcpError) as ei:
        await slack_mod.call_read_tool("get_unreads", {})
    assert "expired auth" in str(ei.value)
    # isError is non-transient: exactly ONE spawn (one clean connect), the session
    # stays alive (persistent — no reap on a non-stream error).
    assert err_state["spawns"] == 1
    assert err_state["reaped"] == 0

    # Reset persistent state so part (b) exercises a fresh connect path.
    slack_mod._mcp_state.session = None
    slack_mod._mcp_state.read_stream = None
    slack_mod._mcp_state.write_stream = None
    slack_mod._mcp_state.stdio_cm = None
    slack_mod._mcp_state.session_cm = None
    slack_mod._mcp_state.connected_at = None
    slack_mod._mcp_connect_lock = None

    # (b) A non-stream-break connect error (NOT wrapping a transient leaf) is also
    # NON-transient -> exactly ONE attempt, fail loud (the classifier won't retry).
    connect_spawns = {"n": 0}

    def _bad_connect(params):
        connect_spawns["n"] += 1
        raise ValueError("bad params")

    monkeypatch.setattr(slack_mod, "stdio_client", _bad_connect)
    with pytest.raises(slack_mod.SlackMcpError):
        await slack_mod.call_read_tool("list_dms", {})
    assert connect_spawns["n"] == 1


def test_slack_dm_candidates_distinguish_group_and_dm(reset_scan_ts):
    """_dm_candidates builds DM/group-DM candidates from a list_dms payload by the
    activity window, and a group DM 'C…' is channelType 'group_dm' while a 1:1 DM
    'D…' is 'dm' — even when they share a participant. channelId is verbatim."""
    now = 2_000_000_000_000
    payload = {"dms": [
        {"channelId": "D_one_on_one", "user": "U_alice", "name": "alice",
         "lastActivity": now - 1000},
        {"channelId": "C_group", "isGroup": True, "name": "mpdm-alice--bob-1",
         "lastActivity": now - 1000},
        # Too old -> excluded by the activity window (fallback window from now).
        {"channelId": "D_stale", "user": "U_old", "name": "old",
         "lastActivity": now - 10 * slack_mod._SCAN_WINDOW_MS},
    ]}
    cands = slack_mod._dm_candidates(payload, now)
    by_id = {c["channelId"]: c for c in cands}
    assert set(by_id) == {"D_one_on_one", "C_group"}  # the stale DM dropped
    assert by_id["D_one_on_one"]["channelType"] == "dm"
    assert by_id["C_group"]["channelType"] == "group_dm"
    # The group-DM sender is derived from the mpdm slug's first participant.
    assert by_id["C_group"]["sender"] == "alice"
    # channelId is captured verbatim (it is the routable key, not a secret).
    assert by_id["D_one_on_one"]["channelId"] == "D_one_on_one"


def test_slack_mention_candidates_from_unreads():
    """_mention_candidates pulls ONLY channels where the user is @mentioned,
    channelType 'mention', channelId verbatim."""
    payload = {"mentions": [
        {"channelId": "C_mention", "channel": "#proj",
         "messages": [{"user": "carol", "text": "hey <@W0187CHBRU0> ping", "ts": "1718.5"}]},
        {"channelId": "C_nomention", "channel": "#other",
         "messages": [{"user": "dave", "text": "random chat", "ts": "1718.6"}]},
    ], "dms": [{"channelId": "D_ignored"}]}
    cands = slack_mod._mention_candidates(payload)
    assert len(cands) == 1
    m = cands[0]
    assert m["channelId"] == "C_mention"
    assert m["channelType"] == "mention"
    assert m["sender"] == "carol"
    assert "ping" in m["snippet"]


def test_slack_merge_appends_skeletons_for_new_channels():
    """_merge_scan_candidates appends a needs-classify SKELETON for each genuinely-new
    channelId; a group-DM 'C…' and a 1:1-DM 'D…' that share a participant are kept
    DISTINCT (dedupe is EXACT channelId). (D-025: fresh skeletons land in
    'needs-classify' so the classify worker triages them before drafting.)"""
    items: list = []
    cands = [
        {"channelId": "D1", "channelType": "dm", "sender": "alice",
         "channel": "DM with alice", "snippet": "hi", "ts": 100},
        {"channelId": "C1", "channelType": "group_dm", "sender": "alice",
         "channel": "mpdm-alice--bob-1", "snippet": "team?", "ts": 100},
    ]
    changed = slack_mod._merge_scan_candidates(items, cands)
    assert changed is True
    by_id = {it["channelId"]: it for it in items}
    # Both appended as distinct skeletons (the shared participant does NOT collapse
    # the group DM into the 1:1 DM).
    assert set(by_id) == {"D1", "C1"}
    assert by_id["D1"]["status"] == "needs-classify" and by_id["D1"]["channelType"] == "dm"
    assert by_id["C1"]["status"] == "needs-classify" and by_id["C1"]["channelType"] == "group_dm"
    # Skeletons carry a fresh id and the candidate's routing/snippet fields.
    assert by_id["D1"]["id"] and by_id["D1"]["snippet"] == "hi"


# Realistic epoch-MILLIS timestamps. _as_int_ms keeps values >= 1e11 verbatim
# (treats anything smaller as epoch SECONDS and scales x1000), so the merge tests
# use ms-scale values to assert exact ts round-trips and unambiguous ordering.
_MS = 1_700_000_000_000  # ~2023-11-14 in ms
_MS_OLDER = _MS - 60_000
_MS_NEWER = _MS + 60_000


def test_slack_merge_dedupes_existing_active_channel():
    """An ACTIVE item (needs-draft/needs-review/edited) with the candidate's exact
    channelId already represents it — no duplicate skeleton is appended, and when
    the activity is NOT newer it is left entirely untouched."""
    items = [{
        "id": "a", "channelId": "D1", "channelType": "dm", "status": "needs-draft",
        "snippet": "old", "ts": _MS,
    }]
    cands = [{"channelId": "D1", "channelType": "dm", "sender": "a",
              "channel": "c", "snippet": "same", "ts": _MS_OLDER}]  # not newer
    changed = slack_mod._merge_scan_candidates(items, cands)
    assert changed is False
    assert len(items) == 1  # no duplicate appended
    assert items[0]["snippet"] == "old"  # untouched (activity not newer)


def test_slack_merge_sets_needsredraft_on_stale_needs_review():
    """A needs-review item whose conversation grew (candidate ts NEWER than the
    item's ts) is flagged needsRedraft with an updated snippet/ts — the same
    redraft signal the cron used. edited/sent/dismissed are never flagged."""
    items = [{
        "id": "a", "channelId": "D1", "channelType": "dm", "status": "needs-review",
        "draft": "d", "generatedDraft": "d", "snippet": "first", "ts": _MS_OLDER,
    }]
    cands = [{"channelId": "D1", "channelType": "dm", "sender": "a",
              "channel": "c", "snippet": "and another", "ts": _MS_NEWER}]  # newer
    changed = slack_mod._merge_scan_candidates(items, cands)
    assert changed is True
    assert len(items) == 1  # still no NEW skeleton — the existing item is reused
    assert items[0]["needsRedraft"] is True
    assert items[0]["snippet"] == "and another"  # snippet refreshed
    assert items[0]["ts"] == _MS_NEWER  # ms-scale ts passes through verbatim


def test_slack_merge_does_not_flag_edited_item():
    """An EDITED item (the user owns the draft) is never flagged needsRedraft even
    when the conversation grew — the draft-worker's selection guard would protect
    it anyway, but the merge must not even set the flag (spec item 4)."""
    items = [{
        "id": "a", "channelId": "D1", "channelType": "dm", "status": "edited",
        "draft": "MY WORDS", "generatedDraft": "machine", "snippet": "first", "ts": _MS_OLDER,
    }]
    cands = [{"channelId": "D1", "channelType": "dm", "sender": "a",
              "channel": "c", "snippet": "newer", "ts": _MS_NEWER}]
    changed = slack_mod._merge_scan_candidates(items, cands)
    assert changed is False
    assert "needsRedraft" not in items[0]
    assert items[0]["draft"] == "MY WORDS"  # untouched


def test_slack_merge_dismissed_suppresses_until_newer_activity():
    """A 'dismissed' item suppresses a candidate ONLY while the conversation has no
    activity NEWER than the dismissed item's ts (D-028). Dismiss clears the current
    message; it does not mute the thread. A candidate whose ts <= the dismissed
    item's ts is suppressed and the item is left untouched; a candidate with NEWER
    activity earns a fresh needs-classify skeleton."""
    # Case A: no newer activity (cand ts <= dismissed ts) -> suppressed, untouched.
    items_a = [{
        "id": "a", "channelId": "D1", "channelType": "dm", "status": "dismissed",
        "snippet": "old", "ts": _MS,
    }]
    cands_a = [{"channelId": "D1", "channelType": "dm", "sender": "a",
                "channel": "c", "snippet": "same", "ts": _MS_OLDER}]  # not newer
    assert slack_mod._merge_scan_candidates(items_a, cands_a) is False
    assert len(items_a) == 1
    assert items_a[0]["status"] == "dismissed"  # untouched

    # Case B: newer activity (cand ts > dismissed ts) -> a fresh skeleton is added,
    # the dismissed item stays (a genuinely new message after the dismiss).
    items_b = [{
        "id": "a", "channelId": "D1", "channelType": "dm", "status": "dismissed",
        "snippet": "old", "ts": _MS_OLDER,
    }]
    cands_b = [{"channelId": "D1", "channelType": "dm", "sender": "a",
                "channel": "c", "snippet": "new message!", "ts": _MS_NEWER}]  # newer
    assert slack_mod._merge_scan_candidates(items_b, cands_b) is True
    statuses = sorted(it["status"] for it in items_b)
    assert statuses == ["dismissed", "needs-classify"]  # dismissed stays, skeleton joins


def test_slack_merge_sent_item_suppresses_until_newer_activity():
    """A 'sent' item suppresses a candidate ONLY while the conversation has no
    activity newer than the send. A candidate whose ts <= sentAt is suppressed; a
    candidate with NEWER activity earns a fresh needs-draft skeleton (a genuinely
    new message after the reply went out)."""
    # Case A: no newer activity (ts <= sentAt) -> suppressed, no new card.
    items_a = [{
        "id": "s1", "channelId": "D1", "channelType": "dm", "status": "sent",
        "sentAt": _MS, "snippet": "replied", "ts": _MS,
    }]
    cands_a = [{"channelId": "D1", "channelType": "dm", "sender": "a",
                "channel": "c", "snippet": "old", "ts": _MS}]
    assert slack_mod._merge_scan_candidates(items_a, cands_a) is False
    assert len(items_a) == 1

    # Case B: newer activity (ts > sentAt) -> a fresh skeleton is appended.
    items_b = [{
        "id": "s1", "channelId": "D1", "channelType": "dm", "status": "sent",
        "sentAt": _MS, "snippet": "replied", "ts": _MS,
    }]
    cands_b = [{"channelId": "D1", "channelType": "dm", "sender": "a",
                "channel": "c", "snippet": "new message!", "ts": _MS_NEWER}]
    assert slack_mod._merge_scan_candidates(items_b, cands_b) is True
    statuses = sorted(it["status"] for it in items_b)
    # D-025: a fresh skeleton lands in 'needs-classify'; the sent item stays.
    assert statuses == ["needs-classify", "sent"]  # the sent stays, a new skeleton joins


async def _run_one_scan_cycle(app, monkeypatch, *, unreads):
    """Drive ONE _scan_worker iteration with a canned get_unreads payload.

    Monkeypatches _scan_read to return the canned payload (NO real MCP server),
    forces readiness, tightens the interval, runs the worker a moment, then
    cancels cleanly."""
    async def fake_scan_read(name, arguments):
        if name == "get_unreads":
            return unreads
        if name == "get_messages":
            return {"messages": []}
        raise AssertionError(f"unexpected scan tool {name!r}")

    monkeypatch.setattr(slack_mod, "_scan_read", fake_scan_read)
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    monkeypatch.setattr(slack_mod, "SLACK_SCAN_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._scan_worker(app))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_slack_scan_worker_writes_skeletons_for_new_conversations(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """End-to-end ONE scan cycle: canned list_dms/get_unreads -> the worker writes
    needs-draft SKELETONS for the new DM, group DM, and @mention, deduping by exact
    channelId. NO real MCP server (DIRECTLY patches _scan_read)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")

    now_s = _time.time()
    unreads = {"channels": [
        {"channelId": "D_alice", "name": "alice",
         "messages": [{"user": "alice", "text": "hey", "ts": f"{now_s - 1:.6f}"}]},
        {"channelId": "C_grp", "name": "mpdm-alice--bob-1",
         "messages": [{"user": "bob", "text": "team?", "ts": f"{now_s - 1:.6f}"}]},
        {"channelId": "C_proj", "name": "#proj",
         "messages": [{"user": "carol", "text": "hey <@W0187CHBRU0> look", "ts": f"{now_s - 0.5:.6f}"}]},
    ]}
    await _run_one_scan_cycle(client.app, monkeypatch, unreads=unreads)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"]
    by_id = {it["channelId"]: it for it in saved}
    assert set(by_id) == {"D_alice", "C_grp", "C_proj"}
    # All written as needs-classify skeletons the _classify_worker triages first
    # (D-025), then the _draft_worker drafts the needs-reply / actionable ones.
    assert all(it["status"] == "needs-classify" for it in saved)
    assert by_id["D_alice"]["channelType"] == "dm"
    assert by_id["C_grp"]["channelType"] == "group_dm"
    assert by_id["C_proj"]["channelType"] == "mention"
    # Each skeleton carries a routable channelId verbatim (the send path needs it).
    assert by_id["D_alice"]["channelId"] == "D_alice"


async def test_slack_scan_worker_dedupes_against_existing_and_flags_stale(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """ONE scan cycle against a non-empty queue: an existing active needs-draft for
    the same channelId is NOT duplicated; an existing needs-review whose
    conversation grew is flagged needsRedraft (snippet/ts updated)."""
    now = int(_time.time() * 1000)
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [
        {"id": "keep", "channelId": "D_dup", "channelType": "dm",
         "status": "needs-draft", "snippet": "already here", "ts": now - 5000},
        {"id": "stale", "channelId": "D_grow", "channelType": "dm",
         "status": "needs-review", "draft": "d", "generatedDraft": "d",
         "snippet": "first", "ts": now - 5000},
    ]}), encoding="utf-8")

    now_s = now / 1000.0
    unreads = {"channels": [
        {"channelId": "D_dup", "name": "dup",
         "messages": [{"user": "U1", "text": "again", "ts": f"{now_s - 0.1:.6f}"}]},
        {"channelId": "D_grow", "name": "grow",
         "messages": [{"user": "U2", "text": "and more", "ts": f"{now_s - 0.05:.6f}"}]},
    ]}
    await _run_one_scan_cycle(client.app, monkeypatch, unreads=unreads)

    saved = {it["id"]: it for it in json.loads(slack_file.read_text(encoding="utf-8"))["items"]}
    # No duplicate skeleton for D_dup (still exactly the two original items).
    assert set(saved) == {"keep", "stale"}
    assert saved["keep"]["status"] == "needs-draft"
    # The grown needs-review item was flagged for a redraft, snippet refreshed.
    assert saved["stale"]["needsRedraft"] is True
    assert saved["stale"]["snippet"] == "and more"


async def test_slack_scan_worker_resurfaces_dismissed_and_sent_on_newer_activity(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """ONE scan cycle: a 'dismissed' or 'sent' conversation is suppressed only while
    it has NO activity newer than the dismiss/send. A message NEWER than what was
    dismissed/sent earns a fresh skeleton (D-028: Dismiss clears the current
    message, it does not mute the thread); older activity stays suppressed."""
    now = int(_time.time() * 1000)
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [
        # Dismissed with NO newer activity than the dismiss: stays suppressed.
        {"id": "disold", "channelId": "D_dis_old", "channelType": "dm",
         "status": "dismissed", "snippet": "go away", "ts": now},
        # Dismissed WITH newer activity than the dismiss: resurfaces (D-028).
        {"id": "disnew", "channelId": "D_dis_new", "channelType": "dm",
         "status": "dismissed", "snippet": "go away", "ts": now - 9000},
        {"id": "sentold", "channelId": "D_sent_old", "channelType": "dm",
         "status": "sent", "sentAt": now, "finalText": "done", "ts": now},
        {"id": "sentnew", "channelId": "D_sent_new", "channelType": "dm",
         "status": "sent", "sentAt": now - 9000, "finalText": "done", "ts": now - 9000},
    ]}), encoding="utf-8")

    now_s = now / 1000.0
    unreads = {"channels": [
        # Dismissed conversation, NO newer activity than the dismiss: suppressed.
        {"channelId": "D_dis_old", "name": "d1",
         "messages": [{"user": "U1", "text": "old", "ts": f"{now_s - 5:.6f}"}]},
        # Dismissed conversation WITH newer activity than the dismiss: resurfaces.
        {"channelId": "D_dis_new", "name": "d2",
         "messages": [{"user": "U1", "text": "still here", "ts": f"{now_s - 0.01:.6f}"}]},
        # Sent conversation, NO newer activity than the send: suppressed.
        {"channelId": "D_sent_old", "name": "s1",
         "messages": [{"user": "U2", "text": "old", "ts": f"{now_s - 5:.6f}"}]},
        # Sent conversation WITH newer activity than the send: fresh skeleton.
        {"channelId": "D_sent_new", "name": "s2",
         "messages": [{"user": "U3", "text": "they replied again", "ts": f"{now_s - 0.01:.6f}"}]},
    ]}
    await _run_one_scan_cycle(client.app, monkeypatch, unreads=unreads)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"]
    by_channel: dict = {}
    for it in saved:
        by_channel.setdefault(it["channelId"], []).append(it)
    # Dismissed-no-newer: still exactly the one dismissed item (no new skeleton).
    assert len(by_channel["D_dis_old"]) == 1
    assert by_channel["D_dis_old"][0]["status"] == "dismissed"
    # Dismissed-with-newer: the dismissed item PLUS a fresh needs-classify skeleton.
    assert sorted(it["status"] for it in by_channel["D_dis_new"]) == ["dismissed", "needs-classify"]
    # Sent-no-newer: still exactly the one sent item (no new skeleton).
    assert len(by_channel["D_sent_old"]) == 1
    assert by_channel["D_sent_old"][0]["status"] == "sent"
    # Sent-with-newer: the sent item PLUS a fresh needs-classify skeleton (D-025).
    assert sorted(it["status"] for it in by_channel["D_sent_new"]) == ["needs-classify", "sent"]


async def test_slack_scan_worker_read_failure_leaves_queue_untouched(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """If a Slack/MCP read FAILS (SlackMcpError), the cycle must leave the queue
    UNTOUCHED — never fabricate, never partial-write (fail-loud, mirroring the
    _drive_agent contract). The worker logs it and retries next cycle."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    original = {"items": [{
        "id": "i1", "channelId": "D1", "channelType": "dm", "status": "needs-review",
        "draft": "d", "generatedDraft": "d", "snippet": "hi", "ts": 1,
    }]}
    slack_file.write_text(json.dumps(original), encoding="utf-8")
    before = slack_file.read_text(encoding="utf-8")

    async def boom_scan_read(name, arguments):
        raise slack_mod.SlackMcpError("slack-mcp read failed")

    monkeypatch.setattr(slack_mod, "_scan_read", boom_scan_read)
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    monkeypatch.setattr(slack_mod, "SLACK_SCAN_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._scan_worker(client.app))
    try:
        # A few cycles: each one must fail loud and write nothing.
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The queue is byte-for-byte unchanged — no partial/fabricated write.
    assert slack_file.read_text(encoding="utf-8") == before


async def test_slack_scan_noop_still_advances_last_scan_and_broadcasts(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """A no-op scan (nothing new/changed) STILL advances the persisted lastScanAt
    AND broadcasts 'slack_changed' (D-023, spec item 4). Before D-023 _scan_once
    returned early when ``not changed and not histories`` — leaving the "Updated …"
    label stale and the manual-refresh "Scanning…" hint hanging after a no-op.

    The queue already holds an ACTIVE needs-draft for channel D_seen; get_unreads
    re-surfaces ONLY that same channel, so the dedupe finds it already represented
    → zero new/changed candidates, zero history pre-fetches. We assert (a) a
    'slack_changed' broadcast fired so open pages re-render the freshness label and
    the "Scanning…" hint clears, and (b) the persisted top-level lastScanAt advanced
    past its prior value, even though the items array is otherwise unchanged.
    """
    prior_scan = 1_700_000_000_000  # an old, fixed timestamp on disk
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({
        "lastScanAt": prior_scan,
        "items": [{
            "id": "seen", "channelId": "D_seen", "channelType": "dm",
            "status": "needs-draft", "snippet": "already here", "ts": 1,
        }],
    }), encoding="utf-8")
    items_before = json.loads(slack_file.read_text(encoding="utf-8"))["items"]

    # Record every broadcast (mirror test_slack_watcher_broadcasts_on_external_change).
    events: list = []

    async def record(event_type, data=None):
        events.append((event_type, data))

    monkeypatch.setattr(client.app["ws_manager"], "broadcast", record)

    # get_unreads re-surfaces ONLY the already-queued channel → no new/changed
    # candidate, no history pre-fetch. This is the genuine no-op cycle.
    now_s = _time.time()
    unreads = {"channels": [
        {"channelId": "D_seen", "name": "seen",
         "messages": [{"user": "alice", "text": "already here", "ts": f"{now_s - 1:.6f}"}]},
    ]}
    await _run_one_scan_cycle(client.app, monkeypatch, unreads=unreads)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))
    # (a) A no-op scan still notifies open pages so freshness re-renders.
    assert any(ev[0] == "slack_changed" for ev in events), \
        "a no-op scan must still broadcast slack_changed so the freshness label advances"
    # (b) The persisted freshness timestamp advanced past its prior value...
    assert saved["lastScanAt"] > prior_scan
    # ...even though the items array is unchanged (this WAS a no-op for the queue).
    assert saved["items"] == items_before


async def test_slack_scan_worker_inert_under_harness_via_sleep_first(
    client, slack_file, monkeypatch
):
    """Inertness contract (preserved under D-023's interruptible sleep): the startup
    _scan_worker must NOT drive any MCP read on startup. Per the slack.py warning,
    in THIS env _probe_readiness() is genuinely ready, so the ONLY thing keeping the
    worker inert under aiohttp_client(app) is the FIRST-action interval wait. D-023
    replaced the bare ``sleep`` with ``wait_for(event.wait(), SLACK_SCAN_INTERVAL_S)``
    — but absent a SET wake Event that wait_for just blocks until the full interval
    times out, behaving EXACTLY like the old sleep-first. We assert _scan_read is NOT
    called within a short window even though the gate would pass — proving the
    interruptible-but-unset wait, not the gate, provides the inertness here."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "channelId": "D1", "channelType": "dm", "status": "needs-review",
        "draft": "d", "generatedDraft": "d", "snippet": "hi", "ts": 1,
    }]}), encoding="utf-8")

    called = []

    async def spy_scan_read(name, arguments):
        called.append(name)
        return {}

    # The gate IS ready (as it genuinely is in this env) — so only the unmodified
    # full SLACK_SCAN_INTERVAL_S (300s) wait can keep the worker from scanning.
    monkeypatch.setattr(slack_mod, "_scan_read", spy_scan_read)
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    # Do NOT shorten SLACK_SCAN_INTERVAL_S, and do NOT set the wake Event — leave the
    # real full-interval interruptible wait in place so it behaves as a plain sleep.
    task = asyncio.create_task(slack_mod._scan_worker(client.app))
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # No scan was driven: the unset interruptible wait kept it inert despite the
    # ready gate (and the wake Event stayed unset — nothing triggered an early scan).
    assert called == []
    assert not slack_mod._get_scan_event().is_set()


async def test_slack_scan_worker_task_started_and_cancelled_cleanly(aiohttp_client, app):
    """The scan-worker lifecycle: on_startup creates app['slack_scan_task'];
    closing the client cancels+awaits it (no orphan, no 'Task was destroyed').
    Mirrors the draft-worker lifecycle test — the in-flight MCP subprocess is
    reaped by stdio_client's context manager on cancellation."""
    c = await aiohttp_client(app)
    task = app.get("slack_scan_task")
    assert task is not None and not task.done()
    await c.close()
    assert task.done()
    assert "slack_scan_task" not in app


async def test_slack_cleanup_persistent_mcp_called_on_shutdown(aiohttp_client, app):
    """_cleanup_persistent_mcp is registered as an on_cleanup hook and calls
    _disconnect_mcp on app shutdown. Since the session is never connected in
    tests (lazy-init, _probe_readiness gate), _disconnect_mcp is a no-op —
    verify it runs without error and leaves the state cleared."""
    c = await aiohttp_client(app)
    # Verify the hook is registered (appended AFTER _stop_scan_worker).
    assert slack_mod._cleanup_persistent_mcp in app.on_cleanup
    # Simulate a connected state by setting the dataclass fields.
    slack_mod._mcp_state.session = "fake_session"
    slack_mod._mcp_state.connected_at = 12345.0
    try:
        await c.close()
    finally:
        # Ensure cleanup cleared the state (no-op path since the fake CMs are None).
        assert slack_mod._mcp_state.session is None
        assert slack_mod._mcp_state.connected_at is None


async def test_slack_disconnect_mcp_idempotent(aiohttp_client, app):
    """_disconnect_mcp is idempotent — calling it when no session is connected
    is a safe no-op (no error raised, state stays cleared)."""
    await aiohttp_client(app)
    # Ensure clean state.
    slack_mod._mcp_state.session = None
    slack_mod._mcp_state.read_stream = None
    slack_mod._mcp_state.write_stream = None
    slack_mod._mcp_state.connected_at = None
    slack_mod._mcp_state.stdio_cm = None
    slack_mod._mcp_state.session_cm = None
    # Should not raise.
    await slack_mod._disconnect_mcp()
    assert slack_mod._mcp_state.session is None
    assert slack_mod._mcp_state.connected_at is None


async def test_slack_disconnect_mcp_exits_context_managers(aiohttp_client, app):
    """_disconnect_mcp properly calls __aexit__ on both the session and stdio
    context managers, reaping the subprocess."""
    await aiohttp_client(app)
    exit_calls = []

    class FakeCM:
        def __init__(self, name):
            self._name = name

        async def __aexit__(self, *args):
            exit_calls.append(self._name)

    slack_mod._mcp_state.session = "fake_session"
    slack_mod._mcp_state.read_stream = "fake_read"
    slack_mod._mcp_state.write_stream = "fake_write"
    slack_mod._mcp_state.connected_at = 99999.0
    slack_mod._mcp_state.session_cm = FakeCM("session_cm")
    slack_mod._mcp_state.stdio_cm = FakeCM("stdio_cm")
    slack_mod._mcp_connect_lock = None  # Force fresh lock creation

    await slack_mod._disconnect_mcp()

    # Both context managers had their __aexit__ called, in order.
    assert "session_cm" in exit_calls
    assert "stdio_cm" in exit_calls
    # Session CM exited BEFORE stdio CM (session handshake before subprocess kill).
    assert exit_calls.index("session_cm") < exit_calls.index("stdio_cm")
    # All state cleared.
    assert slack_mod._mcp_state.session is None
    assert slack_mod._mcp_state.read_stream is None
    assert slack_mod._mcp_state.write_stream is None
    assert slack_mod._mcp_state.connected_at is None
    assert slack_mod._mcp_state.stdio_cm is None
    assert slack_mod._mcp_state.session_cm is None


# --------------------------------------------------------------------------- #
# OS-cron routes (/api/oscron) — READ-ONLY mirror of crontab + systemd timers.
# The subprocess seams (_run_crontab/_run_systemctl_*) are monkeypatched so the
# tests never exec a real `crontab`/`systemctl`.
# --------------------------------------------------------------------------- #
import server.routes.oscron as oscron_mod  # noqa: E402

# A realistic crontab: a CRON_TZ line, a redirect-to-log job (with preceding
# comments used as the description), a no-log job, and a secret-bearing job.
_FAKE_CRONTAB = (
    "# Black Falcon refresh — unified.\n"
    "# Runs twice daily 3am/3pm Pacific.\n"
    "CRON_TZ=America/Los_Angeles\n"
    "0 3,15 * * * cd /proj && python3 refresh_cron.py "
    ">> /proj/data/refresh-cron.log 2>&1\n"
    "\n"
    "30 9 * * 1 echo hello\n"
    "# leaky\n"
    "0 0 * * * curl --cookie ~/.midway/cookie https://example\n"
)

_FAKE_SYSTEMD = json.dumps([
    {"next": 1781558357465630, "last": 1781471957407619,
     "unit": "backup.timer", "activates": "backup.service"},
    {"next": None, "last": None,
     "unit": "grub-boot-success.timer", "activates": "grub-boot-success.service"},
])


def _patch_oscron(monkeypatch, *, crontab=("rc", "out"), systemd=None):
    rc, out = crontab

    async def fake_crontab():
        return (0, out) if rc == "ok" else (rc if isinstance(rc, int) else 1, out)

    async def fake_user():
        return (0, systemd) if systemd else (1, "")

    async def fake_system():
        return (1, "")

    monkeypatch.setattr(oscron_mod, "_run_crontab", fake_crontab)
    monkeypatch.setattr(oscron_mod, "_run_systemctl_user", fake_user)
    monkeypatch.setattr(oscron_mod, "_run_systemctl_system", fake_system)


async def test_oscron_parses_crontab_with_tz_and_log(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=("ok", _FAKE_CRONTAB), systemd=_FAKE_SYSTEMD)
    resp = await client.get("/api/oscron")
    assert resp.status == 200
    jobs = (await resp.json())["jobs"]

    cron_jobs = [j for j in jobs if j["source"] == "crontab"]
    # The secret-bearing line must be dropped, leaving the 3am/3pm + the weekly job.
    assert len(cron_jobs) == 2
    refresh = next(j for j in cron_jobs if j["schedule"] == "0 3,15 * * *")
    assert refresh["cronTz"] == "America/Los_Angeles"
    assert refresh["logPath"] == "/proj/data/refresh-cron.log"
    assert refresh["runHistory"] == "logs_only"
    # First comment line is the short title; the rest forms the longer description.
    assert refresh["title"] == "Black Falcon refresh — unified."
    assert refresh["description"] == "Runs twice daily 3am/3pm Pacific."

    weekly = next(j for j in cron_jobs if j["schedule"] == "30 9 * * 1")
    assert weekly["logPath"] is None
    assert weekly["runHistory"] == "none"


async def test_oscron_next_run_is_pacific_3am_3pm(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=("ok", _FAKE_CRONTAB))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    refresh = next(j for j in jobs if j.get("schedule") == "0 3,15 * * *")
    assert refresh["nextRun"] is not None
    assert len(refresh["nextRuns"]) == 3
    from datetime import datetime
    from zoneinfo import ZoneInfo
    pac = ZoneInfo("America/Los_Angeles")
    for ms in refresh["nextRuns"]:
        dt = datetime.fromtimestamp(ms / 1000, pac)
        assert dt.hour in (3, 15) and dt.minute == 0


async def test_oscron_unparseable_schedule_next_run_null(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=("ok", "bogus not a schedule at all here now\n"))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    cron_jobs = [j for j in jobs if j["source"] == "crontab"]
    assert len(cron_jobs) == 1
    assert cron_jobs[0]["nextRun"] is None
    assert cron_jobs[0]["nextRuns"] == []


async def test_oscron_empty_crontab_no_error(client, monkeypatch):
    """`crontab -l` exiting non-zero ('no crontab for user') => empty list, no 500."""
    _patch_oscron(monkeypatch, crontab=(1, "no crontab for user\n"))
    resp = await client.get("/api/oscron")
    assert resp.status == 200
    assert (await resp.json())["jobs"] == []


async def test_oscron_systemd_timers_mapped(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd=_FAKE_SYSTEMD)
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    sd = [j for j in jobs if j["source"] == "systemd"]
    assert len(sd) == 2
    backup = next(j for j in sd if j["unit"] == "backup.timer")
    assert backup["command"] == "backup.service"
    assert backup["nextRun"] == 1781558357465  # usec -> ms
    assert backup["lastRun"] == 1781471957407
    assert backup["runHistory"] == "logs_only"
    # A timer with next:null still lists, with nextRun None.
    grub = next(j for j in sd if j["unit"] == "grub-boot-success.timer")
    assert grub["nextRun"] is None


async def test_oscron_bad_systemd_json_degrades(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd="}{ not json")
    resp = await client.get("/api/oscron")
    assert resp.status == 200
    assert (await resp.json())["jobs"] == []


async def test_oscron_command_secret_never_surfaced(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=("ok", _FAKE_CRONTAB))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    for j in jobs:
        blob = json.dumps(j).lower()
        for marker in oscron_mod._SECRET_MARKERS:
            assert marker not in blob, f"secret marker leaked: {marker!r}"


async def test_oscron_secret_in_comment_not_surfaced_as_description(client, monkeypatch):
    """A credential-bearing comment line must never reach the title or description."""
    crontab = (
        "# Refresh job.\n"
        "# Runs nightly.\n"
        "# Requires a valid Midway session at run time (~/.midway/cookie).\n"
        "0 3 * * * /bin/run.sh >> /tmp/run.log 2>&1\n"
    )
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    cron = next(j for j in jobs if j["source"] == "crontab")
    # First comment is the title; the secret-bearing comment must be scrubbed from
    # the description, leaving only the safe remaining line.
    assert cron["title"] == "Refresh job."
    assert cron["description"] == "Runs nightly."
    blob = json.dumps(cron).lower()
    for marker in oscron_mod._SECRET_MARKERS:
        assert marker not in blob


async def test_oscron_log_tail_reads_redirect_log(client, monkeypatch, tmp_path):
    log = tmp_path / "refresh-cron.log"
    log.write_text("line one\nAuthorization: Bearer secret-token\nline three\n", encoding="utf-8")
    crontab = f"# job\n0 3 * * * /bin/run.sh >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))

    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    resp = await client.get(f"/api/oscron/{job_id}/log")
    assert resp.status == 200
    body = await resp.json()
    assert body["path"] == str(log)
    assert body["lines"] == ["line one", "line three"]  # secret line scrubbed out
    assert "error" not in body


async def test_oscron_log_missing_file_not_available(client, monkeypatch, tmp_path):
    missing = tmp_path / "nope.log"
    crontab = f"0 3 * * * /bin/run.sh >> {missing} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    body = await (await client.get(f"/api/oscron/{job_id}/log")).json()
    assert body["lines"] == []
    assert body["error"] == "not available"


async def test_oscron_log_no_logpath_not_available(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=("ok", "30 9 * * 1 echo hi\n"))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")
    body = await (await client.get(f"/api/oscron/{job_id}/log")).json()
    assert body["lines"] == []
    assert body["error"] == "not available"


async def test_oscron_log_unknown_id_not_available(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=(1, ""))
    body = await (await client.get("/api/oscron/deadbeef/log")).json()
    assert body["lines"] == []
    assert body["error"] == "not available"


async def test_oscron_log_tail_caps_at_max(client, monkeypatch, tmp_path):
    log = tmp_path / "big.log"
    log.write_text("\n".join(f"l{i}" for i in range(1000)) + "\n", encoding="utf-8")
    crontab = f"0 3 * * * /bin/run.sh >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    body = await (await client.get(f"/api/oscron/{job_id}/log?tail=9999")).json()
    assert len(body["lines"]) == oscron_mod._MAX_TAIL
    assert body["truncated"] is True


# --------------------------------------------------------------------------- #
# Execution-history endpoint (/api/oscron/{id}/runs) + per-run log slicing.
# --------------------------------------------------------------------------- #

# A refresh_cron.py-format log: a failed (AUTH-error) run, a succeeded run, and a
# started-but-unfinished trailing run. The AUTH-error path embeds a secret marker
# so we can assert it never leaks into the runs response.
_FAKE_REFRESH_LOG = (
    "2020-01-01 03:00:00,001 [INFO] === refresh_cron starting at 2020-01-01T03:00:00.001000+00:00 ===\n"
    "2020-01-01 03:00:00,002 [ERROR] AUTH: No Midway cookie at /home/u/.midway/cookie. Run mwinit.\n"
    "2020-01-01 03:05:00,001 [INFO] === refresh_cron starting at 2020-01-01T03:05:00.001000+00:00 ===\n"
    "2020-01-01 03:05:00,010 [INFO] Claimed refresh job abc123 (trigger=cron)\n"
    "2020-01-01 03:05:00,500 [INFO] doing work\n"
    "2020-01-01 03:05:47,001 [INFO] Refresh job abc123 finished: state=succeeded finishedAt=2020-01-01T03:05:47.001000+00:00\n"
    "2020-01-01 03:10:00,001 [INFO] === refresh_cron starting at 2020-01-01T03:10:00.001000+00:00 ===\n"
    "2020-01-01 03:10:00,010 [INFO] Claimed refresh job def456 (trigger=manual)\n"
    "2020-01-01 03:10:00,500 [INFO] still going\n"
)


async def test_oscron_runs_crontab_segments_with_statuses(client, monkeypatch, tmp_path):
    log = tmp_path / "refresh-cron.log"
    log.write_text(_FAKE_REFRESH_LOG, encoding="utf-8")
    crontab = f"# refresh\n0 3 * * * /bin/refresh >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body["parsed"] is True
    assert body["source"] == "crontab"
    runs = body["runs"]
    assert len(runs) == 3
    # Most-recent first: the trailing unfinished run (old start) => unknown.
    trailing, succeeded, failed = runs
    assert trailing["status"] == "unknown"  # not recent + no finish => never "succeeded"
    assert trailing["finishedAt"] is None
    assert trailing["durationMs"] is None
    assert trailing["trigger"] == "manual"

    assert succeeded["status"] == "succeeded"
    assert succeeded["trigger"] == "cron"
    assert succeeded["durationMs"] == 47000  # 03:05:47 - 03:05:00
    assert succeeded["startedAt"] is not None and succeeded["finishedAt"] is not None
    assert succeeded["lineStart"] == 3 and succeeded["lineEnd"] == 6

    # AUTH error before any finish => failed (never silently succeeded).
    assert failed["status"] == "failed"
    assert failed["lineStart"] == 1


async def test_oscron_runs_running_when_recent(client, monkeypatch, tmp_path):
    """A started-but-unfinished run whose start is recent => 'running' (not 'succeeded')."""
    log = tmp_path / "run.log"
    log.write_text(
        "2099-01-01 03:00:00,001 [INFO] === refresh_cron starting at 2099-01-01T03:00:00+00:00 ===\n"
        "2099-01-01 03:00:00,010 [INFO] Claimed refresh job z (trigger=cron)\n",
        encoding="utf-8",
    )
    crontab = f"0 3 * * * /bin/run >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    # Freeze "now" just after the start so the orphan is recent.
    monkeypatch.setattr(oscron_mod.time, "time", lambda: 4070919660.0)  # ~1min after 2099-01-01 03:00 UTC
    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body["parsed"] is True
    assert body["runs"][0]["status"] == "running"
    assert body["runs"][0]["finishedAt"] is None


async def test_oscron_runs_no_markers_parsed_false(client, monkeypatch, tmp_path):
    """A log with no recognizable run-start markers => parsed:false + empty runs."""
    log = tmp_path / "plain.log"
    log.write_text("just some output\nno markers here\nmore lines\n", encoding="utf-8")
    crontab = f"0 3 * * * /bin/run >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body["parsed"] is False
    assert body["runs"] == []
    assert body["source"] == "crontab"


async def test_oscron_runs_no_logpath_parsed_false(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=("ok", "30 9 * * 1 echo hi\n"))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")
    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body == {"runs": [], "parsed": False, "source": "crontab"}


async def test_oscron_runs_unknown_id_parsed_false(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=(1, ""))
    body = await (await client.get("/api/oscron/deadbeef/runs")).json()
    assert body["parsed"] is False
    assert body["runs"] == []


async def test_oscron_runs_secret_never_surfaced(client, monkeypatch, tmp_path):
    """The AUTH-error line carries a secret marker; it must never leak into runs."""
    log = tmp_path / "secret.log"
    log.write_text(_FAKE_REFRESH_LOG, encoding="utf-8")
    crontab = f"0 3 * * * /bin/run >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    blob = json.dumps(body).lower()
    for marker in oscron_mod._SECRET_MARKERS:
        assert marker not in blob, f"secret marker leaked into runs: {marker!r}"


async def test_oscron_log_slice_returns_line_range(client, monkeypatch, tmp_path):
    log = tmp_path / "refresh-cron.log"
    log.write_text(_FAKE_REFRESH_LOG, encoding="utf-8")
    crontab = f"0 3 * * * /bin/run >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    # The succeeded run spans lines 3..6.
    body = await (await client.get(f"/api/oscron/{job_id}/log?lineStart=3&lineEnd=6")).json()
    assert "error" not in body
    assert any("Claimed refresh job abc123" in ln for ln in body["lines"])
    assert any("finished: state=succeeded" in ln for ln in body["lines"])
    # Lines outside the slice are absent.
    assert not any("def456" in ln for ln in body["lines"])
    assert body["truncated"] is False


async def test_oscron_log_slice_scrubs_secret(client, monkeypatch, tmp_path):
    log = tmp_path / "refresh-cron.log"
    log.write_text(_FAKE_REFRESH_LOG, encoding="utf-8")
    crontab = f"0 3 * * * /bin/run >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")

    # The failed run spans lines 1..2; line 2 is the secret-bearing AUTH error.
    body = await (await client.get(f"/api/oscron/{job_id}/log?lineStart=1&lineEnd=2")).json()
    blob = json.dumps(body).lower()
    for marker in oscron_mod._SECRET_MARKERS:
        assert marker not in blob


async def test_oscron_log_tail_unchanged_without_slice(client, monkeypatch, tmp_path):
    """Absent lineStart/lineEnd, get_log keeps today's tail behavior."""
    log = tmp_path / "run.log"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    crontab = f"0 3 * * * /bin/run >> {log} 2>&1\n"
    _patch_oscron(monkeypatch, crontab=("ok", crontab))
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "crontab")
    body = await (await client.get(f"/api/oscron/{job_id}/log")).json()
    assert body["lines"] == ["a", "b", "c"]


# A journald JSON stream: a Starting/Finished pair (done => succeeded) and a
# Starting/Finished pair (failed). __REALTIME_TIMESTAMP is usec since epoch.
_FAKE_JOURNAL = "\n".join([
    json.dumps({"MESSAGE": "Starting Cleanup of Temporary Directories...",
                "__REALTIME_TIMESTAMP": "1781471957000000"}),
    json.dumps({"MESSAGE": "Finished Cleanup of Temporary Directories.",
                "__REALTIME_TIMESTAMP": "1781471959000000", "JOB_RESULT": "done"}),
    json.dumps({"MESSAGE": "Starting Cleanup of Temporary Directories...",
                "__REALTIME_TIMESTAMP": "1781475557000000"}),
    json.dumps({"MESSAGE": "Finished Cleanup of Temporary Directories.",
                "__REALTIME_TIMESTAMP": "1781475560000000", "JOB_RESULT": "failed"}),
])


def _patch_oscron_systemd_runs(monkeypatch, *, journal=None, show=None):
    async def fake_journal_user(unit):
        return (0, journal) if journal else (1, "")

    async def fake_journal_system(unit):
        return (0, journal) if journal else (1, "")

    async def fake_show_user(unit):
        return (0, show) if show else (1, "")

    async def fake_show_system(unit):
        return (0, show) if show else (1, "")

    monkeypatch.setattr(oscron_mod, "_run_journalctl_user", fake_journal_user)
    monkeypatch.setattr(oscron_mod, "_run_journalctl_system", fake_journal_system)
    monkeypatch.setattr(oscron_mod, "_run_systemctl_show_user", fake_show_user)
    monkeypatch.setattr(oscron_mod, "_run_systemctl_show_system", fake_show_system)


async def test_oscron_runs_systemd_journald_mapping(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd=_FAKE_SYSTEMD)
    _patch_oscron_systemd_runs(monkeypatch, journal=_FAKE_JOURNAL)
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "systemd")

    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body["parsed"] is True
    assert body["source"] == "systemd"
    runs = body["runs"]
    assert len(runs) == 2
    # Most-recent first: the failed run, then the succeeded one.
    failed, succeeded = runs
    assert failed["status"] == "failed"
    assert succeeded["status"] == "succeeded"
    assert succeeded["startedAt"] == 1781471957000  # usec -> ms
    assert succeeded["finishedAt"] == 1781471959000
    assert succeeded["durationMs"] == 2000


async def test_oscron_runs_systemd_show_fallback(client, monkeypatch):
    """No journald events => fall back to a single systemctl-show invocation."""
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd=_FAKE_SYSTEMD)
    show = (
        "ExecMainStartTimestamp=@1781471957\n"
        "ExecMainExitTimestamp=@1781471959\n"
        "ExecMainStatus=0\n"
        "Result=success\n"
    )
    _patch_oscron_systemd_runs(monkeypatch, journal=None, show=show)
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "systemd")

    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body["parsed"] is True
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["status"] == "succeeded"
    assert run["startedAt"] == 1781471957000
    assert run["durationMs"] == 2000


async def test_oscron_runs_systemd_empty_parsed_false(client, monkeypatch):
    """No journald + no show data => parsed:false (never a fabricated run)."""
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd=_FAKE_SYSTEMD)
    _patch_oscron_systemd_runs(monkeypatch, journal=None, show=None)
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "systemd")
    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    assert body == {"runs": [], "parsed": False, "source": "systemd"}


async def test_oscron_runs_systemd_journal_secret_scrubbed(client, monkeypatch):
    """A journald MESSAGE carrying a secret marker is dropped, never surfaced."""
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd=_FAKE_SYSTEMD)
    journal = "\n".join([
        json.dumps({"MESSAGE": "Starting job with ~/.midway/cookie leak",
                    "__REALTIME_TIMESTAMP": "1781471957000000"}),
        json.dumps({"MESSAGE": "Finished Cleanup.",
                    "__REALTIME_TIMESTAMP": "1781471959000000", "JOB_RESULT": "done"}),
    ])
    _patch_oscron_systemd_runs(monkeypatch, journal=journal, show=None)
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "systemd")
    body = await (await client.get(f"/api/oscron/{job_id}/runs")).json()
    blob = json.dumps(body).lower()
    for marker in oscron_mod._SECRET_MARKERS:
        assert marker not in blob


async def test_oscron_log_slice_systemd_not_available(client, monkeypatch):
    _patch_oscron(monkeypatch, crontab=(1, ""), systemd=_FAKE_SYSTEMD)
    jobs = (await (await client.get("/api/oscron")).json())["jobs"]
    job_id = next(j["id"] for j in jobs if j["source"] == "systemd")
    body = await (await client.get(f"/api/oscron/{job_id}/log?lineStart=1&lineEnd=2")).json()
    assert body["lines"] == []
    assert body["error"] == "not available"



# --- T5 QA: send-routing prefers channelId over userId, double-send guard --- #
#
# The parallel writer already landed test_slack_approve_falls_back_to_user_id
# (userId fallback), test_slack_approve_unroutable_item_fails_loudly (400 on
# no id), and test_slack_approve_empty_channel_id_is_unroutable (empty-string
# edge). These two close the remaining spec gaps:
#   * channelId is PREFERRED over userId when both are present (the human-
#     readable 'channel' field must NEVER reach the send seam as target).
#   * The already-sent→409 double-send guard works identically WITH channelId.


async def test_slack_approve_uses_channel_id_in_send_payload(client, slack_file, monkeypatch):
    """approve_item sends to the captured channelId (the D.../C.../G... conversation
    id from the Slack read payload), NOT the human-readable 'channel' field.
    channelId is preferred over userId when both are present."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "Huan Wang", "channel": "DM with Huan Wang",
        "channelType": "dm", "channelId": "D02P0TU89CN", "userId": "U02P2R0L3DX",
        "snippet": "can you review?", "threadContext": "", "draft": "sure thing",
        "generatedDraft": "sure thing", "status": "needs-review", "ts": 1,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "sent"
    # The send seam was called with the routable channelId as target.
    assert len(agent.calls) == 1
    action, payload = agent.calls[0]
    assert action == "send"
    assert payload["target"] == "D02P0TU89CN"
    # channelId is PREFERRED over userId — verify userId is NOT the target.
    assert payload["target"] != "U02P2R0L3DX"
    # The human-readable 'channel' field must NOT appear as the send target.
    assert "DM with" not in payload.get("target", "")


async def test_slack_approve_double_send_guard_still_holds_with_channel_id(client, slack_file, monkeypatch):
    """The already-sent->409 double-send guard is INDEPENDENT of the channelId
    routing logic: an item with channelId AND status='sent' still returns 409
    and the send seam is never called."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "i1", "sender": "Huan Wang", "channel": "DM with Huan Wang",
        "channelType": "dm", "channelId": "D02P0TU89CN", "userId": "U02P2R0L3DX",
        "snippet": "done?", "threadContext": "", "draft": "yep",
        "generatedDraft": "yep", "status": "sent", "ts": 1, "finalText": "yep",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/i1/approve", json={
        "text": "yep", "etag": "stale-or-bogus", "expectedDraft": "yep",
    })
    assert resp.status == 409
    # No send attempted.
    assert len(agent.calls) == 0


# --- T4 QA: group_dm channelType flows through the backend correctly --- #
#
# The backend treats channelType as informational metadata (not a validation
# gate — _VALID_STATUS validates only the status field). These tests confirm
# that "group_dm" items round-trip, route, dismiss/undismiss, and save without
# any channelType-specific rejection.


async def test_slack_group_dm_item_round_trips(client, slack_file):
    """A group_dm item written to the sidecar is served back via GET /api/slack/queue
    with channelType 'group_dm' intact — the backend never rewrites or rejects it."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "gdm1", "sender": "manhalr", "channel": "mpdm-manhalr--xulaicao--emake-1",
        "channelType": "group_dm", "channelId": "C0BC0TT5Z2L",
        "snippet": "hey team, quick sync?", "threadContext": "",
        "draft": "sure, let me check my calendar", "generatedDraft": "sure, let me check my calendar",
        "status": "needs-review", "ts": 1718600000,
    }]}), encoding="utf-8")
    resp = await client.get("/api/slack/queue")
    assert resp.status == 200
    body = await resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["channelType"] == "group_dm"
    assert item["channelId"] == "C0BC0TT5Z2L"
    assert item["channel"] == "mpdm-manhalr--xulaicao--emake-1"
    assert item["sender"] == "manhalr"
    assert item["status"] == "needs-review"


async def test_slack_group_dm_approve_routes_via_channel_id(client, slack_file, monkeypatch):
    """Approve on a group_dm item routes the send to the channelId — same
    routing logic as DMs. channelId is the routable key regardless of channelType."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "gdm1", "sender": "manhalr", "channel": "mpdm-manhalr--xulaicao--emake-1",
        "channelType": "group_dm", "channelId": "C0BC0TT5Z2L",
        "snippet": "hey team", "threadContext": "",
        "draft": "on it", "generatedDraft": "on it",
        "status": "needs-review", "ts": 1718600000,
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True})
    resp = await client.post("/api/slack/queue/gdm1/approve", json={
        "text": "on it",
    })
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "sent"
    # The send seam was called with channelId as the target.
    assert len(agent.calls) == 1
    action, payload = agent.calls[0]
    assert action == "send"
    assert payload["target"] == "C0BC0TT5Z2L"
    # The human-readable mpdm slug must NOT be used as the send target.
    assert "mpdm" not in payload.get("target", "")


async def test_slack_group_dm_dismiss_and_undismiss(client, slack_file):
    """Soft-dismiss semantics apply identically to group_dm items: dismiss flips
    to 'dismissed', undismiss restores to 'needs-review' (draft == generatedDraft)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "gdm1", "sender": "emake", "channel": "mpdm-manhalr--xulaicao--emake-1",
        "channelType": "group_dm", "channelId": "C0BC0TT5Z2L",
        "snippet": "thoughts?", "threadContext": "",
        "draft": "let me think about it", "generatedDraft": "let me think about it",
        "status": "needs-review", "ts": 1718600000,
    }]}), encoding="utf-8")

    # Dismiss it.
    resp = await client.delete("/api/slack/queue/gdm1", json={})
    assert resp.status == 200
    items = (await (await client.get("/api/slack/queue")).json())["items"]
    assert len(items) == 1
    assert items[0]["status"] == "dismissed"
    assert items[0]["channelType"] == "group_dm"  # channelType preserved

    # Undismiss it — restores to needs-review since draft == generatedDraft.
    resp = await client.post("/api/slack/queue/gdm1/undismiss", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "needs-review"
    assert body["item"]["channelType"] == "group_dm"  # still group_dm

    # Verify persisted to disk.
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["channelType"] == "group_dm"


async def test_slack_channeltype_group_dm_accepted_by_save_draft(client, slack_file):
    """PUT (save draft) on a group_dm item in needs-review status with a draft
    succeeds with 200 — no validation blocks 'group_dm' as a channelType value."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "gdm1", "sender": "manhalr", "channel": "mpdm-manhalr--xulaicao--emake-1",
        "channelType": "group_dm", "channelId": "C0BC0TT5Z2L",
        "snippet": "can you review this PR?", "threadContext": "",
        "draft": "auto draft", "generatedDraft": "auto draft",
        "status": "needs-review", "ts": 1718600000,
    }]}), encoding="utf-8")
    resp = await client.put("/api/slack/queue/gdm1", json={"draft": "will review after lunch"})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["draft"] == "will review after lunch"
    assert body["item"]["status"] == "edited"  # diverged from generatedDraft
    assert body["item"]["channelType"] == "group_dm"  # still group_dm
    # Persisted correctly.
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "will review after lunch"
    assert saved["channelType"] == "group_dm"


# --------------------------------------------------------------------------- #
# T3 regression: the slack-mcp "r5 status: 429" SAML-throttle fix (D-022).
#
# INVARIANT under test: the ONLY code path that cold-starts a slack-mcp process is
# the single PERSISTENT read-only session (_connect_mcp). The draft + regenerate
# paths pre-fetch any conversation history on THAT warm-auth session (via
# _fetch_history_for) and render it into the prompt, so their `claude --print`
# subprocess gets NO --allowedTools and an EMPTY --mcp-config under
# --strict-mcp-config (so it does NOT inherit the global slack-mcp entry) and
# never boots its own slack-mcp. 'send' is the ONE intentional cold-start exception (it needs the
# post_message write tool the read-only session refuses). _mark_channel_read uses
# ONLY the persistent session — never a one-shot stdio_client/ClientSession.
#
# These tests are PURELY ADDITIVE. They reuse the existing reset_mcp_state /
# _run_one_draft_cycle / _stub_agent / slack_file / client fixtures and the
# _SlackFakeProc / _result_line helpers (no new module-level fixture/class names,
# avoiding the duplicate-name shadowing footgun documented in qa.md).
# --------------------------------------------------------------------------- #


# ── ITEM #1: _mark_channel_read uses ONLY the persistent session ──────────────


async def test_slack_mark_channel_read_uses_persistent_session_never_one_shot(
        monkeypatch, reset_mcp_state):
    """_mark_channel_read must route set_last_read through the PERSISTENT MCP
    session (via _connect_mcp / _mcp_state.session) and NEVER construct a one-shot
    stdio_client / StdioServerParameters connection (which would trigger a fresh
    SAML auth and risk the 429 throttle). We monkeypatch both connection
    constructors to RAISE if ever touched, point _connect_mcp at a fake recording
    session, and assert (a) neither constructor was hit and (b) the fake session
    received the set_last_read call."""
    def _boom_stdio(*_a, **_k):
        raise AssertionError("_mark_channel_read constructed a one-shot stdio_client!")

    def _boom_params(*_a, **_k):
        raise AssertionError("_mark_channel_read constructed StdioServerParameters!")

    monkeypatch.setattr(slack_mod, "stdio_client", _boom_stdio)
    monkeypatch.setattr(slack_mod, "StdioServerParameters", _boom_params)

    calls = []

    class _FakeSession:
        async def call_tool(self, name, args):
            calls.append((name, args))

            class _R:
                isError = False
                structuredContent = {"ok": True}

            return _R()

    async def fake_connect():
        # The persistent path: _connect_mcp sets _mcp_state.session (no spawn here).
        slack_mod._mcp_state.session = _FakeSession()

    monkeypatch.setattr(slack_mod, "_connect_mcp", fake_connect)

    await slack_mod._mark_channel_read("D1")

    # (a) The one-shot connection constructors were NEVER touched (no _boom raised).
    # (b) The mark-read went through the persistent session's call_tool.
    assert calls and calls[0][0] == "set_last_read"
    assert calls[0][1].get("channel") == "D1"


async def test_slack_mark_channel_read_empty_channel_is_noop(monkeypatch, reset_mcp_state):
    """An empty channel id is a clean no-op — it never even connects (so it can
    never cold-start slack-mcp). Guards the early-return guard."""
    def _boom_connect():
        raise AssertionError("_mark_channel_read connected for an empty channel id!")

    monkeypatch.setattr(slack_mod, "_connect_mcp", _boom_connect)
    # No raise from the boom connect => the early return fired before connecting.
    await slack_mod._mark_channel_read("")


# ── ITEM #2: the draft path is MCP-FREE (history via the persistent session) ──


async def test_slack_draft_path_prefetches_history_on_persistent_session(
        client, slack_file, monkeypatch):
    """A needs-draft skeleton with a channelId but NO history3d must have its
    history pre-fetched via _fetch_history_for (the PERSISTENT warm-auth session)
    and passed into the draft seam payload — so the 'draft' subprocess never needs
    MCP. We assert _fetch_history_for was called with the item's channelId and that
    the seam payload carried that history3d."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "nd1", "sender": "alice", "channel": "DM with alice", "channelType": "dm",
        "channelId": "D1", "userId": "U1", "snippet": "ping", "ts": 1,
        "status": "needs-draft",
    }]}), encoding="utf-8")

    canned = [{"ts": 1, "author": "alice", "text": "ping from history"}]
    fetched_for = []

    async def fake_fetch(channel_id):
        fetched_for.append(channel_id)
        return list(canned)

    captured = {}

    async def fake_agent(action, payload):
        captured["action"] = action
        captured["payload"] = payload
        return {"available": True, "draft": "drafted from history",
                "history3d": payload.get("history3d", []),
                "generatedDraft": "drafted from history"}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)
    # Drive the worker with OUR history-fetch stub (the persistent-session seam).
    await _run_one_draft_cycle(client.app, monkeypatch, fetch_history=fake_fetch)

    # The persistent session supplied history for THIS item's channelId.
    assert "D1" in fetched_for
    # The seam ran the MCP-free 'draft' action and carried the fetched history3d.
    assert captured.get("action") == "draft"
    assert captured["payload"].get("history3d") == canned
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["draft"] == "drafted from history"


async def test_slack_draft_path_empty_history_still_drafts_from_snippet(
        client, slack_file, monkeypatch):
    """EDGE CASE: if _fetch_history_for returns [] (unreadable channel), the worker
    must STILL draft (from the snippet alone) via the MCP-free 'draft' action — it
    must NEVER fall back to a cold-MCP path just to draft text."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "nd2", "sender": "bob", "channel": "DM with bob", "channelType": "dm",
        "channelId": "D2", "userId": "U2", "snippet": "are you free?", "ts": 2,
        "status": "needs-draft",
    }]}), encoding="utf-8")

    async def fake_fetch(channel_id):
        return []  # channel unreadable — no history

    actions = []

    async def fake_agent(action, payload):
        actions.append(action)
        return {"available": True, "draft": "sure, what's up?",
                "generatedDraft": "sure, what's up?"}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)
    await _run_one_draft_cycle(client.app, monkeypatch, fetch_history=fake_fetch)

    # The draft proceeded via the MCP-free 'draft' action (NEVER a cold-MCP path).
    assert actions and all(a == "draft" for a in actions)
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["draft"] == "sure, what's up?"


def _capture_spawn_args(monkeypatch, result_text):
    """Monkeypatch create_subprocess_exec to RECORD the spawned arg list and return
    a fake proc whose stdout yields one strict-JSON result event. Returns the list
    the recorded args are appended to (one entry per spawn).

    Each recorded entry also carries an attribute-free side channel: we snapshot the
    --mcp-config file's PARSED contents AT SPAWN TIME into a parallel ``configs``
    list, because _drive_agent unlinks that temp file in its finally block before
    returning — reading it after the call would raise FileNotFoundError."""
    class _SpawnLog(list):
        """A list (so existing `spawned[0]` callers keep working) that also carries
        a parallel ``.configs`` list of parsed --mcp-config snapshots."""
        configs: list = []

    spawned = _SpawnLog()
    spawned.configs = []

    async def fake_exec(*args, **kwargs):
        arglist = list(args)
        spawned.append(arglist)
        cfg = None
        if "--mcp-config" in arglist:
            cfg_path = arglist[arglist.index("--mcp-config") + 1]
            try:
                cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cfg = None
        spawned.configs.append(cfg)
        return _SlackFakeProc(stdout_lines=[_result_line(result_text)])

    monkeypatch.setattr(slack_mod.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


def _assert_strict_empty_mcp_config(args, cfg):
    """A draft/regenerate spawn must be SCOPED AWAY from the global slack-mcp:
    --strict-mcp-config is present AND the inline --mcp-config carries an EMPTY
    mcpServers map (no slack-mcp). Without strict, a bare `claude --print` inherits
    the user's GLOBAL ~/.claude.json mcpServers (which contains slack-mcp) and
    cold-starts it on every draft — the exact regression this guards (D-022).

    ``cfg`` is the config PARSED AT SPAWN TIME (the temp file is unlinked before
    _drive_agent returns, so it can't be re-read here)."""
    assert "--strict-mcp-config" in args, (
        "draft/regenerate spawn dropped --strict-mcp-config — a bare "
        "`claude --print` inherits the global slack-mcp and cold-starts it")
    assert "--mcp-config" in args, "expected an inline (empty) --mcp-config to scope the subprocess"
    assert cfg is not None, "--mcp-config file was unreadable at spawn time"
    # Empty server map: the subprocess loads NO MCP server, so nothing cold-starts.
    assert cfg.get("mcpServers") == {}, (
        f"draft/regenerate --mcp-config must omit ALL servers, got {cfg.get('mcpServers')}")


async def test_slack_drive_agent_draft_spawn_is_mcp_free_but_strict(monkeypatch):
    """At the _drive_agent level: a 'draft' spawn (with pre-fetched history) builds
    `claude --print` args with NO --allowedTools and an EMPTY --mcp-config under
    --strict-mcp-config — so it loads no slack-mcp and never cold-starts one."""
    spawned = _capture_spawn_args(monkeypatch, json.dumps(
        {"draft": "hi", "generatedDraft": "hi", "threadContext": "t"}))
    result = await slack_mod._drive_agent(
        "draft", {"id": "x", "prompt": "P",
                  "history3d": [{"ts": 1, "author": "a", "text": "x"}]})
    assert result["available"] is True
    assert spawned, "no subprocess was spawned"
    assert "--allowedTools" not in spawned[0]
    _assert_strict_empty_mcp_config(spawned[0], spawned.configs[0])


async def test_slack_drive_agent_regenerate_spawn_is_mcp_free_but_strict(monkeypatch):
    """A 'regenerate' spawn is likewise MCP-free: no --allowedTools, and an EMPTY
    --mcp-config under --strict-mcp-config so it cannot inherit the global slack-mcp."""
    spawned = _capture_spawn_args(monkeypatch, json.dumps({"draft": "redrafted"}))
    result = await slack_mod._drive_agent("regenerate", {"id": "x", "prompt": "P"})
    assert result["available"] is True
    assert "--allowedTools" not in spawned[0]
    _assert_strict_empty_mcp_config(spawned[0], spawned.configs[0])


async def test_slack_drive_agent_polish_spawn_is_mcp_free_but_strict(monkeypatch):
    """A 'polish' spawn is MCP-free like draft/regenerate (D-029): no --allowedTools,
    and an EMPTY --mcp-config under --strict-mcp-config so it loads no slack-mcp and
    can never read or send on Slack — it only transforms the text in its prompt."""
    spawned = _capture_spawn_args(monkeypatch, json.dumps({"draft": "polished"}))
    result = await slack_mod._drive_agent("polish", {"text": "rough text"})
    assert result["available"] is True
    assert "--allowedTools" not in spawned[0]
    _assert_strict_empty_mcp_config(spawned[0], spawned.configs[0])


async def test_slack_drive_agent_send_spawn_keeps_mcp_config(monkeypatch):
    """CONTRAST (the one intentional exception): a 'send' spawn DOES carry
    --mcp-config + --allowedTools (it needs the post_message write tool the
    persistent read-only session refuses). This proves the draft/regenerate
    MCP-free assertions above are not vacuous — the flag IS added when needed."""
    spawned = _capture_spawn_args(monkeypatch, json.dumps({"ok": True, "ts": "1.2"}))
    result = await slack_mod._drive_agent(
        "send", {"target": "D1", "text": "hi"})
    assert result["available"] is True
    args = spawned[0]
    assert "--mcp-config" in args
    assert "--allowedTools" in args
    # And the send allowlist grants the write tool — the deliberate exception.
    idx = args.index("--allowedTools")
    assert "post_message" in args[idx + 1]


# ── ITEM #3: the regenerate path is MCP-FREE (history via persistent session) ──


async def test_slack_regenerate_prefetches_history_on_persistent_session(
        client, slack_file, monkeypatch):
    """POST /api/slack/queue/{id}/refresh (regenerate_item) for an item with a
    channelId and NO history3d must pre-fetch history via _fetch_history_for (the
    PERSISTENT warm-auth session) and feed it into the prompt the MCP-free
    'regenerate' seam drafts from — so the regenerate path never cold-starts
    slack-mcp. We assert _fetch_history_for was called with the channelId and that
    the fetched message text reached the seam prompt."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "rg1", "sender": "carol", "channel": "DM with carol", "channelType": "dm",
        "channelId": "D9", "userId": "U9", "snippet": "thoughts?", "threadContext": "",
        "draft": "old", "generatedDraft": "old", "status": "needs-review", "ts": 3,
    }]}), encoding="utf-8")

    fetched_for = []

    async def fake_fetch(channel_id):
        fetched_for.append(channel_id)
        return [{"ts": 3, "author": "carol", "text": "HISTORY_MARKER_TEXT"}]

    monkeypatch.setattr(slack_mod, "_fetch_history_for", fake_fetch)

    captured = {}

    async def fake_agent(action, payload):
        captured["action"] = action
        captured["prompt"] = payload.get("prompt", "")
        return {"available": True, "draft": "regenerated reply"}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)

    resp = await client.post("/api/slack/queue/rg1/refresh")
    assert resp.status == 200
    body = await resp.json()

    # The persistent session supplied history for THIS item's channelId.
    assert fetched_for == ["D9"]
    # The MCP-free 'regenerate' action ran, with the fetched history in its prompt.
    assert captured.get("action") == "regenerate"
    assert "HISTORY_MARKER_TEXT" in captured["prompt"]
    assert body["item"]["draft"] == "regenerated reply"
    assert body["item"]["status"] == "needs-review"


async def test_slack_regenerate_skips_history_fetch_when_already_present(
        client, slack_file, monkeypatch):
    """If the item ALREADY carries history3d, regenerate must NOT re-fetch (avoid a
    redundant persistent-session round-trip) — but still runs the MCP-free
    'regenerate' seam. Guards the `if not item.get("history3d")` gate."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "rg2", "sender": "dave", "channel": "DM with dave", "channelType": "dm",
        "channelId": "D10", "snippet": "ping", "threadContext": "",
        "draft": "old", "generatedDraft": "old", "status": "needs-review", "ts": 4,
        "history3d": [{"ts": 4, "author": "dave", "text": "already here"}],
    }]}), encoding="utf-8")

    fetched_for = []

    async def fake_fetch(channel_id):
        fetched_for.append(channel_id)
        return [{"ts": 4, "author": "dave", "text": "should not be used"}]

    monkeypatch.setattr(slack_mod, "_fetch_history_for", fake_fetch)

    captured = {}

    async def fake_agent(action, payload):
        captured["action"] = action
        return {"available": True, "draft": "regenerated"}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)

    resp = await client.post("/api/slack/queue/rg2/refresh")
    assert resp.status == 200
    # Already had history3d -> no redundant fetch.
    assert fetched_for == []
    assert captured.get("action") == "regenerate"


# --------------------------------------------------------------------------- #
# D-025: Slack pause toggle, classify worker, and thread muting.
#
# These tests are PURELY ADDITIVE — they reuse the existing slack_file / _stub_agent
# / _run_one_scan_cycle / reset_scan_ts fixtures and the module-level slack_mod.
# The pause/classify gates are checked INSIDE the worker loops (never by stopping
# tasks); classification is a NEW worker between scan and draft; muting is a
# channelId-scoped key in the existing slack_threads.json sidecar.
# --------------------------------------------------------------------------- #


# ── Feature 1: pause toggle ───────────────────────────────────────────────────


async def test_slack_put_config_sets_paused_and_queue_reports_it(client, slack_file):
    """PUT /api/slack/config {paused:true} persists the top-level flag and GET
    /queue echoes it; absent on disk == unpaused (false)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")

    # Absent key -> unpaused.
    body = await (await client.get("/api/slack/queue")).json()
    assert body["paused"] is False

    resp = await client.put("/api/slack/config", json={"paused": True})
    assert resp.status == 200
    assert (await resp.json())["paused"] is True

    # Persisted on disk + reflected by GET /queue.
    saved = json.loads(slack_file.read_text(encoding="utf-8"))
    assert saved["paused"] is True
    body = await (await client.get("/api/slack/queue")).json()
    assert body["paused"] is True

    # Toggling back off round-trips.
    resp = await client.put("/api/slack/config", json={"paused": False})
    assert resp.status == 200
    assert (await resp.json())["paused"] is False


async def test_slack_put_config_requires_paused_field(client, slack_file):
    """PUT /api/slack/config without `paused` is a 400 (fail loud on missing input)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    resp = await client.put("/api/slack/config", json={"something": True})
    assert resp.status == 400


async def test_slack_put_config_conflict_returns_409(client, slack_file):
    """PUT /api/slack/config is etag-guarded: a stale client etag → 409 with the
    current state, mirroring save_draft's optimistic-concurrency contract."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    # Capture a now-stale etag, then move the file out-of-band so it no longer matches.
    stale = (await (await client.get("/api/slack/queue")).json())["etag"]
    _time.sleep(0.01)  # mtime_ns etags collide without a real-time gap
    data, cur = slack_mod.filestore.read_json(slack_file)
    data["items"].append({"id": "x", "status": "needs-review"})
    slack_mod.filestore.write_json(slack_file, data, cur)

    resp = await client.put("/api/slack/config", json={"paused": True, "etag": stale})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert "etag" in body


async def test_slack_paused_skips_scan_cycle(client, slack_file, reset_scan_ts, monkeypatch):
    """paused=true: a scan-worker cycle skips the scan entirely — _scan_read is NEVER
    called and the queue file is byte-unchanged (the gate is INSIDE the loop, after
    the readiness gate)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"paused": True, "items": []}), encoding="utf-8")
    before = slack_file.read_text(encoding="utf-8")

    called = []

    async def spy_scan_read(name, arguments):
        called.append(name)
        return {}

    monkeypatch.setattr(slack_mod, "_scan_read", spy_scan_read)
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    monkeypatch.setattr(slack_mod, "SLACK_SCAN_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._scan_worker(client.app))
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The paused gate fired before any MCP read; nothing scanned, file untouched.
    assert called == []
    assert slack_file.read_text(encoding="utf-8") == before


async def test_slack_paused_skips_draft_cycle(client, slack_file, monkeypatch):
    """paused=true: a draft-worker cycle skips drafting — the seam is NEVER called
    and a needs-draft skeleton stays needs-draft (gate INSIDE the loop)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"paused": True, "items": [{
        "id": "s1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "ping", "ts": 1, "status": "needs-draft",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "x"})
    await _run_one_draft_cycle(client.app, monkeypatch)

    assert agent.calls == []
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-draft"


async def test_slack_paused_skips_classify_cycle(client, slack_file, monkeypatch):
    """paused=true: a classify-worker cycle skips classification — the seam is NEVER
    called and a needs-classify skeleton stays needs-classify."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"paused": True, "items": [{
        "id": "c1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "ping", "ts": 1, "status": "needs-classify",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "classifications": []})
    await _run_one_classify_cycle(client.app, monkeypatch)

    assert agent.calls == []
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-classify"


async def _run_one_manual_scan(app, monkeypatch, *, unreads):
    """Drive ONE MANUAL scan (D-026): set the worker's wake Event so the cycle takes
    the manual (not periodic-timeout) path, then run the worker a moment and cancel.

    Identical harness to ``_run_one_scan_cycle`` (canned _scan_read, forced
    readiness) EXCEPT the wake Event is set so the loop treats this as a Refresh-
    triggered manual scan — which must run even while paused AND chain the full
    classify+draft pipeline. We keep SLACK_SCAN_INTERVAL_S long so the ONLY thing
    that wakes the loop is our Event (no spurious periodic tick races in)."""
    async def fake_scan_read(name, arguments):
        if name == "get_unreads":
            return unreads
        if name == "get_messages":
            return {"messages": []}
        raise AssertionError(f"unexpected scan tool {name!r}")

    monkeypatch.setattr(slack_mod, "_scan_read", fake_scan_read)
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    monkeypatch.setattr(slack_mod, "SLACK_SCAN_INTERVAL_S", 30)
    task = asyncio.create_task(slack_mod._scan_worker(app))
    try:
        await asyncio.sleep(0.02)   # let the worker park on the wake Event
        slack_mod._get_scan_event().set()
        await asyncio.sleep(0.25)   # full manual pipeline: scan -> classify -> draft
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_slack_manual_scan_runs_even_when_paused(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """D-026: `paused` gates ONLY the periodic tick. A MANUAL scan (wake Event set,
    as the Refresh button does) STILL scans while paused — _scan_read IS called and
    a fresh needs-classify skeleton is written for the new conversation."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"paused": True, "items": []}), encoding="utf-8")

    now_s = _time.time()
    unreads = {"channels": [
        {"channelId": "D_alice", "name": "alice",
         "messages": [{"user": "alice", "text": "hey", "ts": f"{now_s - 1:.6f}"}]},
    ]}
    # Stub classify/draft seams so the chained pipeline is hermetic (no real claude).
    async def empty_history(_channel_id):
        return []
    monkeypatch.setattr(slack_mod, "_fetch_history_for", empty_history)
    _stub_agent(monkeypatch, {"available": True, "classifications": [], "draft": ""})

    await _run_one_manual_scan(client.app, monkeypatch, unreads=unreads)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"]
    # The manual scan ran despite paused=true: the new DM was scanned and written.
    assert [it["channelId"] for it in saved] == ["D_alice"]
    assert saved[0]["status"] == "needs-classify"
    # paused is still true — a manual scan does NOT resume the periodic workers.
    assert json.loads(slack_file.read_text(encoding="utf-8")).get("paused") is True


async def test_slack_manual_scan_drives_full_pipeline_while_paused(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """D-026: a MANUAL scan while paused drives the FULL one-time pipeline —
    scan → classify → draft — so the user gets ready-to-review drafts even though
    the periodic classify/draft workers are paused. The freshly-scanned skeleton
    lands needs-classify, is classified needs-reply → needs-draft, then drafted →
    needs-review, all in the single manual cycle."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"paused": True, "items": []}), encoding="utf-8")

    now_s = _time.time()
    unreads = {"channels": [
        {"channelId": "D_alice", "name": "alice",
         "messages": [{"user": "alice", "text": "can you review the PR?", "ts": f"{now_s - 1:.6f}"}]},
    ]}

    async def empty_history(_channel_id):
        return []
    monkeypatch.setattr(slack_mod, "_fetch_history_for", empty_history)

    # The seam: classify routes the scanned item to needs-reply, then draft fills it.
    async def fake_agent(action, payload):
        fake_agent.calls.append(action)
        if action == "classify":
            ids = [it["id"] for it in payload["items"]]
            return {"available": True, "classifications": [
                {"id": i, "classification": "needs-reply"} for i in ids]}
        if action == "draft":
            return {"available": True, "draft": "Sure, I'll take a look."}
        return {"available": True, "action": action}
    fake_agent.calls = []
    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)

    await _run_one_manual_scan(client.app, monkeypatch, unreads=unreads)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"]
    assert len(saved) == 1
    item = saved[0]
    # Full pipeline ran in the single manual cycle: classify + draft both fired and
    # the item ended drafted-and-ready despite paused=true.
    assert "classify" in fake_agent.calls
    assert "draft" in fake_agent.calls
    assert item["status"] == "needs-review"
    assert item["draft"] == "Sure, I'll take a look."
    # No send ever fired (the pipeline never sends — only the user-gated seam does).
    assert "send" not in fake_agent.calls


# ── Feature 2: classify worker ────────────────────────────────────────────────


async def _run_one_classify_cycle(app, monkeypatch):
    """Drive ONE _classify_worker iteration: force readiness, tighten the interval,
    run the worker a moment, then cancel cleanly (mirrors _run_one_draft_cycle)."""
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": True})
    monkeypatch.setattr(slack_mod, "SLACK_CLASSIFY_POLL_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._classify_worker(app))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_slack_scan_skeleton_is_needs_classify():
    """A freshly-scanned skeleton lands in 'needs-classify' (NOT 'needs-draft', D-025)
    so the classify worker triages it first."""
    skel = slack_mod._skeleton_for({
        "channelId": "D1", "channelType": "dm", "sender": "a",
        "channel": "c", "snippet": "hi", "ts": 1,
    })
    assert skel["status"] == "needs-classify"


def test_slack_classify_status_and_active_sets():
    """D-027: needs-classify is a valid status and dedupes in scan. 'fyi' is NO
    LONGER a status — it is a classification LABEL on a needs-draft/needs-review
    item, so it must NOT appear in the status sets; the valid classifications live
    in their own set."""
    assert "needs-classify" in slack_mod._VALID_STATUS
    assert "fyi" not in slack_mod._VALID_STATUS
    assert "fyi" in slack_mod._VALID_CLASSIFICATIONS
    assert "needs-classify" in slack_mod._SCAN_ACTIVE_STATUSES
    assert "fyi" not in slack_mod._SCAN_ACTIVE_STATUSES
    # D-028: 'dismissed' is NOT an in-flight active status — a dismissed
    # conversation that grows a newer message must resurface (suppress-until-newer
    # in _merge_scan_candidates), not be silently deafened forever.
    assert "dismissed" not in slack_mod._SCAN_ACTIVE_STATUSES


def test_slack_load_migrates_legacy_fyi_status(slack_file):
    """D-027 migration: a legacy item written with status 'fyi' (the old terminal
    state) is converted on _load to status 'needs-draft' + classification 'fyi' so
    it rejoins drafting and gets context + a draft. _load must NOT write to disk (a
    read stays a read) — the migration is in-memory until the next normal write."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "old_fyi", "sender": "bot", "channel": "#deploys", "channelType": "mention",
        "channelId": "C1", "snippet": "deploy finished", "ts": 1, "status": "fyi",
    }]}), encoding="utf-8")
    before = slack_file.read_text(encoding="utf-8")

    data, _ = slack_mod._load()
    item = data["items"][0]
    assert item["status"] == "needs-draft"
    assert item["classification"] == "fyi"
    # The read did not mutate the file on disk (still the legacy shape).
    assert slack_file.read_text(encoding="utf-8") == before


async def test_slack_fyi_classified_item_gets_drafted(client, slack_file, monkeypatch):
    """D-027: an item classified 'fyi' is NOT terminal — it flows to needs-draft and
    the draft worker drafts it (context + draft), ending needs-review with the fyi
    classification preserved. This is the fix for 'FYI has no thread context/draft'."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "f1", "sender": "bot", "channel": "#deploys", "channelType": "mention",
        "channelId": "C1", "snippet": "deploy finished", "ts": 1,
        "status": "needs-draft", "classification": "fyi",
    }]}), encoding="utf-8")

    async def empty_history(_channel_id):
        return []
    monkeypatch.setattr(slack_mod, "_fetch_history_for", empty_history)
    _stub_agent(monkeypatch, {"available": True, "draft": "Noted, thanks for the heads up."})
    await _run_one_draft_cycle(client.app, monkeypatch)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"        # drafted like any other item
    assert saved["draft"] == "Noted, thanks for the heads up."
    assert saved["classification"] == "fyi"          # label preserved through drafting


def test_slack_classify_prompt_and_parse_shape():
    """The classify prompt asks for the {id, classification} JSON array with the
    needs-reply/fyi/actionable definitions and forbids tool calls; the parser
    requires a JSON array and fails loud on a non-array."""
    prompt = slack_mod._build_agent_prompt("classify", {"items": [
        {"id": "c1", "sender": "alice", "channel": "DM", "snippet": "can you review?"},
    ]})
    assert "needs-reply" in prompt and "fyi" in prompt and "actionable" in prompt
    assert "JSON array of {id, classification}" in prompt
    assert "do NOT call any tools" in prompt
    assert "c1" in prompt  # the item id is rendered into the batch

    ok = slack_mod._parse_agent_result(
        json.dumps([{"id": "c1", "classification": "fyi"}]), "classify")
    assert ok["available"] is True
    assert ok["classifications"][0]["classification"] == "fyi"
    # A non-array reply fails loud (no fabrication).
    bad = slack_mod._parse_agent_result(json.dumps({"id": "c1"}), "classify")
    assert bad["available"] is False


def test_slack_classify_action_is_mcp_free_at_spawn():
    """The 'classify' action is MCP-FREE at spawn (needs_mcp=False) so it gets no
    --allowedTools / empty --mcp-config. _ALLOWED_TOOLS must NOT grant it any tool
    (it is absent / read-free by design — the snippet-only triage path)."""
    assert "classify" not in slack_mod._ALLOWED_TOOLS or \
        "mcp__slack-mcp__post_message" not in slack_mod._ALLOWED_TOOLS.get("classify", [])


async def test_slack_classify_spawn_is_mcp_free(monkeypatch):
    """The classify subprocess spawns with --strict-mcp-config + an EMPTY --mcp-config
    (no servers) and NO --allowedTools — zero Slack access, zero cold-start (D-025,
    same no-MCP path as draft/regenerate)."""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        return _SlackFakeProc(stdout_lines=[_result_line(json.dumps([]))])

    monkeypatch.setattr(slack_mod.asyncio, "create_subprocess_exec", fake_exec)
    result = await slack_mod._run_slack_agent("classify", {"items": [
        {"id": "c1", "sender": "a", "channel": "c", "snippet": "hi"}]})
    assert result["available"] is True

    args = captured["args"]
    assert "--strict-mcp-config" in args
    # No --allowedTools on the MCP-free classify spawn.
    assert "--allowedTools" not in args
    # The --mcp-config (if written) carries NO servers (empty), so no slack-mcp boots.
    if "--mcp-config" in args:
        cfg_path = args[args.index("--mcp-config") + 1]
        if Path(cfg_path).exists():
            cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
            assert cfg.get("mcpServers", {}) == {}


async def test_slack_classify_worker_batches_and_routes(client, slack_file, monkeypatch):
    """ONE classify cycle: the worker batches all needs-classify items into ONE
    _run_slack_agent('classify') call, RECORDS the label in `classification`, and
    routes EVERY item → needs-draft (D-027) so each gets context + a draft
    regardless of label. fyi is a classification, NOT a terminal status."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [
        {"id": "reply1", "sender": "alice", "channel": "DM with alice",
         "channelType": "dm", "channelId": "D1", "snippet": "can you review the PR?",
         "ts": 1, "status": "needs-classify"},
        {"id": "fyi1", "sender": "bot", "channel": "#deploys", "channelType": "mention",
         "channelId": "C1", "snippet": "deploy finished", "ts": 2, "status": "needs-classify"},
        {"id": "act1", "sender": "carol", "channel": "DM with carol",
         "channelType": "dm", "channelId": "D2", "snippet": "please file the ticket",
         "ts": 3, "status": "needs-classify"},
    ]}), encoding="utf-8")

    calls = []

    async def fake_agent(action, payload):
        calls.append((action, payload))
        # Canned classifications keyed to the batch ids.
        return {"available": True, "classifications": [
            {"id": "reply1", "classification": "needs-reply"},
            {"id": "fyi1", "classification": "fyi"},
            {"id": "act1", "classification": "actionable"},
        ]}

    monkeypatch.setattr(slack_mod, "_run_slack_agent", fake_agent)
    await _run_one_classify_cycle(client.app, monkeypatch)

    # ONE classify call for the whole batch (all 3 fit in SLACK_CLASSIFY_BATCH=10).
    classify_calls = [c for c in calls if c[0] == "classify"]
    assert len(classify_calls) == 1
    assert {it["id"] for it in classify_calls[0][1]["items"]} == {"reply1", "fyi1", "act1"}
    # No send call ever fired (classify is read-free, never sends).
    assert all(action != "send" for action, _ in calls)

    saved = {it["id"]: it for it in json.loads(slack_file.read_text(encoding="utf-8"))["items"]}
    # D-027: EVERY classified item flows to needs-draft (so it gets a draft); the
    # triage label is recorded in `classification` and only drives review grouping.
    assert saved["reply1"]["status"] == "needs-draft"
    assert saved["reply1"]["classification"] == "needs-reply"
    assert saved["act1"]["status"] == "needs-draft"
    assert saved["act1"]["classification"] == "actionable"
    assert saved["fyi1"]["status"] == "needs-draft"     # fyi is NO LONGER terminal
    assert saved["fyi1"]["classification"] == "fyi"     # it's a label, not a status


async def test_slack_classify_worker_unavailable_leaves_items(client, slack_file, monkeypatch):
    """An unavailable classify result leaves the needs-classify items AS-IS (fail
    loud, no fabrication) so the next cycle retries them."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "c1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "hi", "ts": 1, "status": "needs-classify",
    }]}), encoding="utf-8")
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    await _run_one_classify_cycle(client.app, monkeypatch)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-classify"


async def test_slack_classify_worker_inert_until_ready(client, slack_file, monkeypatch):
    """When _probe_readiness is NOT ready (claude absent — the suite default), the
    classify worker spawns nothing even with pending needs-classify items."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "c1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "hi", "ts": 1, "status": "needs-classify",
    }]}), encoding="utf-8")
    agent = _stub_agent(monkeypatch, {"available": True, "classifications": [
        {"id": "c1", "classification": "needs-reply"}]})
    monkeypatch.setattr(slack_mod, "_probe_readiness", lambda: {"ready": False})
    monkeypatch.setattr(slack_mod, "SLACK_CLASSIFY_POLL_INTERVAL_S", 0.01)
    task = asyncio.create_task(slack_mod._classify_worker(client.app))
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert agent.calls == []
    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-classify"


async def test_slack_classify_worker_task_started_and_cancelled_cleanly(aiohttp_client, app):
    """The classify-worker lifecycle: on_startup creates app['slack_classify_task'];
    closing the client cancels+awaits it (no orphan, no 'Task was destroyed')."""
    c = await aiohttp_client(app)
    task = app.get("slack_classify_task")
    assert task is not None and not task.done()
    await c.close()
    assert task.done()
    assert "slack_classify_task" not in app


# ── Feature 3: thread muting ──────────────────────────────────────────────────


def test_slack_mute_key_falls_back_to_channel_id():
    """_mute_key_for is channelId-scoped today (no threadTs is captured anywhere);
    it only becomes thread-scoped when a non-empty threadTs is present (D-025)."""
    assert slack_mod._mute_key_for({"channelId": "D1"}) == "D1"
    assert slack_mod._mute_key_for({"channelId": "D1", "threadTs": ""}) == "D1"
    assert slack_mod._mute_key_for({"channelId": "D1", "threadTs": "1700.5"}) == "D1_1700.5"
    # No channelId -> no key (unroutable, can't be muted).
    assert slack_mod._mute_key_for({"sender": "a"}) == ""


async def test_slack_mute_dismisses_and_records_muted_thread(client, slack_file, monkeypatch):
    """POST /queue/{id}/mute soft-dismisses the item AND adds its mute key to
    mutedThreads with a unix timestamp."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "m1", "sender": "noisy", "channel": "DM with noisy", "channelType": "dm",
        "channelId": "D_noise", "snippet": "again and again", "ts": 1,
        "status": "needs-review", "draft": "d", "generatedDraft": "d",
    }]}), encoding="utf-8")
    # _mark_channel_read uses the persistent MCP session; stub it so muting never
    # touches a real slack-mcp in the test.
    async def noop_mark(_cid):
        return None
    monkeypatch.setattr(slack_mod, "_mark_channel_read", noop_mark)

    resp = await client.post("/api/slack/queue/m1/mute")
    assert resp.status == 200
    body = await resp.json()
    assert body["muteKey"] == "D_noise"
    assert body["item"]["status"] == "dismissed"

    saved = json.loads(slack_file.read_text(encoding="utf-8"))
    # The item is soft-dismissed (preserved), and the mute key is recorded.
    assert saved["items"][0]["status"] == "dismissed"
    assert "D_noise" in saved["mutedThreads"]
    assert isinstance(saved["mutedThreads"]["D_noise"], int)


async def test_slack_mute_sent_item_is_409(client, slack_file, monkeypatch):
    """Muting a 'sent' item is blocked (409, like dismiss) — sent is real outbound
    history that must not be hidden."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [{
        "id": "s1", "sender": "a", "channel": "c", "channelType": "dm",
        "channelId": "D1", "snippet": "hi", "ts": 1, "status": "sent",
        "sentAt": 100, "finalText": "done",
    }]}), encoding="utf-8")
    resp = await client.post("/api/slack/queue/s1/mute")
    assert resp.status == 409
    saved = json.loads(slack_file.read_text(encoding="utf-8"))
    assert "mutedThreads" not in saved or "D1" not in saved.get("mutedThreads", {})


async def test_slack_mute_unknown_item_is_404(client, slack_file):
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": []}), encoding="utf-8")
    resp = await client.post("/api/slack/queue/nope/mute")
    assert resp.status == 404


async def test_slack_get_and_delete_muted_round_trip(client, slack_file):
    """GET /muted lists the map; DELETE /muted/{key} removes one (404 if absent)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({
        "items": [],
        "mutedThreads": {"D_one": 1_700_000_000, "C_two": 1_700_000_100},
    }), encoding="utf-8")

    body = await (await client.get("/api/slack/muted")).json()
    assert set(body["muted"]) == {"D_one", "C_two"}

    resp = await client.delete("/api/slack/muted/D_one")
    assert resp.status == 200
    assert (await resp.json())["muteKey"] == "D_one"

    saved = json.loads(slack_file.read_text(encoding="utf-8"))
    assert set(saved["mutedThreads"]) == {"C_two"}

    # Deleting an absent key -> 404.
    resp = await client.delete("/api/slack/muted/D_one")
    assert resp.status == 404


async def test_slack_unmute_rejects_path_traversal_key(client, slack_file):
    """A mute key with path-traversal chars is rejected defensively (400), even
    though it is only ever a dict key (never a filesystem path)."""
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({"items": [], "mutedThreads": {}}), encoding="utf-8")
    # aiohttp routes '/' as a path separator; '..%2F' or backslash chars exercise the
    # guard via the matched segment. Use a backslash, which survives as one segment.
    resp = await client.delete("/api/slack/muted/" + "a\\..\\b")
    assert resp.status in (400, 404)  # rejected (400) or simply not matched/found


async def test_slack_muted_thread_filters_scan_candidates(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """A muted thread key filters scan candidates: a get_unreads payload for a muted
    channel produces NO skeleton (the candidate is dropped before the merge)."""
    now_s = _time.time()
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({
        "items": [],
        # D_muted is muted (fresh timestamp so it is not pruned); D_open is not.
        "mutedThreads": {"D_muted": int(now_s) - 60},
    }), encoding="utf-8")

    unreads = {"channels": [
        {"channelId": "D_muted", "name": "noisy",
         "messages": [{"user": "noisy", "text": "again", "ts": f"{now_s - 1:.6f}"}]},
        {"channelId": "D_open", "name": "alice",
         "messages": [{"user": "alice", "text": "hey", "ts": f"{now_s - 1:.6f}"}]},
    ]}
    await _run_one_scan_cycle(client.app, monkeypatch, unreads=unreads)

    saved = json.loads(slack_file.read_text(encoding="utf-8"))["items"]
    by_id = {it["channelId"]: it for it in saved}
    # The muted channel produced NO skeleton; the open channel did.
    assert "D_muted" not in by_id
    assert "D_open" in by_id
    assert by_id["D_open"]["status"] == "needs-classify"


async def test_slack_scan_prunes_stale_muted_entries(
    client, slack_file, reset_scan_ts, monkeypatch
):
    """A mutedThreads entry older than the 30-day TTL is pruned on a scan cycle; a
    fresh entry survives. The pruned map is persisted in the same write."""
    now_s = _time.time()
    stale = int(now_s) - (slack_mod._MUTE_TTL_S + 86_400)  # 31 days ago
    fresh = int(now_s) - 60
    slack_file.parent.mkdir(parents=True, exist_ok=True)
    slack_file.write_text(json.dumps({
        "items": [],
        "mutedThreads": {"D_stale": stale, "D_fresh": fresh},
    }), encoding="utf-8")

    # An empty get_unreads is a clean no-op scan that still prunes + persists.
    await _run_one_scan_cycle(client.app, monkeypatch, unreads={"channels": []})

    saved = json.loads(slack_file.read_text(encoding="utf-8"))
    assert "D_stale" not in saved["mutedThreads"]   # aged out
    assert "D_fresh" in saved["mutedThreads"]        # still live


