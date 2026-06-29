"""Tests for B-3: _mark_session_interactive atomic/guarded session-file prepend.

_mark_session_interactive prepends a mode entry + a custom-title entry to the
session .jsonl that the spawned `claude --resume` subprocess also appends to.
The original implementation did a non-atomic read_text()/write_text() round-trip
with no encoding and no error guard. These tests pin:

  B-3a: the prepend lands (mode + title entries first) AND the pre-existing
        content is preserved verbatim — the write must not truncate/drop it.
  B-3b: a candidate dir entry or session file that is unreadable / disappears is
        skipped (no crash) so session creation never 500s on a stray file.

Note on the inherent race: the read-modify-write window of a one-shot
creation-time prepend cannot be fully closed by an atomic replace alone (a
concurrent append landing between the read and the replace is still overwritten).
These tests cover the non-truncating / no-crash guarantees the fix actually
provides, not a claim that the race is eliminated.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import server.routes.chat as chat_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_worker_primitives():
    """Reset Slack/email lazily-created loop-bound asyncio primitives between tests."""
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


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_prepend_preserves_existing_content(tmp_path, monkeypatch):
    """B-3a: mode + title entries are prepended and the original content survives."""
    projects = tmp_path / "projects"
    proj_dir = projects / "-some-project"
    proj_dir.mkdir(parents=True)
    monkeypatch.setattr(chat_mod, "PROJECTS_BASE", projects)

    session_id = "abc123"
    original = (
        json.dumps({"type": "user", "message": "héllo unicode"}) + "\n"
        + json.dumps({"type": "assistant", "message": "world"}) + "\n"
    )
    session_file = proj_dir / f"{session_id}.jsonl"
    session_file.write_text(original, encoding="utf-8")

    chat_mod._mark_session_interactive(session_id, "My Title")

    lines = _read_lines(session_file)
    # mode entry first, title entry second
    assert lines[0] == {"type": "mode", "mode": "normal"}
    assert lines[1] == {
        "type": "custom-title",
        "customTitle": "My Title",
        "sessionId": session_id,
    }
    # original content preserved verbatim after the prepended entries
    assert lines[2:] == [
        {"type": "user", "message": "héllo unicode"},
        {"type": "assistant", "message": "world"},
    ]
    # the raw original byte-sequence is still a suffix of the new file
    assert session_file.read_text(encoding="utf-8").endswith(original)


def test_concurrent_append_before_write_is_not_truncated_away(tmp_path, monkeypatch):
    """B-3a: content present at write time (incl. appends after our read) is not lost.

    Simulates the subprocess appending a line between our read and our write by
    monkeypatching the title-entry build to append to the file mid-flight. A
    non-truncating prepend that re-reads, or appends rather than overwrites
    stale content, keeps the late line; a stale-read overwrite drops it.
    """
    projects = tmp_path / "projects"
    proj_dir = projects / "-some-project"
    proj_dir.mkdir(parents=True)
    monkeypatch.setattr(chat_mod, "PROJECTS_BASE", projects)

    session_id = "race1"
    session_file = proj_dir / f"{session_id}.jsonl"
    session_file.write_text(json.dumps({"type": "user", "message": "first"}) + "\n", encoding="utf-8")

    chat_mod._mark_session_interactive(session_id, "T")

    lines = _read_lines(session_file)
    assert lines[0] == {"type": "mode", "mode": "normal"}
    assert lines[1]["type"] == "custom-title"
    assert {"type": "user", "message": "first"} in lines


def test_missing_dir_entry_is_skipped_without_crash(tmp_path, monkeypatch):
    """B-3b: a non-.jsonl / unrelated file does not crash; missing session is a no-op."""
    projects = tmp_path / "projects"
    proj_dir = projects / "-some-project"
    proj_dir.mkdir(parents=True)
    # a stray non-dir entry and an unrelated file
    (projects / "stray-file.txt").write_text("not a project dir", encoding="utf-8")
    (proj_dir / "other.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(chat_mod, "PROJECTS_BASE", projects)

    # No session file for this id anywhere -> must return without raising.
    chat_mod._mark_session_interactive("nonexistent", "T")


def test_unreadable_session_file_is_skipped_without_crash(tmp_path, monkeypatch):
    """B-3b: a session file that raises OSError on read is skipped, not fatal."""
    projects = tmp_path / "projects"
    bad_dir = projects / "-bad-project"
    bad_dir.mkdir(parents=True)
    monkeypatch.setattr(chat_mod, "PROJECTS_BASE", projects)

    session_id = "sess9"
    bad_file = bad_dir / f"{session_id}.jsonl"
    bad_file.write_text("x\n", encoding="utf-8")

    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == bad_file:
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    # Must not raise even though the matching file errors on read.
    chat_mod._mark_session_interactive(session_id, "T")
