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
