"""Tests for the single-reader + asyncio.Queue stdout refactor and idle reaper.

The stdout read loop is shared by EVERY session (ChatPage + ClarifyChat), so it
is the highest-risk surface in the project. These tests pin the behaviours the
ClarifyChat tab-switch fix depends on, WITHOUT execing a real `claude` binary:

  (a) ABANDONED TURN COMPLETES + FLUSHES — a consumer that disconnects mid-turn
      (break after an assistant event, then gen.aclose()) does NOT stall the
      subprocess: the single background reader keeps draining stdout to the
      turn's `result` and EOF, so the CLI can flush the complete transcript to
      disk. We assert the reader drained ALL remaining events (the abandoned
      tail is not stuck in the OS pipe).

  (b) IDLE REAPER — _reap_idle stops a session idle past the TTL with no active
      consumer, while SPARING one with an active consumer and one recently
      active.

  (c) CLEAN NEXT TURN — a resumed session's next send() returns a FRESH turn
      keyed to the new prompt, never the prior turn's buffered result (the
      STALE-TAIL-REPLAY trap).

The existing send()/auth/respawn tests in tests/test_server.py stay green; these
fakes are copied minimal locals (named to avoid shadowing — though this is a
separate module) and never touch the real ~/.claude tree.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import server.session_manager as session_mod


# --------------------------------------------------------------------------- #
# Minimal subprocess fakes (copied from tests/test_server.py — kept local so
# this file is self-contained; returncode=None makes .alive True).
# --------------------------------------------------------------------------- #
class _FakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        # Mirror asyncio.StreamWriter.close() so a real PersistentSession.stop()
        # (e.g. when the warm pool evicts a spare) works against the fake.
        pass


class _FakeStdout:
    """Yields JSONL event lines (slowly) then raises StopAsyncIteration (EOF)."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        await asyncio.sleep(0)  # let the consumer/reader interleave
        return self._lines.pop(0)


class _FakeProc:
    returncode = None  # mimics a live process (so .alive is True)

    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _turn_lines(text: str = "hi there"):
    """A full turn: system event, two assistant chunks, then a result."""
    return [
        _line({"type": "system", "session_id": "sess-1"}),
        _line({"type": "assistant",
               "message": {"content": [{"type": "text", "text": "hi"}]}}),
        _line({"type": "assistant",
               "message": {"content": [{"type": "text", "text": " there"}]}}),
        _line({"type": "result", "is_error": False, "result": text}),
    ]


# --------------------------------------------------------------------------- #
# (a) Abandoned turn still completes + flushes
# --------------------------------------------------------------------------- #
async def test_abandoned_turn_is_drained_to_completion_by_background_reader():
    """Client disconnects mid-turn; the single reader still drains to result+EOF.

    This is the load-bearing fix: in the old code send() was the ONLY stdout
    reader, so abandoning it left the subprocess blocked on a full stdout pipe
    and the reply tail never flushed to disk (the TRUNCATED-COMPLETE trap). Now a
    background reader owns stdout for the session's lifetime; once the consumer
    aclose()s, the reader keeps going and pushes every remaining event so the CLI
    can finish the turn and flush the full transcript.
    """
    s = session_mod.PersistentSession(session_id="sess-1")
    s._proc = _FakeProc(_turn_lines())
    s._started = True

    gen = s.send("hello")
    got = []
    async for evt in gen:
        got.append(evt)
        if evt.get("type") == "assistant":
            break  # client disconnects mid-stream

    # Generator is suspended at a yield, still holding the lock.
    assert s._lock.locked()
    await gen.aclose()
    assert not s._lock.locked()
    # aclose() ran the finally → no active consumer remains.
    assert s._active_consumers == 0

    # The background reader keeps draining the abandoned turn to its result + EOF
    # even with no consumer attached. Wait for it to finish (bounded).
    await asyncio.wait_for(s._reader_task, timeout=2.0)

    # Every remaining event (including the trailing `result`) was drained off the
    # stdout pipe and queued — proving the subprocess was never left blocked and
    # the turn ran to completion (so the CLI could flush the full transcript).
    drained = []
    while not s._events.empty():
        drained.append(s._events.get_nowait())
    assert any(e is session_mod._STDOUT_EOF for e in drained)
    results = [e for e in drained if isinstance(e, dict) and e.get("type") == "result"]
    assert results, "the abandoned turn's result must have been drained, not stuck in the pipe"
    assert results[-1]["result"] == "hi there"


