"""Bounded-cache tests for the three unbounded in-process caches (D-078 item 27a-c).

Each of these was an unbounded-growth leak in a long-lived server process:

  (a) `agents._PARSE_CACHE` — an etag-keyed parse memo. Every write to a role's
      context .md mints a FRESH etag, so the old key was never reachable again
      yet stayed resident forever. Bounded two ways: a MISS for
      (project_dir, name, etag) evicts every other key for that same
      (project_dir, name) — the actual churn driver — and a hard
      `_PARSE_CACHE_MAX` cap evicts oldest-inserted as a backstop.
  (b) `usage._cache` — records/seen/file_mtimes grew monotonically, including
      records whose source lines were deleted or whose file was removed. A
      periodic FULL reconcile rebuilds all three TOGETHER from the files
      currently on disk (records and `seen` must move in LOCKSTEP — trimming
      records while leaving orphaned UUIDs in `seen` would suppress a record's
      reappearance, the D-037 invariant).
  (c) `SessionManager._mgr_locks` — one asyncio.Lock per session id, never
      removed. Cleanup is REFCOUNTED: an entry is dropped only when no
      lifecycle op is in flight for that id AND the id is gone from
      `_sessions` (i.e. after teardown). Popping on every release would strand
      a queued waiter on an orphaned Lock while a new caller minted a second
      Lock for the same id — exactly the double-spawn race the lock prevents.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.routes.agents as agents_mod  # noqa: E402
import server.routes.usage as usage_mod  # noqa: E402
import server.session_manager as session_mod  # noqa: E402


# ─── (a) agents._PARSE_CACHE ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_parse_cache(monkeypatch):
    """The memo is module-level; give each test its own plain dict.

    Kept a PLAIN dict deliberately (tests/test_agents.py monkeypatches it with
    `{}`), so the bounding lives in the write path, not in the container type.
    """
    monkeypatch.setattr(agents_mod, "_PARSE_CACHE", {})


def _write_context(project_dir: Path, name: str, body: str) -> Path:
    d = project_dir / ".claude" / "agent-context"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_parse_cache_evicts_stale_etag_keys_for_same_file(tmp_path):
    """Repeated context_summary calls across CHANGING etags for one file leave
    exactly one cached key — the current etag's — and the stale key is gone."""
    project = tmp_path / "proj"
    keys_seen = []
    for i in range(12):
        path = _write_context(project, "backend-dev", f"# ctx\n- 2026-07-0{i % 9 + 1}: entry {i}.\n")
        # Force a distinct etag per iteration (etag is st_mtime_ns).
        os.utime(path, ns=(1_700_000_000_000_000_000 + i, 1_700_000_000_000_000_000 + i))
        summary = agents_mod.context_summary(project, "backend-dev")
        assert summary["entryCount"] == 1
        keys_seen.append((str(project), "backend-dev", f"{1_700_000_000_000_000_000 + i}"))

    assert len(agents_mod._PARSE_CACHE) == 1, agents_mod._PARSE_CACHE.keys()
    assert keys_seen[-1] in agents_mod._PARSE_CACHE
    for stale in keys_seen[:-1]:
        assert stale not in agents_mod._PARSE_CACHE


def test_parse_cache_hard_cap_across_many_files(tmp_path, monkeypatch):
    """A hard cap bounds the memo even when every key is a DIFFERENT file (so
    the same-file prefix eviction never fires), evicting oldest-inserted."""
    assert agents_mod._PARSE_CACHE_MAX > 0
    monkeypatch.setattr(agents_mod, "_PARSE_CACHE_MAX", 5)

    project = tmp_path / "proj"
    for i in range(20):
        _write_context(project, f"role-{i}", f"# ctx\n- 2026-07-01: entry for role {i}.\n")
        agents_mod.context_summary(project, f"role-{i}")

    assert len(agents_mod._PARSE_CACHE) <= 5
    # Oldest-inserted went first: the newest key is still resident.
    assert (str(project), "role-19", agents_mod.filestore.etag_for(
        project / ".claude" / "agent-context" / "role-19.md")) in agents_mod._PARSE_CACHE
    assert not any(k[1] == "role-0" for k in agents_mod._PARSE_CACHE)


def test_parse_cache_still_reuses_the_parse_for_an_unchanged_file(tmp_path, monkeypatch):
    """Bounding must not defeat the memo: an UNCHANGED .md still hits."""
    project = tmp_path / "proj"
    _write_context(project, "backend-dev", "# ctx\n- 2026-07-01: entry one.\n")

    calls = []
    real_parse = agents_mod.parse_context_entries

    def counting_parse(content: str):
        calls.append(1)
        return real_parse(content)

    monkeypatch.setattr(agents_mod, "parse_context_entries", counting_parse)

    agents_mod.context_summary(project, "backend-dev")
    first = len(calls)
    agents_mod.context_summary(project, "backend-dev")
    assert len(calls) == first, "unchanged .md must reuse the memoized parse"


