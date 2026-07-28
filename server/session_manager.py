"""Persistent Claude session manager.

Spawns a long-lived `claude --print --input-format stream-json --output-format stream-json`
process per session. Sends user messages via stdin, reads streaming responses from stdout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# Sentinel pushed onto a session's event queue when the single stdout reader
# reaches EOF (the subprocess closed its stdout). A waiting send() consumer
# treats it as end-of-stream and breaks without yielding it.
_STDOUT_EOF = object()

# Sentinel pushed onto the queue when the single stdout reader's read loop
# raises (the subprocess crashed / its stdout died mid-turn). Unlike a clean
# _STDOUT_EOF — which send() turns into a silent empty break (normal end of
# stream) — a preceding _STDOUT_ERROR makes send() surface a synthetic error
# `result` event, so a dead/crashed subprocess shows up as a visible error
# turn instead of a blank one (a turn where "Claude said nothing"). The reader
# pushes a (_STDOUT_ERROR, reason) tuple so the message can name the failure.
_STDOUT_ERROR = object()

# Per-turn hard ceiling on how long send() will wait for the NEXT event from
# the subprocess. Generous — well above worst-case model + MCP-tool latency —
# so it never cuts a legitimately slow turn; it only fires when the subprocess
# wedges (or an MCP tool call hangs) and would otherwise block send() forever
# while it holds self._lock, wedging the NEXT send() on the session too
# (asyncio.Lock has no acquire timeout). On timeout send() yields one synthetic
# error result and breaks, releasing the lock.
_TURN_TIMEOUT_SECONDS = 120

# Warm-subprocess pool bounds. A pre-warmed claude process pays the ~4s
# credential-export + Node/plugin boot cost OFF the user's critical path, so a
# new (or reaped-then-resumed) chat can claim an already-booted process instead
# of waiting for a cold spawn in front of the user. The pool is bounded TWO
# ways so it can never leak orphaned subprocesses: a hard size cap evicts the
# oldest entry at insert time, and the idle reaper evicts entries older than
# the warm TTL (shorter than the 30-min session TTL — a spare nobody claimed
# should be reclaimed quickly).
_WARM_POOL_MAX = 4
_WARM_IDLE_TTL_SECONDS = 5 * 60


# Explicit, unambiguous credential phrases → definitely an auth failure. The
# claude subprocess routes through Bedrock, so expired AWS/Midway creds surface
# as these. Matched case-insensitively against result-event text and stderr.
_AUTH_EXPLICIT = (
    "security token included in the request is invalid",
    "security token included in the request is expired",
    "unrecognizedclientexception",
    "expiredtoken",
    "expired token",
    "invalidsignatureexception",
    "unable to locate credentials",
    "could not load credentials",
)

# Auth-ish HTTP status markers; only treated as auth when paired with a
# credential keyword (below), so generic 403s from tools aren't misclassified.
_AUTH_STATUS = ("403", "401", "accessdenied", "not authorized", "unauthorized")

# How long a session may sit with NO active send()/SSE consumer before the
# idle reaper stops it. The single-reader design keeps an abandoned clarify
# session's subprocess alive until its turn finishes and then idles, so this
# bounds that intentional leak. Actively-used ChatPage sessions update
# _last_activity on every streamed event, so they are never reaped while live.
_SESSION_IDLE_TTL_SECONDS = 30 * 60

# How often the background reaper sweeps for idle sessions.
_REAPER_INTERVAL_SECONDS = 5 * 60


def is_auth_error(text: str | None) -> bool:
    """Classify whether an error string is a credential/auth failure.

    Conservative by design: matches either an explicit token/credential phrase,
    or a 403/401-style status combined with a credential keyword. Ordinary
    model/tool errors carry none of these, so they are NOT misclassified.
    """
    if not text:
        return False
    t = text.lower()
    if any(p in t for p in _AUTH_EXPLICIT):
        return True
    has_status = any(s in t for s in _AUTH_STATUS)
    has_cred = "credential" in t or "token" in t or "authenticat" in t
    return has_status and has_cred


class PersistentSession:
    """A long-lived Claude process that accepts multiple turns."""

    def __init__(self, session_id: str | None = None, cwd: str | None = None,
                 permission_mode: str = "default", name: str | None = None):
        self.session_id = session_id
        self.cwd = cwd or str(Path.home())
        self.permission_mode = permission_mode
        self.name = name
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_reader: asyncio.StreamReader | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._stderr_task: asyncio.Task | None = None
        # Bounded tail of recent stderr lines, used to classify auth failures
        # (some credential errors surface on stderr rather than as a result event).
        self._stderr_tail: list[str] = []

        # Single-reader + queue design. ONE background task (_drain_stdout) owns
        # the stdout read loop for the session's whole lifetime and pushes parsed
        # events onto this bounded queue (maxsize=2000, drop-oldest policy);
        # send() consumes from the queue rather than reading stdout directly.
        # Because the reader keeps draining even when no send() is awaiting (the
        # SSE client went away), the subprocess never blocks on a full ~64KB
        # stdout pipe — so an abandoned mid-stream turn still runs to its `result`
        # and the claude CLI flushes the complete transcript to disk. The single
        # reader also prevents send() and the drain from both consuming stdout and
        # stealing each other's events. When the queue is full, the oldest event
        # is discarded to make room (drop-oldest) — this bounds memory usage for
        # long-running sessions while ensuring critical sentinels always enqueue.
        self._events: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._reader_task: asyncio.Task | None = None
        # RESULT-COUNTING turn-boundary mechanism (replaces the old "drain the
        # queue before writing the prompt" approach, which was empirically
        # insufficient — it only drains events ALREADY enqueued, so a prior
        # abandoned turn whose `result` was still IN THE OS PIPE at send() time
        # would leak into the next turn and truncate it).
        #
        # The source of truth is the count of PROMPTS WRITTEN minus turn-final
        # RESULTS CONSUMED — NOT the count of results the reader has emitted.
        # That distinction is the whole point: a prior abandoned turn may have
        # written its prompt but its result is still IN THE PIPE (not yet
        # emitted/enqueued) at the instant the next send() begins, so a
        # snapshot of "results emitted so far" would be zero and miss it. Every
        # send() writes exactly ONE prompt and the subprocess will emit exactly
        # ONE turn-final `result` for it, so (_prompts_written - _results_consumed)
        # at the top of a send() is exactly the number of prior-turn results
        # still owed. send() skips that many results (and every non-result event
        # preceding each), waiting on get() for the late in-pipe result to
        # arrive, before yielding its OWN turn. A queue sentinel or an
        # enqueue-time turn-id can NOT do this (the sentinel lands ahead of the
        # still-in-pipe result; an enqueue-time id stamps the late result with
        # the new turn's id). The reader ALSO tallies emitted results in
        # _results_emitted purely for observability/asserting in tests.
        self._prompts_written: int = 0
        self._results_emitted: int = 0
        self._results_consumed: int = 0
        # Wall-clock-independent timestamp of the last send() activity, used by
        # the SessionManager idle reaper. Initialized at construction so a session
        # that never sends still has a sane idle baseline.
        self._last_activity: float = time.monotonic()
        # Number of in-flight send() consumers. The reaper must NEVER stop a
        # session with an active consumer (an open SSE stream), so it checks this.
        self._active_consumers: int = 0

    async def start(self) -> str | None:
        """Start the Claude process. Returns session_id from init event."""
        claude_bin = shutil.which("claude") or "claude"
        args = [
            claude_bin, "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--permission-mode", self.permission_mode,
        ]
        if self.session_id:
            args += ["--resume", self.session_id]
        if self.name:
            args += ["--name", self.name]

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        self._started = True

        # Drain stderr in background. Keep the handle so stop() can cancel it
        # rather than orphaning one task per session/respawn.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Launch the single stdout reader alongside stderr. It owns stdout for the
        # session's whole lifetime so the pipe is always drained, even with no
        # send() consuming (see _drain_stdout / __init__).
        self._reader_task = asyncio.create_task(self._drain_stdout())

        return None

    async def _drain_stderr(self):
        """Drain stderr, keeping a bounded tail for auth-error classification."""
        if self._proc and self._proc.stderr:
            async for raw in self._proc.stderr:
                line = raw.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                self._stderr_tail.append(line)
                # Keep only the last 50 lines — enough context for an error,
                # bounded so a chatty process can't grow this without limit.
                if len(self._stderr_tail) > 50:
                    self._stderr_tail = self._stderr_tail[-50:]

    def _recent_stderr(self) -> str:
        return "\n".join(self._stderr_tail)

    def _enqueue_event(self, event) -> None:
        """Enqueue an event with drop-oldest policy when the queue is full.

        When the bounded queue (maxsize=2000) is at capacity, discard the oldest
        event to make room for the new one. This ensures critical sentinels
        (_STDOUT_EOF, _STDOUT_ERROR) are never lost — they evict stale data
        events rather than being dropped themselves.
        """
        if self._events.full():
            try:
                self._events.get_nowait()  # discard oldest
            except asyncio.QueueEmpty:
                pass
            logger.warning(
                "Event queue full for session %s, dropping oldest event",
                self.session_id,
            )
        self._events.put_nowait(event)

    async def _drain_stdout(self):
        """Own the stdout read loop for the session's whole lifetime.

        Reads every line the subprocess emits, parses it, and pushes the event
        onto self._events via _enqueue_event (a bounded queue with drop-oldest
        policy, so this never blocks on a slow/absent consumer and memory is
        capped). This is the load-bearing fix: because the reader keeps draining
        even when no send() is awaiting (the SSE client disconnected), the
        subprocess can never stall on a full ~64KB stdout pipe, so an abandoned
        mid-stream turn still runs to its `result` and the claude CLI flushes
        the complete transcript to ~/.claude/projects/<slug>/<id>.jsonl.

        Side effects that USED to live in send() now happen here so they fire
        regardless of whether a consumer is attached:
          - session_id capture from the system/init event, and
          - auth-error classification of an error `result` event.
        On a clean EOF it pushes the _STDOUT_EOF sentinel so a waiting consumer
        breaks; if the read loop RAISES (the subprocess crashed), it first
        pushes an (_STDOUT_ERROR, reason) tuple so send() can surface a visible
        error result instead of a silent blank turn.
        """
        try:
            if self._proc and self._proc.stdout:
                async for raw in self._proc.stdout:
                    line = raw.decode(errors="replace").rstrip("\n")
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Capture session_id from init (needed even if no consumer is
                    # attached, so register() can later key the session).
                    if evt.get("type") == "system" and evt.get("session_id"):
                        self.session_id = evt["session_id"]

                    # Tag auth/credential failures so the client can show
                    # actionable guidance + a bounded retry, instead of a raw
                    # 403. Classify using the result text plus recent stderr
                    # (some credential errors only appear on stderr). Done here
                    # so tagging still happens for an abandoned turn.
                    if evt.get("type") == "result" and evt.get("is_error"):
                        blob = " ".join(filter(None, [
                            str(evt.get("result", "")),
                            self._recent_stderr(),
                        ]))
                        if is_auth_error(blob):
                            evt = {**evt, "errorKind": "auth"}

                    # Tally every turn-final result as it is enqueued, purely
                    # for observability/test assertions. The send() skip count
                    # is driven by _prompts_written (turns started), NOT this —
                    # a prior turn's result may still be in the pipe (unemitted)
                    # at the moment send() snapshots, so this counter can't be
                    # the source of truth for the skip (see __init__).
                    if evt.get("type") == "result":
                        self._results_emitted += 1

                    self._enqueue_event(evt)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a read failure must end cleanly
            logger.warning("stdout reader for session %s failed: %s",
                           self.session_id, e)
            # Surface the failure as an explicit error sentinel BEFORE the EOF
            # in the finally. send() turns this into a synthetic error result
            # so a crashed/errored subprocess is a visible error turn, never a
            # silent blank one (the clean-empty-break trap).
            self._enqueue_event((_STDOUT_ERROR, str(e)))
        finally:
            # Signal end-of-stream so a waiting consumer stops blocking on get().
            self._enqueue_event(_STDOUT_EOF)

    def _ensure_reader(self) -> None:
        """Launch the stdout reader if it is not already running.

        start() launches it eagerly, but the existing send() tests (and the very
        first turn of a session created via get_or_create that hasn't streamed
        yet) drive send() directly after wiring _proc, so send() must launch the
        reader lazily on first use or no events would ever arrive.
        """
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._drain_stdout())

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def send(self, text: str) -> AsyncIterator[dict]:
        """Send a user message and yield response events as they stream back.

        Observable contract (relied on by chat.py and the test suite, unchanged):
          - async iterator of events; the consumer breaks AFTER the `result`
            event is yielded;
          - session_id is captured from the system event and auth errors are
            tagged on the result event (both now done by the single reader, so
            send() does not re-tag);
          - holds self._lock across the whole yield loop, so chat.py's
            gen.aclose() releases the lock deterministically when the client
            disconnects mid-stream.

        Internally it CONSUMES events from self._events (filled by the single
        _drain_stdout reader) instead of reading stdout directly, so the reader
        and send() never both consume stdout and steal each other's events.

        Turn boundaries use RESULT-COUNTING (see __init__): before writing the
        prompt we snapshot how many prior-turn `result` events are still
        outstanding, then skip exactly that many (and the non-result events
        preceding each) before yielding this turn's own events. This is the only
        mechanism that correctly separates an abandoned prior turn's result —
        even one still IN THE OS PIPE, not yet enqueued — from the new turn.
        """
        if not self.alive:
            raise RuntimeError("Session process is not running")

        msg = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}]
            }
        })

        async with self._lock:
            # RESULT-COUNTING snapshot, taken BEFORE writing the prompt. Every
            # prior send() wrote a prompt the subprocess owes exactly one
            # turn-final `result` for; any of those not yet consumed by a send()
            # belongs to a PRIOR (abandoned) turn. We must skip exactly this many
            # results before accepting our own — and because a prior result may
            # still be IN THE PIPE (not yet enqueued) at this instant, basing the
            # count on prompts-written (not results-emitted) is what lets us know
            # to wait on get() for the late in-pipe result instead of mistaking
            # it for this turn's first result.
            outstanding = self._prompts_written - self._results_consumed

            if self._proc and self._proc.stdin:
                self._proc.stdin.write((msg + "\n").encode())
                await self._proc.stdin.drain()
            # Count this prompt only AFTER a successful write, so a write that
            # raises doesn't inflate the outstanding count for the next send().
            self._prompts_written += 1

            # Lazily ensure the single stdout reader is running. start() launches
            # it eagerly, but the send()-driven tests (and a session that hasn't
            # streamed since spawn) reach here first.
            self._ensure_reader()

            self._active_consumers += 1
            self._last_activity = time.monotonic()
            try:
                while True:
                    try:
                        evt = await asyncio.wait_for(
                            self._events.get(), timeout=_TURN_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        # The subprocess wedged (or an MCP tool hung) mid-turn.
                        # Yield ONE synthetic error result and break — breaking
                        # exits `async with self._lock`, freeing the lock so the
                        # NEXT send() on this session can proceed (do not keep
                        # waiting while holding the lock: that is the held-lock-
                        # forever failure mode). Count it so the result counter
                        # stays consistent for the next turn's snapshot.
                        self._results_consumed += 1
                        yield {
                            "type": "result",
                            "is_error": True,
                            "result": (
                                "Claude did not respond within "
                                f"{_TURN_TIMEOUT_SECONDS}s; the turn was aborted."
                            ),
                        }
                        break

                    self._last_activity = time.monotonic()

                    if evt is _STDOUT_EOF:
                        # Clean end of stream with no preceding error: the
                        # normal turn-finished / process-closed break.
                        break

                    if isinstance(evt, tuple) and evt and evt[0] is _STDOUT_ERROR:
                        # The reader's read loop raised (crashed subprocess).
                        # Surface it as a visible error turn, never a blank one.
                        # Count the synthetic result against this turn's prompt
                        # so the counter stays balanced.
                        reason = evt[1] if len(evt) > 1 else "unknown error"
                        self._results_consumed += 1
                        yield {
                            "type": "result",
                            "is_error": True,
                            "result": f"Claude session ended unexpectedly: {reason}",
                        }
                        break

                    # While there are still outstanding prior-turn results to
                    # skip, swallow events (results AND the non-result events
                    # that precede each) without yielding — they belong to an
                    # abandoned earlier turn.
                    if outstanding > 0:
                        if isinstance(evt, dict) and evt.get("type") == "result":
                            self._results_consumed += 1
                            outstanding -= 1
                        continue

                    yield evt
                    # "result" marks the end of THIS turn.
                    if isinstance(evt, dict) and evt.get("type") == "result":
                        self._results_consumed += 1
                        break
            finally:
                # Decrement even on aclose() (an abandoned stream): the finally
                # runs when the generator is closed, so the reaper sees no active
                # consumer once the SSE client is gone.
                self._active_consumers -= 1

    async def stop(self):
        """Terminate the Claude process, escalating SIGTERM→SIGKILL.

        Each wait is bounded so a process that ignores stdin-close or SIGTERM
        can't hang stop() (and thus stop_all() on shutdown, or respawn())
        forever. The background stderr-drain task is cancelled so it isn't
        orphaned across respawns.
        """
        if self._proc and self._proc.returncode is None:
            if self._proc.stdin:
                self._proc.stdin.close()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    # Last resort: SIGKILL and a final bounded wait.
                    self._proc.kill()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._stderr_task = None
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None
        self._started = False


class SessionManager:
    """Manages multiple persistent sessions."""

    def __init__(self):
        self._sessions: dict[str, PersistentSession] = {}
        # Per-session-id locks serialize lifecycle ops (get_or_create / respawn /
        # stop) on the SAME id, while leaving DIFFERENT ids free to proceed in
        # parallel (a single global lock would needlessly serialize every chat
        # turn's session lookup). asyncio.Lock is NOT reentrant, so a per-id lock
        # must never be held while awaiting another method that needs the same
        # id's lock.
        self._mgr_locks: dict[str, asyncio.Lock] = {}
        # In-flight lifecycle ops per session id (incremented on entry to
        # _lock_scope, BEFORE acquiring, so a queued waiter counts). A lock
        # entry is dropped only when this returns to 0 AND the id is gone from
        # _sessions — see _lock_scope.
        self._lock_waiters: dict[str, int] = {}
        # Brand-new sessions arrive with session_id=None (no resume id), so there
        # is no id to key a lock on at creation time. Concurrent no-id creates are
        # serialized by this single creation lock; each produces its own session
        # that register() later keys by the discovered id.
        self._create_lock = asyncio.Lock()
        # Background idle-reaper task handle (None until start_reaper()).
        self._reaper_task: asyncio.Task | None = None
        # WARM POOL (B2/B3). Pre-booted no-resume sessions keyed by
        # (cwd, permission_mode) so a new chat — or a reaped-then-resumed chat
        # warmed early by the resume trigger — can CLAIM an already-booted
        # process instead of paying the ~4s creds+Node boot synchronously in
        # front of the user. Keyed by cwd because a session subprocess is
        # spawned with a specific cwd and `claude --resume` must run in the
        # session's original cwd, so a warm process is only interchangeable
        # with a fresh one of the SAME cwd (and permission mode). Bounded by
        # _WARM_POOL_MAX at insert and by _WARM_IDLE_TTL_SECONDS in the reaper
        # so unclaimed spares can never leak.
        self._warm_pool: dict[tuple[str, str], PersistentSession] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """Get-or-create the per-session-id lifecycle lock."""
        lock = self._mgr_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._mgr_locks[session_id] = lock
        return lock

    @asynccontextmanager
    async def _lock_scope(self, session_id: str) -> AsyncIterator[None]:
        """Hold the per-id lifecycle lock, dropping the entry after teardown.

        The lock map used to grow one asyncio.Lock per session id forever. Here
        cleanup is REFCOUNTED: an in-flight counter is bumped BEFORE acquiring
        (so a caller queued on the lock counts too) and the `_mgr_locks` entry is
        popped on exit ONLY when the counter is back to 0 AND the id is absent
        from `_sessions` — i.e. only after the session is genuinely torn down.

        Popping on every release would be a correctness regression, not a fix: a
        queued waiter would be left holding an orphaned Lock while the next
        caller minted a SECOND Lock for the same id, reopening the double-spawn
        race the per-id lock exists to prevent. A live session keeps its lock.
        """
        self._lock_waiters[session_id] = self._lock_waiters.get(session_id, 0) + 1
        try:
            async with self._lock_for(session_id):
                yield
        finally:
            remaining = self._lock_waiters.get(session_id, 1) - 1
            if remaining > 0:
                self._lock_waiters[session_id] = remaining
            else:
                self._lock_waiters.pop(session_id, None)
                if session_id not in self._sessions:
                    self._mgr_locks.pop(session_id, None)

    @staticmethod
    def _warm_key(cwd: str | None, permission_mode: str) -> tuple[str, str]:
        """Normalize the warm-pool key. cwd defaults to the home dir exactly as
        PersistentSession does, so prewarm(None, ...) and a get_or_create with
        cwd=None claim the same entry."""
        return (cwd or str(Path.home()), permission_mode)

    @staticmethod
    def _is_warm_healthy(session: PersistentSession) -> bool:
        """A warm session is claimable only if its subprocess is alive AND its
        single stdout reader is still running (a dead reader means no events
        would ever reach a real turn). Health-checked before every hand-off."""
        return (
            session.alive
            and session._reader_task is not None
            and not session._reader_task.done()
        )

    @staticmethod
    async def _start_or_cleanup(session: PersistentSession) -> None:
        """Start a fresh session; if start() raises after spawning, stop the
        half-started session (so no subprocess + stderr task is orphaned) and
        re-raise. (F5)
        """
        try:
            await session.start()
        except BaseException:
            await session.stop()
            raise

    async def get_or_create(self, session_id: str | None = None,
                            cwd: str | None = None,
                            permission_mode: str = "default",
                            name: str | None = None) -> PersistentSession:
        """Get an existing session or create a new one."""
        # No id to resume: a brand-new session whose real id is unknown until the
        # system event. Serialize no-id creates under the dedicated creation lock;
        # register() (also lock-guarded) stores it once the id is discovered.
        if not session_id:
            async with self._create_lock:
                # CLAIM a pre-warmed process for this (cwd, mode) if one is
                # available and healthy — this is the start/resume latency win:
                # the warm process already paid the ~4s creds+Node boot off the
                # critical path, so the user's first turn skips it. A warm entry
                # that has gone unhealthy (process died, reader stopped) is
                # stopped and discarded, then we fall through to a fresh spawn.
                key = self._warm_key(cwd, permission_mode)
                warm = self._warm_pool.pop(key, None)
                if warm is not None:
                    if self._is_warm_healthy(warm):
                        # The warm session booted with no --name; adopt the
                        # requested name only for visibility purposes — the
                        # process is already running so we cannot re-pass it,
                        # but the field keeps later bookkeeping consistent.
                        if name and warm.name is None:
                            warm.name = name
                        warm._last_activity = time.monotonic()
                        return warm
                    await warm.stop()

                session = PersistentSession(
                    session_id=session_id,
                    cwd=cwd,
                    permission_mode=permission_mode,
                    name=name,
                )
                await self._start_or_cleanup(session)
                return session

        # Resuming a known id: serialize the read-then-mutate critical section so
        # two concurrent requests for the same id can't both spawn a subprocess.
        async with self._lock_scope(session_id):
            if session_id in self._sessions:
                session = self._sessions[session_id]
                if session.alive:
                    return session
                # Dead session — remove and recreate.
                del self._sessions[session_id]

            session = PersistentSession(
                session_id=session_id,
                cwd=cwd,
                permission_mode=permission_mode,
                name=name,
            )
            await self._start_or_cleanup(session)
            # Store atomically with creation so a spawned subprocess is always
            # reachable by stop_all().
            self._sessions[session_id] = session
            return session

    async def prewarm(self, cwd: str | None = None,
                      permission_mode: str = "default") -> None:
        """Pre-boot a no-resume session for (cwd, permission_mode) into the pool.

        Pays the ~4s credential-export + Node/plugin boot cost OFF the user's
        critical path so the next new (or resume-warmed) chat for the same
        (cwd, mode) can CLAIM it via get_or_create instead of spawning cold.

        Idempotent per key: if a HEALTHY warm entry already exists for the key
        we return without spawning a second (so repeated panel-opens can't pile
        up processes). An existing-but-unhealthy entry is replaced. The pool is
        size-capped here (evict the oldest on overflow) and age-reaped by
        _reap_idle, so unclaimed spares are bounded both ways and never leak.

        Warmed sessions carry no --resume and their real session_id is only
        known once their system event fires; they are claimed by (cwd, mode),
        never by id, so they can never collide with the --resume path.
        """
        key = self._warm_key(cwd, permission_mode)
        async with self._create_lock:
            existing = self._warm_pool.get(key)
            if existing is not None:
                if self._is_warm_healthy(existing):
                    return
                # Stale/dead spare — drop it before re-warming.
                self._warm_pool.pop(key, None)
                await existing.stop()

            session = PersistentSession(
                session_id=None,
                cwd=cwd,
                permission_mode=permission_mode,
            )
            try:
                await self._start_or_cleanup(session)
            except BaseException as e:  # noqa: BLE001 — prewarm is best-effort
                logger.warning("prewarm for %s failed: %s", key, e)
                return
            session._last_activity = time.monotonic()
            self._warm_pool[key] = session

            # Hard size cap: evict the OLDEST spare so a burst of panel-opens
            # across many cwds can never grow the pool without bound.
            if len(self._warm_pool) > _WARM_POOL_MAX:
                oldest_key = min(
                    self._warm_pool,
                    key=lambda k: self._warm_pool[k]._last_activity,
                )
                evicted = self._warm_pool.pop(oldest_key)
                await evicted.stop()

    def register(self, session: PersistentSession):
        """Register a session after we know its ID.

        A single dict assignment is atomic across asyncio await points, so this
        stays synchronous (chat.py calls it without await). The real double-spawn
        race lives in get_or_create, which _create_lock / _lock_for serialize.
        """
        if session.session_id:
            self._sessions[session.session_id] = session

    async def stop(self, session_id: str):
        """Stop a specific session."""
        async with self._lock_scope(session_id):
            if session_id in self._sessions:
                await self._sessions[session_id].stop()
                del self._sessions[session_id]

    async def respawn(self, session_id: str, cwd: str | None = None,
                      permission_mode: str | None = None) -> PersistentSession:
        """Tear down a session's stale subprocess and start a fresh one.

        Used to recover from credential expiry: the long-lived `claude` process
        caches the env/credentials it had at spawn time, so after the user
        re-authenticates we must replace the process to pick up new creds. Scoped
        to the one session — other sessions and their SSE streams are untouched.

        Reuses the existing session's cwd (so --resume lands in the right dir)
        unless an explicit cwd is given. Likewise, the existing session's
        permission_mode is preserved unless the caller passes one, so a
        credential-recovered session keeps its write ability instead of
        silently reverting to the interactive "default" mode. The fresh process
        resumes the same session_id, preserving conversation history.
        """
        # Serialize the whole read-existing/stop/del/construct/start/store
        # sequence on this id so a concurrent get_or_create/stop/respawn for the
        # same id can't interleave. Do the start-failure cleanup inline rather
        # than via self.stop() — asyncio.Lock is not reentrant and self.stop()
        # would deadlock on this same per-id lock.
        async with self._lock_scope(session_id):
            existing = self._sessions.get(session_id)
            resume_cwd = cwd or (existing.cwd if existing else None)
            resume_mode = permission_mode or (existing.permission_mode if existing else "default")
            if existing:
                await existing.stop()
                del self._sessions[session_id]

            session = PersistentSession(
                session_id=session_id,
                cwd=resume_cwd,
                permission_mode=resume_mode,
            )
            await self._start_or_cleanup(session)
            self._sessions[session_id] = session
            return session

    async def _reap_idle(self) -> None:
        """Stop sessions idle past the TTL with no active consumer.

        A session is reaped only when BOTH hold:
          - it has no in-flight send() / SSE consumer (_active_consumers == 0),
            so an open ChatPage stream is never torn out from under the client; and
          - it has been idle (no send() activity) longer than the TTL.

        This catches a clarify session whose nav-tab-switch abandoned it: the
        single reader finishes the turn and the session then idles with zero
        consumers, becoming eligible after the TTL. Stopping goes through the
        normal self.stop() / per-id lock path so it can't race a concurrent
        lifecycle op or mutate _sessions while another op holds the lock — we
        snapshot the candidate ids first, then stop() each (which re-checks the
        dict under the lock).

        It ALSO sweeps the warm pool, evicting unclaimed spares older than the
        (shorter) warm TTL so a pre-warmed process nobody claimed is reclaimed
        promptly — the age-reap half of the bounded-no-leak guarantee.
        """
        now = time.monotonic()
        candidates = [
            sid for sid, s in self._sessions.items()
            if s._active_consumers == 0
            and (now - s._last_activity) > _SESSION_IDLE_TTL_SECONDS
        ]
        for sid in candidates:
            try:
                await self.stop(sid)
                logger.info("Reaped idle session %s", sid)
            except Exception as e:  # noqa: BLE001 — one bad stop must not stop the sweep
                logger.warning("Failed to reap idle session %s: %s", sid, e)

        # Also age-reap unclaimed warm spares past the (shorter) warm TTL. A
        # spare nobody claimed within the window is a wasted process, so reclaim
        # it. Snapshot the keys first — never mutate the dict mid-iteration —
        # then stop each. Together with the insert-time size cap, this is the
        # bounded-no-leak guarantee for the warm pool.
        warm_candidates = [
            key for key, s in self._warm_pool.items()
            if (now - s._last_activity) > _WARM_IDLE_TTL_SECONDS
        ]
        for key in warm_candidates:
            session = self._warm_pool.pop(key, None)
            if session is None:
                continue
            try:
                await session.stop()
                logger.info("Reaped idle warm session for %s", key)
            except Exception as e:  # noqa: BLE001 — one bad stop must not stop the sweep
                logger.warning("Failed to reap warm session for %s: %s", key, e)

    async def _reaper_loop(self) -> None:
        """Periodic sweep that stops idle sessions.

        Mirrors sessions.py:_session_watcher exactly: sleeps FIRST (so it is
        inert on startup — start hooks fire under aiohttp_client(app) in every
        test), re-raises CancelledError so stop_reaper() can cancel+await
        cleanly, and a per-iteration try/except catches everything else so one
        bad sweep never kills the loop. It spawns no subprocess and needs no
        to_thread (it's pure async).
        """
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            try:
                await self._reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad sweep must not kill the loop
                logger.warning("Session reaper iteration failed: %s", e)

    def start_reaper(self) -> asyncio.Task:
        """Launch the background idle reaper (idempotent).

        Call from the app's on_startup hook. Safe to call repeatedly — a
        running task is left in place. Returns the live reaper task so callers
        (e.g. chat.register's startup hook) can mirror it onto app state for the
        /api/workers status registry, whose _derive_status reads app[taskKey].
        """
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())
        return self._reaper_task

    async def stop_reaper(self) -> None:
        """Cancel AND await the reaper so it tears down cleanly.

        Call from the app's on_cleanup/on_shutdown hook (stop_all already runs
        on shutdown; stopping the reaper here keeps the suite warning-free).
        """
        task = self._reaper_task
        self._reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def stop_all(self):
        """Stop all sessions (for shutdown).

        Stop concurrently and tolerate failures: one raising/slow stop() must not
        prevent the rest from being stopped on shutdown. (F6)
        """
        await self.stop_reaper()
        await asyncio.gather(
            *[s.stop() for s in self._sessions.values()],
            *[s.stop() for s in self._warm_pool.values()],
            return_exceptions=True,
        )
        self._sessions.clear()
        self._warm_pool.clear()
        # stop_all() stops the PersistentSessions directly (not via self.stop()),
        # so their per-id lock entries would otherwise survive the teardown. Drop
        # them only when nothing is in flight — an op still holding/queued on a
        # lock must keep its entry, or the next caller for that id would mint a
        # second Lock and lose the serialization.
        if not self._lock_waiters:
            self._mgr_locks.clear()

    @property
    def active_sessions(self) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.alive]