# --------------------------------------------------------------------------- #
# (b) Idle reaper
# --------------------------------------------------------------------------- #
async def test_reaper_stops_idle_session_but_spares_active_and_recent():
    """_reap_idle stops only an idle, consumer-free session past the TTL."""
    mgr = session_mod.SessionManager()
    now = session_mod.time.monotonic()
    ttl = session_mod._SESSION_IDLE_TTL_SECONDS

    stopped = []

    def make_session(sid, *, last_activity, consumers):
        s = session_mod.PersistentSession(session_id=sid)
        s._proc = _FakeProc([])  # .alive True
        s._started = True
        s._last_activity = last_activity
        s._active_consumers = consumers

        async def fake_stop():
            stopped.append(sid)
            s._proc.returncode = 0

        s.stop = fake_stop
        return s

    # Idle past TTL, no consumer → REAP.
    mgr._sessions["idle"] = make_session(
        "idle", last_activity=now - ttl - 60, consumers=0)
    # Idle past TTL but has an active SSE consumer → SPARE (never tear out a live
    # stream).
    mgr._sessions["busy"] = make_session(
        "busy", last_activity=now - ttl - 60, consumers=1)
    # Recently active, no consumer → SPARE.
    mgr._sessions["recent"] = make_session(
        "recent", last_activity=now, consumers=0)

    await mgr._reap_idle()

    assert stopped == ["idle"]
    assert "idle" not in mgr._sessions
    assert "busy" in mgr._sessions
    assert "recent" in mgr._sessions


async def test_reaper_loop_lifecycle_is_inert_on_startup_and_cancels_cleanly(monkeypatch):
    """start_reaper sleeps FIRST (no sweep on startup) and stop_reaper cancels
    and awaits cleanly — mirrors the session-watcher lifecycle.
    """
    mgr = session_mod.SessionManager()
    swept = {"n": 0}

    async def counting_reap():
        swept["n"] += 1

    monkeypatch.setattr(mgr, "_reap_idle", counting_reap)
    # Tiny interval so we could observe a sweep — but the sleep-FIRST design means
    # none fires before we cancel within one short window.
    monkeypatch.setattr(session_mod, "_REAPER_INTERVAL_SECONDS", 10)

    mgr.start_reaper()
    assert mgr._reaper_task is not None
    await asyncio.sleep(0)  # let the task schedule and hit the first sleep
    assert swept["n"] == 0  # inert on startup — no immediate sweep

    await mgr.stop_reaper()
    assert mgr._reaper_task is None  # torn down, no lingering task


async def test_stop_all_also_stops_reaper(monkeypatch):
    """stop_all() tears the reaper down too (shutdown path stays warning-free)."""
    mgr = session_mod.SessionManager()
    mgr.start_reaper()
    assert mgr._reaper_task is not None
    await mgr.stop_all()
    assert mgr._reaper_task is None


async def test_reaper_is_wired_into_production_startup(aiohttp_client, tmp_path, monkeypatch):
    """AC-8 live-proof: bringing up the real app actually STARTS the reaper.

    The reaper logic was correct but never wired into a startup hook, so in
    production manager._reaper_task stayed None and /api/workers never listed it
    (the periodic sweep AC-8 requires never ran). This asserts the wiring: under
    aiohttp_client(app) the session manager's reaper task is live AND the worker
    registry surfaces it as a running 'Session Reaper'.
    """
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    from server.app import create_app

    app = create_app()
    app["allowed_cwd_roots"] = [tmp_path]
    app["default_cwd"] = tmp_path
    app["permission_mode"] = "bypassPermissions"

    client = await aiohttp_client(app)  # fires on_startup hooks

    # The manager owns a live reaper task (NOT None, NOT done).
    mgr = app["session_manager"]
    assert mgr._reaper_task is not None
    assert not mgr._reaper_task.done()
    # And it is mirrored onto app state for the worker registry.
    assert app.get("session_reaper_task") is mgr._reaper_task

    resp = await client.get("/api/workers")
    assert resp.status == 200
    workers = await resp.json()
    reaper = [w for w in workers if w["name"] == "Session Reaper"]
    assert reaper, "/api/workers must list the Session Reaper (AC-8 wiring)"
    assert reaper[0]["status"] == "running"
    assert reaper[0]["intervalS"] == session_mod._REAPER_INTERVAL_SECONDS