# ─── (b) usage._cache ─────────────────────────────────────────────────────────


def _usage_record(uid: str, inp: int = 100) -> str:
    return json.dumps({
        "type": "assistant",
        "uuid": uid,
        "timestamp": "2026-07-01T10:00:00.000Z",
        "isSidechain": False,
        "cwd": "/home/tester/proj",
        "message": {
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": inp,
                "output_tokens": 10,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    })


@pytest.fixture
def usage_tree(tmp_path, monkeypatch) -> Path:
    base = tmp_path / "claude_projects"
    proj = base / "-home-tester-proj"
    proj.mkdir(parents=True)
    (proj / "a.jsonl").write_text(_usage_record("uuid-a1") + "\n" + _usage_record("uuid-a2") + "\n",
                                  encoding="utf-8")
    (proj / "b.jsonl").write_text(_usage_record("uuid-b1") + "\n", encoding="utf-8")
    monkeypatch.setattr(usage_mod, "CLAUDE_PROJECTS_BASE", base)
    monkeypatch.setattr(usage_mod, "_cache", usage_mod._Cache())
    return base


async def _refresh(monkeypatch, *, reconcile: bool) -> None:
    """Expire the response TTL and force (or suppress) the periodic reconcile."""
    monkeypatch.setattr(usage_mod, "_RECONCILE_INTERVAL_S", 0 if reconcile else 10_000)
    usage_mod._cache.updated_at = 0
    await usage_mod._ensure_cache()


async def test_usage_reconcile_drops_records_whose_lines_are_gone(usage_tree, monkeypatch):
    """A REWRITTEN transcript (lines removed) leaves stale records + orphaned
    UUIDs on the incremental path; the periodic reconcile rebuilds records,
    `seen` and file_mtimes together from what is on disk."""
    await usage_mod._ensure_cache()
    assert len(usage_mod._cache.records) == 3
    assert usage_mod._cache.seen == {"uuid-a1", "uuid-a2", "uuid-b1"}

    # a.jsonl is compacted down to a single record (a2 is gone for good).
    (usage_tree / "-home-tester-proj" / "a.jsonl").write_text(
        _usage_record("uuid-a1") + "\n", encoding="utf-8")

    await _refresh(monkeypatch, reconcile=False)
    # Incremental alone cannot know a2's line vanished (documents the leak).
    assert len(usage_mod._cache.records) == 3

    await _refresh(monkeypatch, reconcile=True)
    assert len(usage_mod._cache.records) == 2
    # LOCKSTEP: the orphaned UUID left `seen` too, so a re-import would count.
    assert usage_mod._cache.seen == {"uuid-a1", "uuid-b1"}
    assert set(usage_mod._cache.file_mtimes) == {
        str(usage_tree / "-home-tester-proj" / "a.jsonl"),
        str(usage_tree / "-home-tester-proj" / "b.jsonl"),
    }


async def test_usage_reconcile_evicts_deleted_file_state(usage_tree, monkeypatch):
    """After a reconcile, nothing about a DELETED file survives — records,
    `seen` and file_mtimes all shrink consistently."""
    await usage_mod._ensure_cache()
    deleted = usage_tree / "-home-tester-proj" / "b.jsonl"
    deleted.unlink()

    await _refresh(monkeypatch, reconcile=True)

    assert str(deleted) not in usage_mod._cache.file_mtimes
    assert all(r.source_file != str(deleted) for r in usage_mod._cache.records)
    assert len(usage_mod._cache.records) == 2
    assert usage_mod._cache.seen == {"uuid-a1", "uuid-a2"}


async def test_usage_reconcile_fires_only_after_the_interval(usage_tree, monkeypatch):
    """The reconcile is periodic, not per-request: a refresh inside the window
    stays on the cheap incremental path, one past it forces the full rebuild."""
    seen_flags: list[bool] = []
    real_scan = usage_mod._scan_incremental

    def spy(prev_records, prev_file_mtimes, prev_seen, force_full=False):
        seen_flags.append(force_full)
        return real_scan(prev_records, prev_file_mtimes, prev_seen, force_full=force_full)

    monkeypatch.setattr(usage_mod, "_scan_incremental", spy)
    monkeypatch.setattr(usage_mod, "_RECONCILE_INTERVAL_S", 10_000)

    await usage_mod._ensure_cache()          # cold scan is a full one anyway
    usage_mod._cache.updated_at = 0
    await usage_mod._ensure_cache()          # inside the window → incremental
    assert seen_flags[-1] is False

    monkeypatch.setattr(usage_mod, "_RECONCILE_INTERVAL_S", 0)
    usage_mod._cache.updated_at = 0
    await usage_mod._ensure_cache()          # past the window → reconcile
    assert seen_flags[-1] is True

    # Responses stay correct across both paths.
    assert usage_mod._cache.responses["usage"]["total"]["messages"] == 3


# ─── (c) SessionManager._mgr_locks ────────────────────────────────────────────


class _CacheBoundsFakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _CacheBoundsFakeProc:
    """Mimics a live subprocess: returncode None → PersistentSession.alive."""
    returncode = None

    def __init__(self):
        self.stdin = _CacheBoundsFakeStdin()
        self.stdout = None

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


@pytest.fixture
def fake_start(monkeypatch):
    """Patch PersistentSession.start/stop so no subprocess is ever spawned."""
    starts: list[str | None] = []

    async def _start(self):
        starts.append(self.session_id)
        await asyncio.sleep(0)
        self._proc = _CacheBoundsFakeProc()
        self._started = True

    async def _stop(self):
        self._proc = None
        self._started = False

    monkeypatch.setattr(session_mod.PersistentSession, "start", _start)
    monkeypatch.setattr(session_mod.PersistentSession, "stop", _stop)
    return starts


async def test_mgr_locks_do_not_grow_across_stopped_sessions(fake_start):
    """Many create→stop cycles must leave no per-id Lock behind."""
    mgr = session_mod.SessionManager()
    for i in range(50):
        await mgr.get_or_create(session_id=f"sess-{i}")
        await mgr.stop(f"sess-{i}")

    assert mgr._sessions == {}
    assert mgr._mgr_locks == {}, f"leaked {len(mgr._mgr_locks)} locks"


async def test_mgr_lock_retained_for_a_live_session(fake_start):
    """A LIVE id keeps its lock, and `_lock_for` returns the SAME object (the
    get-or-create semantics other call sites depend on)."""
    mgr = session_mod.SessionManager()
    await mgr.get_or_create(session_id="live-1")

    lock = mgr._lock_for("live-1")
    assert mgr._lock_for("live-1") is lock
    assert mgr._mgr_locks["live-1"] is lock

    # Another lifecycle op on the live id must reuse that very Lock, not mint one.
    await mgr.respawn("live-1")
    assert mgr._mgr_locks["live-1"] is lock

    await mgr.stop("live-1")
    assert "live-1" not in mgr._mgr_locks


async def test_same_id_lifecycle_ops_still_serialize(fake_start):
    """Two concurrent get_or_create for the same id spawn ONCE: the second
    waits on the per-id lock and then observes the stored live session."""
    mgr = session_mod.SessionManager()

    a, b = await asyncio.gather(
        mgr.get_or_create(session_id="dup"),
        mgr.get_or_create(session_id="dup"),
    )

    assert len(fake_start) == 1, f"double spawn: {fake_start}"
    assert a is b is mgr._sessions["dup"]


async def test_queued_waiter_keeps_the_lock_entry_alive(monkeypatch):
    """While one op holds the lock and another is queued, the entry must stay —
    popping on release would hand the waiter an orphaned Lock while the next
    caller mints a second one for the same id (the double-spawn race)."""
    mgr = session_mod.SessionManager()
    gate = asyncio.Event()
    in_stop = asyncio.Event()

    seeded = session_mod.PersistentSession(session_id="held")
    seeded._proc = _CacheBoundsFakeProc()
    seeded._started = True

    async def slow_stop():
        in_stop.set()
        await gate.wait()

    seeded.stop = slow_stop
    mgr._sessions["held"] = seeded

    stopper = asyncio.create_task(mgr.stop("held"))
    await in_stop.wait()

    lock = mgr._mgr_locks["held"]
    waiter = asyncio.create_task(mgr.stop("held"))
    await asyncio.sleep(0)
    # Second caller is queued on the SAME Lock object, not a fresh one.
    assert mgr._mgr_locks["held"] is lock
    assert lock.locked()

    gate.set()
    await asyncio.gather(stopper, waiter)
    # Only once BOTH ops finished and the id left _sessions is the entry dropped.
    assert mgr._mgr_locks == {}


async def test_late_caller_cannot_bypass_a_lock_still_in_use(monkeypatch):
    """The race the refcount protects, exercised end-to-end.

    Op1 holds the lock while op2 queues. Op1 releases; op2 acquires and, while
    it is INSIDE its critical section, a third caller arrives. If the entry were
    popped on release, op3 would mint a SECOND Lock for the same id and spawn
    concurrently with op2 — the double-spawn race. Mutual exclusion is asserted
    directly (max concurrent occupants of the critical section == 1).
    """
    mgr = session_mod.SessionManager()

    class _DeadProc(_CacheBoundsFakeProc):
        returncode = 0  # not alive → every caller falls through to a spawn

    occupancy = {"now": 0, "max": 0}
    starts: list[str | None] = []
    late: dict[str, asyncio.Task | None] = {"task": None}

    async def _start(self):
        starts.append(self.session_id)
        occupancy["now"] += 1
        occupancy["max"] = max(occupancy["max"], occupancy["now"])
        if len(starts) == 2 and late["task"] is None:
            # Arrives AFTER op1 released — the moment a pop-on-release would
            # have deleted the entry that op2 is currently holding.
            late["task"] = asyncio.create_task(mgr.get_or_create(session_id="held"))
        for _ in range(6):
            await asyncio.sleep(0)  # stay inside the critical section a while
        self._proc = _DeadProc()
        self._started = True
        occupancy["now"] -= 1

    async def _stop(self):
        self._proc = None
        self._started = False

    monkeypatch.setattr(session_mod.PersistentSession, "start", _start)
    monkeypatch.setattr(session_mod.PersistentSession, "stop", _stop)

    await asyncio.gather(
        mgr.get_or_create(session_id="held"),
        mgr.get_or_create(session_id="held"),
    )
    assert late["task"] is not None, "the late caller was never launched"
    await late["task"]

    assert len(starts) == 3
    assert occupancy["max"] == 1, (
        f"{occupancy['max']} lifecycle ops were inside the same id's critical "
        "section at once — the per-id lock stopped serializing"
    )


async def test_stopping_unknown_ids_leaks_nothing(fake_start):
    """stop() on an id that was never registered (a stale client, a double-stop)
    must not create a permanent lock entry — the cheapest growth path of all."""
    mgr = session_mod.SessionManager()
    for i in range(100):
        await mgr.stop(f"ghost-{i}")

    assert mgr._mgr_locks == {}
    assert mgr._lock_waiters == {}


async def test_failed_spawn_leaks_no_lock(monkeypatch):
    """A start() that raises leaves nothing in _sessions, so the lock entry must
    be released on the exception path too (the finally, not the happy path)."""
    mgr = session_mod.SessionManager()

    async def _boom(self):
        raise RuntimeError("spawn failed")

    async def _stop(self):
        self._proc = None
        self._started = False

    monkeypatch.setattr(session_mod.PersistentSession, "start", _boom)
    monkeypatch.setattr(session_mod.PersistentSession, "stop", _stop)

    for i in range(10):
        with pytest.raises(RuntimeError):
            await mgr.get_or_create(session_id=f"fail-{i}")

    assert mgr._sessions == {}
    assert mgr._mgr_locks == {}
    assert mgr._lock_waiters == {}


async def test_stop_all_drops_the_lock_entries_too(fake_start):
    """stop_all() stops sessions directly (not via self.stop()), so it must also
    shed their lock entries — otherwise shutdown-path teardown still leaks."""
    mgr = session_mod.SessionManager()
    for i in range(5):
        await mgr.get_or_create(session_id=f"bulk-{i}")
    assert len(mgr._mgr_locks) == 5

    await mgr.stop_all()

    assert mgr._sessions == {}
    assert mgr._mgr_locks == {}


async def test_different_ids_still_proceed_in_parallel(monkeypatch):
    """Per-id locking (not a global lock): two different ids overlap inside
    start(). A serialized implementation would time out here."""
    mgr = session_mod.SessionManager()
    both_inside = asyncio.Event()
    counter = {"n": 0}

    async def _start(self):
        counter["n"] += 1
        if counter["n"] >= 2:
            both_inside.set()
        # Only completes if BOTH ids are inside start() at once — a global lock
        # (or an over-broad serialization) would deadlock into this timeout.
        await asyncio.wait_for(both_inside.wait(), timeout=2)
        self._proc = _CacheBoundsFakeProc()
        self._started = True

    monkeypatch.setattr(session_mod.PersistentSession, "start", _start)

    await asyncio.gather(
        mgr.get_or_create(session_id="par-a"),
        mgr.get_or_create(session_id="par-b"),
    )
    assert set(mgr._sessions) == {"par-a", "par-b"}
    assert set(mgr._mgr_locks) == {"par-a", "par-b"}
