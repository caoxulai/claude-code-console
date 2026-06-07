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
          other.jsonl               (first line type=queue-operation) -> NOT filtered
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
    _write_jsonl(other_dir / "other.jsonl", [
        {"type": "queue-operation"},
        {"type": "user", "message": {"content": "hi"}},
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
    # ...and the print-mode session in a NON-home dir is NOT filtered.
    assert "other" in ids

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
    assert data == {"sessionId": "renamed", "title": "Final name"}


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

    def slug_for(p: Path) -> str:
        return "-" + str(p).lstrip("/").replace("/", "-")

    # alpha: CLAUDE.md + sop/skill/docs + 2 sessions + 3 memories
    alpha = workspace / "alpha"
    alpha.mkdir()
    (alpha / "CLAUDE.md").write_text("# Alpha\n\nThe alpha project.\n", encoding="utf-8")
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

    Mirrors the real claude-web (ClaudeCodeConsole) and oncall-kpi
    (GlennBlackFalconOncallDashboard) mappings the UAT checks.
    """
    workspace = projects_layout["workspace"]
    _write_git_remote(workspace / "alpha", "ssh://git.amazon.com/pkg/ClaudeCodeConsole")
    _write_git_remote(
        workspace / "beta", "https://git.amazon.com/pkg/GlennBlackFalconOncallDashboard"
    )

    projects = await _get_projects(client)
    assert projects["alpha"]["codeUrl"] == "https://code.amazon.com/packages/ClaudeCodeConsole"
    assert (
        projects["beta"]["codeUrl"]
        == "https://code.amazon.com/packages/GlennBlackFalconOncallDashboard"
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


def test_code_url_for_project_strips_dot_git_suffix(tmp_path):
    """A trailing .git on the package segment is stripped from the package name."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_git_remote(proj, "ssh://git.amazon.com/pkg/ClaudeCodeConsole.git")
    assert (
        sessions_mod._code_url_for_project(proj)
        == "https://code.amazon.com/packages/ClaudeCodeConsole"
    )


def test_code_url_for_project_non_git_dir_is_none(tmp_path):
    """A directory that isn't a git repo yields codeUrl None."""
    proj = tmp_path / "plain"
    proj.mkdir()
    assert sessions_mod._code_url_for_project(proj) is None


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