# --------------------------------------------------------------------------- #
# (c) Clean next turn after resume
# --------------------------------------------------------------------------- #
async def test_next_send_returns_fresh_turn_not_stale_buffered_result():
    """After a prior turn was drained, the next send() yields the NEW turn only.

    Seeds the queue with a fully-drained prior turn (events + result + EOF), as if
    the reader had finished an abandoned turn while the client was away. The next
    send() must discard that stale tail before writing its prompt, so the caller
    only ever sees events produced for the NEW prompt — never the prior turn's
    buffered result (the STALE-TAIL-REPLAY trap).
    """
    s = session_mod.PersistentSession(session_id="sess-1")
    # The new turn the subprocess will emit once the new prompt is written.
    s._proc = _FakeProc([
        _line({"type": "assistant",
               "message": {"content": [{"type": "text", "text": "fresh answer"}]}}),
        _line({"type": "result", "is_error": False, "result": "FRESH"}),
    ])
    s._started = True

    # Stale prior-turn events still sitting in the queue (already drained by the
    # background reader while the client was gone). A persistent session does
    # NOT emit EOF between turns — the process stays alive and the next turn's
    # events arrive on the same stdout — so there is no _STDOUT_EOF here. The
    # prior (abandoned) turn wrote a prompt whose result was never consumed, so
    # model that: RESULT-COUNTING uses (_prompts_written - _results_consumed),
    # not the live queue, as the source of truth, so the seeded state must stay
    # consistent with it.
    s._events.put_nowait({"type": "assistant",
                          "message": {"content": [{"type": "text", "text": "stale"}]}})
    s._events.put_nowait({"type": "result", "is_error": False, "result": "STALE"})
    s._prompts_written = 1   # the prior abandoned turn wrote a prompt...
    s._results_emitted = 1   # ...and the reader enqueued its (still-unconsumed) result

    got = [evt async for evt in s.send("a brand new question")]

    result_evt = next(e for e in got if e.get("type") == "result")
    assert result_evt["result"] == "FRESH"
    # The stale buffered result must NOT have leaked into the new turn.
    assert all(e.get("result") != "STALE" for e in got if e.get("type") == "result")
    assert not any(
        e.get("type") == "assistant"
        and e["message"]["content"][0]["text"] == "stale"
        for e in got
    )


# --------------------------------------------------------------------------- #
# Contract regression guards (the highest-risk send() refactor)
# --------------------------------------------------------------------------- #
async def test_send_captures_session_id_and_breaks_after_result():
    """session_id is captured from the system event and send() breaks after the
    result event (observable contract relied on by chat.py).
    """
    s = session_mod.PersistentSession(session_id=None)
    s._proc = _FakeProc(_turn_lines())
    s._started = True

    got = [evt async for evt in s.send("hello")]
    assert s.session_id == "sess-1"
    assert got[-1].get("type") == "result"
    assert not s._lock.locked()


async def test_send_tags_auth_error_via_reader():
    """Auth-error tagging now lives in the reader but is still observed by send()."""
    s = session_mod.PersistentSession(session_id="sess-auth")
    s._proc = _FakeProc([
        _line({"type": "system", "session_id": "sess-auth"}),
        _line({"type": "result", "is_error": True,
               "result": "API Error: 403 The security token included in the request is invalid"}),
    ])
    s._started = True

    got = [evt async for evt in s.send("hi")]
    result_evt = next(e for e in got if e.get("type") == "result")
    assert result_evt.get("errorKind") == "auth"


