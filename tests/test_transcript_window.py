"""Tail-windowed transcript reads + defensive int query params (D-078 items 26, 27d).

Two behaviours are pinned here:

  AC-11/AC-12 — GET /api/sessions/{id}/transcript?tail=true no longer reads and
      json-parses a whole (up to ~37MB) .jsonl per request. It seeks back a byte
      window from EOF, drops the first possibly-partial line and parses only the
      window, growing the window / falling back to the full read whenever the
      window can't cover offset+limit records. The window MUST return
      byte-for-byte the same records the old full read returned — "faster and
      roughly right" is a vanished message in the viewer — so every case here is
      compared against a GOLDEN oracle that runs the OLD algorithm in-test on the
      same file. The envelope keeps `messages`/`offset`/`limit`/`total`.

  AC-14 — `?limit=abc` (or a negative offset) on /api/sessions and the transcript
      endpoint falls back to the documented default instead of raising a 500.

Deliberately a separate module from the tests/test_server.py monolith: its
existing transcript tests (test_server.py:361-411) are the backward-compat guard
and must keep passing UNMODIFIED.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent.parent))
import server.routes.sessions as sessions_mod  # noqa: E402


SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ─── Helpers ────────────────────────────────────────────────────────────────


class _NullWsManager:
    async def broadcast(self, event_type, data=None):
        return None


def _build_app(default_cwd: Path) -> web.Application:
    """A bare Application with sessions.register() wired (mirrors test_session_cleanup)."""
    app = web.Application()
    app["ws_manager"] = _NullWsManager()
    app["default_cwd"] = default_cwd
    app["_worker_registry"] = []
    sessions_mod.register(app)
    return app


def _write_transcript(base: Path, name: str, lines: list[str]) -> Path:
    """Write raw JSONL lines (some may be deliberately malformed) into a project bucket."""
    proj = base / "-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _record_lines(count: int, pad: int = 400) -> list[str]:
    """`count` records of ~430 bytes each, so a few thousand exceed the tail window.

    Real transcript records are assistant turns of a few KB; the padding keeps the
    synthetic file representative enough that the default window covers only part
    of it (otherwise the window path would never be exercised).
    """
    return [
        json.dumps({"type": "assistant", "i": i, "pad": "x" * pad})
        for i in range(count)
    ]


_REAL_FULL_READ = sessions_mod._read_transcript_records


def _golden(path: Path, offset: int, limit: int, tail: bool) -> tuple[list, int]:
    """The OLD full-read implementation, verbatim, as the equivalence oracle.

    Uses the module function captured at import time so a test that spies on
    sessions_mod._read_transcript_records doesn't count the oracle's own read.
    """
    records = _REAL_FULL_READ(path)
    total = len(records)
    if tail:
        end = total - offset
        start = max(0, end - limit)
        return records[start:max(start, end)], total
    return records[offset:offset + limit], total


def _line_start_offsets(path: Path) -> tuple[list[int], int]:
    """Byte offset of every line start in the file, plus its size."""
    data = path.read_bytes()
    offsets = [0]
    for i, byte in enumerate(data):
        if byte == 0x0A and i + 1 < len(data):
            offsets.append(i + 1)
    return offsets, len(data)


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    """Point the project-dir scan at tmp_path and drop the sessions cache.

    Never touches the real ~/.claude tree.
    """
    base = tmp_path / "projects"
    base.mkdir()
    monkeypatch.setattr(sessions_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(sessions_mod, "WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(sessions_mod, "_sessions_cache", sessions_mod._SessionsCache())
    return base


@pytest.fixture
def base(tmp_path):
    return tmp_path / "projects"


@pytest.fixture
async def client(aiohttp_client, tmp_path):
    return await aiohttp_client(_build_app(tmp_path))


async def _fetch(client, offset: int, limit: int, tail: bool = True, sid: str = SESSION_ID):
    params = {"limit": str(limit), "offset": str(offset)}
    if tail:
        params["tail"] = "true"
    resp = await client.get(f"/api/sessions/{sid}/transcript", params=params)
    assert resp.status == 200
    return await resp.json()


# ─── AC-11: golden equivalence ──────────────────────────────────────────────


async def test_tail_window_matches_full_read_on_large_transcript(client, base):
    """A multi-hundred-KB transcript: tail=true&limit=200 == the old full-read slice."""
    path = _write_transcript(base, SESSION_ID, _record_lines(4000))
    assert path.stat().st_size > sessions_mod._TRANSCRIPT_TAIL_WINDOW_BYTES

    data = await _fetch(client, offset=0, limit=200)
    expected, total = _golden(path, 0, 200, tail=True)

    assert data["messages"] == expected
    assert len(data["messages"]) == 200
    assert data["total"] == total


async def test_tail_window_matches_full_read_with_malformed_line(client, base):
    """A malformed line mid-file is skipped, never aborts, and the window still
    returns exactly the records the full read would (tolerance re-pinned)."""
    lines = _record_lines(4000)
    lines[3900] = "{not valid json"       # inside the tail window
    lines[10] = "{also not valid json"    # far outside it
    lines[3999] = "{trailing garbage"     # the very last line
    path = _write_transcript(base, SESSION_ID, lines)

    for offset in (0, 200):
        data = await _fetch(client, offset=offset, limit=200)
        expected, _total = _golden(path, offset, 200, tail=True)
        assert data["messages"] == expected, f"offset={offset}"
        # The malformed record is absent, its neighbours survive.
        assert all(isinstance(m, dict) for m in data["messages"])


async def test_tail_window_offset_paging_matches_full_read(client, base):
    """Backward offset paging (offset counts records from EOF) stays exact."""
    path = _write_transcript(base, SESSION_ID, _record_lines(4000))

    for offset in (0, 200, 400, 1000, 3900, 4000, 4200):
        data = await _fetch(client, offset=offset, limit=200)
        expected, total = _golden(path, offset, 200, tail=True)
        assert data["messages"] == expected, f"offset={offset}"
        assert data["total"] == total, f"offset={offset}"


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("records_available", [199, 200, 299])
async def test_tail_window_boundary_off_by_one(client, base, monkeypatch,
                                              records_available, delta):
    """Window edges: the boundary lands exactly on a line start (delta 0) and one
    byte either side, with the window holding exactly limit-1 / limit / limit+99
    complete records after the partial-line drop. Every combination must equal the
    full read — an off-by-one here silently drops a message.
    """
    path = _write_transcript(base, SESSION_ID, _record_lines(1000))
    offsets, size = _line_start_offsets(path)

    # A window starting at line k drops line k (the "possibly partial" first
    # line), leaving len(offsets) - k - 1 complete records.
    k = len(offsets) - records_available - 1
    window = size - offsets[k] + delta
    monkeypatch.setattr(sessions_mod, "_TRANSCRIPT_TAIL_WINDOW_BYTES", window)

    for offset in (0, 100, 200):
        data = await _fetch(client, offset=offset, limit=200)
        expected, _total = _golden(path, offset, 200, tail=True)
        assert data["messages"] == expected, f"avail={records_available} d={delta} off={offset}"


async def test_tail_window_drops_partial_line_that_parses_as_json(client, base, monkeypatch):
    """The dropped first line of the window is load-bearing when the partial
    remnant happens to be VALID json.

    A cut inside a long numeric line leaves a shorter number, which json.loads
    accepts — so without the drop the window gains a SPURIOUS record, is judged
    long enough, and the viewer renders a fabricated message in place of the
    oldest real one. Engineered so exactly limit-1 complete records follow the
    cut line: keeping the remnant reaches `limit` and returns the wrong window.
    """
    lines = _record_lines(1000)
    lines[800] = "1" * 300  # a valid JSON number; any suffix is also valid JSON
    path = _write_transcript(base, SESSION_ID, lines)

    offsets, size = _line_start_offsets(path)
    # Start the window 150 bytes into line 800 → 199 complete records follow.
    monkeypatch.setattr(sessions_mod, "_TRANSCRIPT_TAIL_WINDOW_BYTES",
                        size - (offsets[800] + 150))

    data = await _fetch(client, offset=0, limit=200)
    expected, _total = _golden(path, 0, 200, tail=True)

    assert data["messages"] == expected
    assert len(data["messages"]) == 200
    # The oldest returned record is the real (whole) numeric line, not a remnant.
    assert data["messages"][0] == int("1" * 300)


async def test_tail_window_ignores_unicode_line_separators_inside_records(client, base):
    """A record whose text contains a RAW U+2028/U+2029 (or \\x0b/\\x0c/\\x85) must
    stay ONE record.

    Found against the real 50MB transcript: Claude writes JSON with
    ensure_ascii=False, so raw U+2028 LINE SEPARATOR chars occur inside message
    content. `str.splitlines()` treats those as line breaks (the full read's
    `for line in fh` does not), which shattered a record into two unparseable
    fragments and shifted the whole window — 200 returned messages, all wrong.
    """
    lines = _record_lines(1000)
    lines[900] = json.dumps(
        {"type": "assistant", "i": 900, "text": "before after more\x0bmore"},
        ensure_ascii=False,
    )
    path = _write_transcript(base, SESSION_ID, lines)

    data = await _fetch(client, offset=0, limit=200)
    expected, total = _golden(path, 0, 200, tail=True)

    assert data["messages"] == expected
    assert len(data["messages"]) == 200
    assert data["total"] == total == 1000
    assert data["messages"][0]["i"] == 800
    # The separator-bearing record survived whole, not split into fragments.
    assert any(m.get("i") == 900 and m["text"].startswith("before") for m in data["messages"])


async def test_tail_window_grows_when_initial_window_too_small(client, base, monkeypatch):
    """A window far too small for offset+limit must grow / fall back — never
    silently return fewer records than the full read would."""
    path = _write_transcript(base, SESSION_ID, _record_lines(2000))
    monkeypatch.setattr(sessions_mod, "_TRANSCRIPT_TAIL_WINDOW_BYTES", 512)

    data = await _fetch(client, offset=0, limit=200)
    expected, _total = _golden(path, 0, 200, tail=True)
    assert len(data["messages"]) == 200
    assert data["messages"] == expected


async def test_tail_window_read_lines_tolerates_malformed(tmp_path):
    """The window parser has the SAME skip-malformed-lines tolerance as the
    full-read helper (a bad line is skipped, it never aborts the read)."""
    path = tmp_path / "sess.jsonl"
    path.write_text(
        json.dumps({"type": "system", "session_id": "s1"}) + "\n"
        + "{not valid json\n"
        + json.dumps({"type": "result", "result": "ok"}) + "\n",
        encoding="utf-8",
    )

    records, is_full = sessions_mod._read_transcript_tail(path, 1)

    assert records == [
        {"type": "system", "session_id": "s1"},
        {"type": "result", "result": "ok"},
    ]
    assert is_full is True  # tiny file → whole-file read


# ─── AC-11: proof the whole file is not read/parsed ─────────────────────────


async def test_tail_does_not_full_read_or_parse_whole_file(client, base, monkeypatch):
    """Seam proof: the tail path never calls the full-read helper, and parses far
    fewer lines than the file holds.

    Records are sized like real ones (~2.4KB — measured: 50MB/21k records) so the
    default window covers only a few hundred of the 4000 records.
    """
    path = _write_transcript(base, SESSION_ID, _record_lines(4000, pad=2300))
    assert path.stat().st_size > 8 * sessions_mod._TRANSCRIPT_TAIL_WINDOW_BYTES

    full_read_calls: list[Path] = []
    real_full_read = sessions_mod._read_transcript_records

    def spy_full_read(p):
        full_read_calls.append(p)
        return real_full_read(p)

    parsed_lines: list[int] = []
    real_parse = sessions_mod._parse_transcript_lines

    def spy_parse(lines):
        lines = list(lines)
        parsed_lines.append(len(lines))
        return real_parse(lines)

    monkeypatch.setattr(sessions_mod, "_read_transcript_records", spy_full_read)
    monkeypatch.setattr(sessions_mod, "_parse_transcript_lines", spy_parse)

    data = await _fetch(client, offset=0, limit=200)

    assert len(data["messages"]) == 200
    assert full_read_calls == [], "tail path must not fall back to the full read"
    assert sum(parsed_lines) < 1000, (
        f"parsed {sum(parsed_lines)} lines of 4000 — the window is not bounded")


async def test_non_tail_request_still_uses_full_read(client, base, monkeypatch):
    """tail=false keeps the existing full-read path (exact total, head paging)."""
    path = _write_transcript(base, SESSION_ID, _record_lines(500))

    calls: list[Path] = []
    real_full_read = sessions_mod._read_transcript_records

    def spy_full_read(p):
        calls.append(p)
        return real_full_read(p)

    monkeypatch.setattr(sessions_mod, "_read_transcript_records", spy_full_read)

    data = await _fetch(client, offset=10, limit=5, tail=False)
    expected, total = _golden(path, 10, 5, tail=False)

    assert data["messages"] == expected
    assert data["total"] == total == 500
    assert calls == [path]


# ─── AC-12: envelope ────────────────────────────────────────────────────────


async def test_envelope_keys_present_and_total_correct(client, base):
    """messages/offset/limit/total all present; total == the record count for
    clean input on both the windowed and the full-read path."""
    path = _write_transcript(base, SESSION_ID, _record_lines(4000))

    data = await _fetch(client, offset=7, limit=13)
    assert set(data) == {"messages", "offset", "limit", "total"}
    assert data["offset"] == 7
    assert data["limit"] == 13
    assert data["total"] == 4000

    small = _write_transcript(base, "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                              _record_lines(9))
    data_small = await _fetch(client, offset=0, limit=200,
                              sid="bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
    assert data_small["total"] == 9
    assert len(data_small["messages"]) == 9
    assert small.exists()


def test_count_transcript_lines_counts_without_parsing(tmp_path):
    """The cheap no-parse total counts every line, including a malformed one, and
    a final line with no trailing newline."""
    path = tmp_path / "sess.jsonl"
    path.write_text('{"a": 1}\n{bad\n{"b": 2}', encoding="utf-8")
    assert sessions_mod._count_transcript_lines(path) == 3


# ─── AC-14: defensive int query params ──────────────────────────────────────


@pytest.mark.parametrize("bad", ["abc", "", "-5", "1.5", "9e9x"])
async def test_transcript_bad_limit_falls_back_to_default(client, base, bad):
    """?limit=<junk> must not 500 — it falls back to the documented default 200."""
    _write_transcript(base, SESSION_ID, _record_lines(5))

    resp = await client.get(
        f"/api/sessions/{SESSION_ID}/transcript",
        params={"limit": bad, "offset": "0", "tail": "true"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["limit"] == 200
    assert len(data["messages"]) == 5


@pytest.mark.parametrize("bad", ["abc", "", "-5"])
async def test_transcript_bad_offset_falls_back_to_default(client, base, bad):
    _write_transcript(base, SESSION_ID, _record_lines(5))

    resp = await client.get(
        f"/api/sessions/{SESSION_ID}/transcript",
        params={"limit": "2", "offset": bad, "tail": "true"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["offset"] == 0
    assert [m["i"] for m in data["messages"]] == [3, 4]


@pytest.mark.parametrize("params", [
    {"limit": "abc"},
    {"offset": "abc"},
    {"limit": "-1"},
    {"offset": "-1"},
    {"limit": "abc", "offset": "xyz"},
])
async def test_list_sessions_bad_int_params_fall_back(client, base, params):
    """/api/sessions?limit=abc returns the default page, never a 500 traceback."""
    _write_transcript(base, SESSION_ID, _record_lines(3))

    resp = await client.get("/api/sessions", params=params)
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["sessions"], list)


def test_int_param_helper():
    """The private helper: valid ints pass, junk/negative degrade to the default."""
    parse = sessions_mod._int_param
    assert parse({"limit": "5"}, "limit", 50) == 5
    assert parse({}, "limit", 50) == 50
    assert parse({"limit": "abc"}, "limit", 50) == 50
    assert parse({"limit": "-3"}, "limit", 50) == 50
    assert parse({"limit": "0"}, "limit", 50) == 0
