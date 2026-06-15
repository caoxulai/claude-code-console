"""Persistent Claude session manager.

Spawns a long-lived `claude --print --input-format stream-json --output-format stream-json`
process per session. Sends user messages via stdin, reads streaming responses from stdout.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import AsyncIterator


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

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def send(self, text: str) -> AsyncIterator[dict]:
        """Send a user message and yield response events as they stream back."""
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
            if self._proc and self._proc.stdin:
                self._proc.stdin.write((msg + "\n").encode())
                await self._proc.stdin.drain()

            # Read response lines until we get a "result" event
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Capture session_id from init
                if evt.get("type") == "system" and evt.get("session_id"):
                    self.session_id = evt["session_id"]

                # Tag auth/credential failures so the client can show actionable
                # guidance + a bounded retry, instead of a raw 403. We classify
                # using the result text plus recent stderr (some credential
                # errors only appear on stderr).
                if evt.get("type") == "result" and evt.get("is_error"):
                    blob = " ".join(filter(None, [
                        str(evt.get("result", "")),
                        self._recent_stderr(),
                    ]))
                    if is_auth_error(blob):
                        evt = {**evt, "errorKind": "auth"}

                yield evt

                # "result" marks end of this turn
                if evt.get("type") == "result":
                    break

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
        # Brand-new sessions arrive with session_id=None (no resume id), so there
        # is no id to key a lock on at creation time. Concurrent no-id creates are
        # serialized by this single creation lock; each produces its own session
        # that register() later keys by the discovered id.
        self._create_lock = asyncio.Lock()

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """Get-or-create the per-session-id lifecycle lock."""
        lock = self._mgr_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._mgr_locks[session_id] = lock
        return lock

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
        async with self._lock_for(session_id):
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
        async with self._lock_for(session_id):
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
        async with self._lock_for(session_id):
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

    async def stop_all(self):
        """Stop all sessions (for shutdown).

        Stop concurrently and tolerate failures: one raising/slow stop() must not
        prevent the rest from being stopped on shutdown. (F6)
        """
        await asyncio.gather(
            *[s.stop() for s in self._sessions.values()],
            return_exceptions=True,
        )
        self._sessions.clear()

    @property
    def active_sessions(self) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.alive]