# --------------------------------------------------------------------------- #
# AC-4: IN-FLIGHT abandon-then-resend (the headline regression).
#
# The existing test_next_send_returns_fresh_turn_not_stale_buffered_result above
# pre-seeds the stale result INTO s._events — the already-ENQUEUED ordering. That
# ordering is handled correctly even by the old queue-DRAIN (send() empties the
# queue before writing its prompt), so it gives FALSE CONFIDENCE for the bug we
# actually shipped.
#
# The real regression is the IN-FLIGHT case: a prior turn was abandoned mid-stream
# and its turn-final `result` is STILL IN THE OS PIPE (unconsumed, NOT yet enqueued
# into s._events) at the moment the next send() begins — and only crosses into the
# queue AFTER the new send() has already written its prompt and started consuming.
# A drain empties nothing (the result is still in the pipe), so the new send()
# then consumes the prior turn's late result as its own first `result` and breaks
# early — orphaning the new turn. A queue sentinel lands AHEAD of the still-in-pipe
# result; an enqueue-time turn-id stamps the late result with the new turn's id.
# Only RESULT-COUNTING (the reader counts every turn-final result it enqueues;
# send() skips exactly the outstanding prior-turn count before accepting its own)
# separates the in-flight prior result from the new turn.
#
# To pin the ordering DETERMINISTICALLY (rather than racing asyncio.sleep(0)), the
# fakes below GATE turn 1's result so it is released into the pipe only once the
# SECOND send() has written its prompt — guaranteeing the stale result is genuinely
# in-pipe (in _GatedStdout, not s._events) when the second send begins, then
# arrives mid-consume. This test FAILS on the queue-drain tree (it leaks STALE-T1)
# and PASSES only with result-counting.
# --------------------------------------------------------------------------- #
class _GatedStdin:
    """A stdin whose write() releases the stdout gate after N prompts.

    Couples the two fakes so turn 1's result stays in the pipe until the SECOND
    send() writes its prompt — the precise in-flight ordering AC-4 requires,
    without depending on event-loop scheduling luck.
    """

    def __init__(self, gate: asyncio.Event, *, release_after: int):
        self._gate = gate
        self._release_after = release_after
        self.writes = 0

    def write(self, _data):
        self.writes += 1
        if self.writes >= self._release_after:
            self._gate.set()

    async def drain(self):
        pass


class _GatedStdout:
    """Yields lines, but blocks on a gate before yielding the gated index.

    Lines before `gate_index` flow immediately (one per `await sleep(0)`); the
    line AT `gate_index` (turn 1's result) is held until `gate` is set, so it
    stays in the pipe — never enqueued into s._events — until the test's coupled
    stdin releases it. Everything after flows normally (turn 2's events).
    """

    def __init__(self, lines, gate: asyncio.Event, gate_index: int):
        self._lines = list(lines)
        self._gate = gate
        self._gate_index = gate_index
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._lines):
            raise StopAsyncIteration
        if self._i == self._gate_index:
            # Hold turn 1's result in the pipe until the second prompt is written.
            await self._gate.wait()
        await asyncio.sleep(0)  # let the consumer/reader interleave
        line = self._lines[self._i]
        self._i += 1
        return line


class _GatedProc:
    returncode = None

    def __init__(self, lines, *, gate_index, release_after):
        gate = asyncio.Event()
        self.stdin = _GatedStdin(gate, release_after=release_after)
        self.stdout = _GatedStdout(lines, gate, gate_index)


async def test_inflight_abandoned_result_does_not_leak_into_next_turn():
    """AC-4: an in-flight prior result (still in the pipe) must not leak into the
    next turn. Drives the REAL send() path; fails on queue-drain, passes only with
    result-counting.
    """
    # Both turns' lines are present from the start. Turn 1: system, assistant
    # (STALE-T1), result (STALE-T1) — the result is GATED at index 2 so it stays
    # in the pipe. Turn 2: assistant (FRESH-T2), result (FRESH-T2) — flow freely
    # once the gate opens (after the SECOND prompt is written → release_after=2).
    lines = [
        # turn 1 (abandoned)
        _line({"type": "system", "session_id": "sess-1"}),                       # 0
        _line({"type": "assistant",
               "message": {"content": [{"type": "text", "text": "STALE-T1"}]}}),  # 1
        _line({"type": "result", "is_error": False, "result": "STALE-T1"}),       # 2 (GATED)
        # turn 2 (the real new turn)
        _line({"type": "assistant",
               "message": {"content": [{"type": "text", "text": "FRESH-T2"}]}}),  # 3
        _line({"type": "result", "is_error": False, "result": "FRESH-T2"}),       # 4
    ]
    s = session_mod.PersistentSession(session_id="sess-1")
    s._proc = _GatedProc(lines, gate_index=2, release_after=2)
    s._started = True

    # --- Turn 1: begin, consume one assistant event, then ABANDON it. The
    # turn-final STALE-T1 result is still GATED in the pipe (not yet enqueued).
    gen1 = s.send("first question")
    async for evt in gen1:
        if evt.get("type") == "assistant":
            assert evt["message"]["content"][0]["text"] == "STALE-T1"
            break  # client disconnects mid-turn
    await gen1.aclose()
    # The abandoned send released the lock and left the result unconsumed/in-pipe.
    assert not s._lock.locked()
    # Nothing released the gate yet — turn 1's result has NOT been enqueued.
    assert not s._proc.stdout._gate.is_set()

    # --- Turn 2: send a brand-new question. Its prompt write is the SECOND write
    # (release_after=2), which opens the gate so turn 1's late result crosses into
    # the queue AFTER this send() has begun consuming — the exact in-flight
    # ordering. Result-counting must step over the stale result; a drain cannot.
    got = [evt async for evt in s.send("a brand new question")]

    result_evts = [e for e in got if e.get("type") == "result"]
    assert result_evts, "the new turn must produce its own result, not an empty break"
    assert result_evts[-1]["result"] == "FRESH-T2", (
        "the new turn consumed the prior abandoned turn's in-flight result "
        "(STALE-TAIL-REPLAY) — drain/sentinel cannot fix this, only result-counting"
    )
    # No stale prior-turn artifact leaked into the new turn.
    assert all(e.get("result") != "STALE-T1" for e in result_evts)
    assert not any(
        e.get("type") == "assistant"
        and e["message"]["content"][0]["text"] == "STALE-T1"
        for e in got
    )
    # The new turn is COMPLETE: its own assistant text is present.
    assert any(
        e.get("type") == "assistant"
        and e["message"]["content"][0]["text"] == "FRESH-T2"
        for e in got
    )
    assert not s._lock.locked()


# --------------------------------------------------------------------------- #
# A2: per-turn timeout that releases the lock (the held-lock-forever guard).
# --------------------------------------------------------------------------- #
class _SilentStdout:
    """Never emits any line and never reaches EOF — models a wedged subprocess.

    __anext__ blocks indefinitely, so send()'s await on the next event would hang
    forever WITHOUT the wait_for timeout. (Bounded by the test's own wait_for.)
    """

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # never set → blocks forever
        raise AssertionError("unreachable")  # pragma: no cover


class _SilentProc:
    returncode = None

    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = _SilentStdout()


async def test_turn_timeout_yields_error_result_and_releases_lock(monkeypatch):
    """A turn with no result within _TURN_TIMEOUT_SECONDS yields a synthetic error
    result AND frees the lock, so a SECOND send() on the same session proceeds
    (asyncio.Lock has no acquire timeout — the timeout MUST break the loop).
    """
    monkeypatch.setattr(session_mod, "_TURN_TIMEOUT_SECONDS", 0.05)

    s = session_mod.PersistentSession(session_id="sess-wedged")
    s._proc = _SilentProc()
    s._started = True

    # First turn wedges: no event ever arrives → the timeout fires.
    got = await asyncio.wait_for(_collect(s.send("hello")), timeout=2.0)

    result_evts = [e for e in got if e.get("type") == "result"]
    assert result_evts, "a wedged turn must surface a synthetic result, not hang/blank"
    assert result_evts[-1].get("is_error") is True
    # The lock was released by the timeout breaking the loop (NOT held forever).
    assert not s._lock.locked()

    # The held-lock-forever guard: a SECOND send() on the same session must be able
    # to acquire the lock and proceed (it will time out the same way, but the point
    # is it is NOT blocked forever on the first send()'s still-held lock).
    got2 = await asyncio.wait_for(_collect(s.send("second question")), timeout=2.0)
    assert any(e.get("type") == "result" and e.get("is_error") for e in got2)
    assert not s._lock.locked()


# --------------------------------------------------------------------------- #
# A3: a dead reader (exception path) surfaces as an explicit error result,
# NOT a clean empty break (a crashed claude must not read as a blank turn).
# --------------------------------------------------------------------------- #
class _RaisingStdout:
    """Yields any preamble lines, then raises a non-Cancelled error mid-iteration.

    Models the subprocess stdout pipe breaking / a read failure inside
    _drain_stdout — the path that previously degraded to a lone EOF (clean blank
    break). With the error sentinel it must surface as a visible error result.
    """

    def __init__(self, preamble):
        self._lines = list(preamble)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self._lines:
            return self._lines.pop(0)
        raise RuntimeError("stdout pipe broke")


class _RaisingProc:
    returncode = None

    def __init__(self, preamble):
        self.stdin = _FakeStdin()
        self.stdout = _RaisingStdout(preamble)


async def test_dead_reader_surfaces_error_result_not_blank_break():
    """_drain_stdout's exception path must surface as an error result event
    (is_error True), not a clean empty break that looks like Claude said nothing.
    """
    s = session_mod.PersistentSession(session_id="sess-crash")
    # A system event flows, then the reader raises before any result.
    s._proc = _RaisingProc([
        _line({"type": "system", "session_id": "sess-crash"}),
    ])
    s._started = True

    got = await asyncio.wait_for(_collect(s.send("hi")), timeout=2.0)

    result_evts = [e for e in got if e.get("type") == "result"]
    assert result_evts, (
        "a crashed reader must surface an explicit error result, not a silent "
        "blank break (no result)"
    )
    assert result_evts[-1].get("is_error") is True
    assert not s._lock.locked()


async def test_clean_eof_without_error_still_breaks_without_a_result():
    """A plain EOF with NO preceding error stays a clean break (no synthetic
    error result) — only the exception path manufactures an error. This pins that
    the A3 error sentinel does NOT fire on normal end-of-stream.
    """
    s = session_mod.PersistentSession(session_id="sess-eof")
    # Reader yields a system event then reaches EOF with no result and no error.
    s._proc = _FakeProc([
        _line({"type": "system", "session_id": "sess-eof"}),
    ])
    s._started = True

    got = await asyncio.wait_for(_collect(s.send("hi")), timeout=2.0)

    # Clean EOF → no error result manufactured (the abandoned-turn EOF path).
    assert not any(e.get("type") == "result" and e.get("is_error") for e in got)
    assert not s._lock.locked()


async def _collect(gen) -> list:
    """Drain an async event generator to a list (helper for the timeout tests)."""
    return [evt async for evt in gen]


# --------------------------------------------------------------------------- #
# B2/B3: bounded cwd-keyed warm pool — prewarm, claim, health-check, reap.
#
# A pre-warmed claude process pays the ~4s creds+Node boot OFF the user's
# critical path, so a NEW (or reaped-then-resumed) chat for the same
# (cwd, permission_mode) can CLAIM it instead of spawning cold. The pool MUST
# be bounded both ways (size cap at insert + age reap) so unclaimed spares can
# never leak orphaned subprocesses. These tests patch start() so no real
# `claude` binary is spawned.
# --------------------------------------------------------------------------- #
def _patch_fake_start(monkeypatch):
    """Make PersistentSession.start() wire a live fake proc + reader (no exec)."""
    async def fake_start(self):
        self._proc = _FakeProc([])
        self._started = True
        self._ensure_reader()
    monkeypatch.setattr(session_mod.PersistentSession, "start", fake_start)


async def test_prewarm_then_claim_returns_the_warm_session(monkeypatch):
    """get_or_create (no resume id) for a (cwd, mode) CLAIMS the warm spare for
    that key instead of spawning a fresh one, and removes it from the pool."""
    _patch_fake_start(monkeypatch)
    mgr = session_mod.SessionManager()

    await mgr.prewarm(cwd="/tmp/proj", permission_mode="bypassPermissions")
    key = mgr._warm_key("/tmp/proj", "bypassPermissions")
    warm = mgr._warm_pool[key]

    claimed = await mgr.get_or_create(
        cwd="/tmp/proj", permission_mode="bypassPermissions")

    assert claimed is warm                 # the SAME booted process was handed over
    assert key not in mgr._warm_pool       # claimed → no longer a spare (no double-claim)


async def test_prewarm_is_idempotent_per_key(monkeypatch):
    """A second prewarm for an already-healthy key does NOT spawn a duplicate
    (repeated panel-opens can't pile up processes)."""
    _patch_fake_start(monkeypatch)
    mgr = session_mod.SessionManager()

    await mgr.prewarm(cwd="/tmp/proj", permission_mode="default")
    key = mgr._warm_key("/tmp/proj", "default")
    first = mgr._warm_pool[key]

    await mgr.prewarm(cwd="/tmp/proj", permission_mode="default")
    assert mgr._warm_pool[key] is first    # same spare reused, not replaced
    assert len(mgr._warm_pool) == 1


async def test_claim_skips_and_discards_an_unhealthy_warm_session(monkeypatch):
    """A warm entry whose subprocess died is stopped+discarded on claim, and a
    fresh process is spawned instead (never hand a dead process to a real turn)."""
    _patch_fake_start(monkeypatch)
    mgr = session_mod.SessionManager()

    await mgr.prewarm(cwd="/tmp/proj", permission_mode="default")
    key = mgr._warm_key("/tmp/proj", "default")
    dead = mgr._warm_pool[key]
    # Simulate the warm process having died while it sat in the pool.
    dead._proc.returncode = 0
    stopped = {"v": False}
    async def fake_stop():
        stopped["v"] = True
    dead.stop = fake_stop

    claimed = await mgr.get_or_create(cwd="/tmp/proj", permission_mode="default")

    assert claimed is not dead             # the dead spare was NOT handed over
    assert stopped["v"] is True            # it was stopped/discarded
    assert claimed.alive                   # a fresh, live process was spawned instead


async def test_warm_pool_size_capped_evicts_oldest(monkeypatch):
    """The pool never exceeds _WARM_POOL_MAX: a fresh prewarm beyond the cap
    evicts the OLDEST spare (size-cap-at-insert half of the no-leak guarantee)."""
    _patch_fake_start(monkeypatch)
    monkeypatch.setattr(session_mod, "_WARM_POOL_MAX", 2)
    mgr = session_mod.SessionManager()
    now = session_mod.time.monotonic()

    # Warm "a" then "b" and stamp explicit ages so "a" is unambiguously oldest
    # at the moment the over-cap prewarm of "c" runs its min()-by-_last_activity
    # eviction. Stub each spare's stop() so the evicted one records itself
    # without driving the real subprocess-teardown path against the fake.
    evicted = []
    for label, age in (("a", 30.0), ("b", 20.0)):
        await mgr.prewarm(cwd=f"/tmp/{label}", permission_mode="default")
        sess = mgr._warm_pool[mgr._warm_key(f"/tmp/{label}", "default")]
        sess._last_activity = now - age
        def _stop(name=label):
            async def __stop():
                evicted.append(name)
            return __stop
        sess.stop = _stop()

    # The third prewarm pushes the pool over the cap → evict the oldest ("a").
    await mgr.prewarm(cwd="/tmp/c", permission_mode="default")

    assert evicted == ["a"]                                          # oldest stopped
    assert len(mgr._warm_pool) == 2
    assert mgr._warm_key("/tmp/a", "default") not in mgr._warm_pool   # oldest evicted
    assert mgr._warm_key("/tmp/b", "default") in mgr._warm_pool       # kept
    assert mgr._warm_key("/tmp/c", "default") in mgr._warm_pool       # newest kept


async def test_reaper_evicts_idle_warm_spares(monkeypatch):
    """_reap_idle age-reaps unclaimed warm spares past _WARM_IDLE_TTL_SECONDS
    (the age-reap half of the no-leak guarantee), sparing a recent one."""
    _patch_fake_start(monkeypatch)
    mgr = session_mod.SessionManager()
    now = session_mod.time.monotonic()
    ttl = session_mod._WARM_IDLE_TTL_SECONDS

    stopped = []

    async def add_warm(label, *, last_activity):
        await mgr.prewarm(cwd=f"/tmp/{label}", permission_mode="default")
        key = mgr._warm_key(f"/tmp/{label}", "default")
        sess = mgr._warm_pool[key]
        sess._last_activity = last_activity
        async def _stop():
            stopped.append(label)
        sess.stop = _stop
        return key

    stale_key = await add_warm("stale", last_activity=now - ttl - 60)
    fresh_key = await add_warm("fresh", last_activity=now)

    await mgr._reap_idle()

    assert stopped == ["stale"]
    assert stale_key not in mgr._warm_pool   # idle spare reclaimed
    assert fresh_key in mgr._warm_pool       # recent spare spared


async def test_stop_all_stops_and_clears_warm_pool(monkeypatch):
    """stop_all() must also stop+clear the warm pool, so shutdown leaves no
    orphaned warm subprocess."""
    _patch_fake_start(monkeypatch)
    mgr = session_mod.SessionManager()

    await mgr.prewarm(cwd="/tmp/proj", permission_mode="default")
    key = mgr._warm_key("/tmp/proj", "default")
    stopped = {"v": False}
    async def fake_stop():
        stopped["v"] = True
    mgr._warm_pool[key].stop = fake_stop

    await mgr.stop_all()

    assert stopped["v"] is True
    assert mgr._warm_pool == {}
