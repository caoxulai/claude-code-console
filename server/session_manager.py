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

        # Drain stderr in background
        asyncio.create_task(self._drain_stderr())

        return None

    async def _drain_stderr(self):
        if self._proc and self._proc.stderr:
            async for _ in self._proc.stderr:
                pass

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

                yield evt

                # "result" marks end of this turn
                if evt.get("type") == "result":
                    break

    async def stop(self):
        """Terminate the Claude process."""
        if self._proc and self._proc.returncode is None:
            self._proc.stdin.close()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.terminate()
                await self._proc.wait()
        self._started = False


class SessionManager:
    """Manages multiple persistent sessions."""

    def __init__(self):
        self._sessions: dict[str, PersistentSession] = {}

    async def get_or_create(self, session_id: str | None = None,
                            cwd: str | None = None,
                            permission_mode: str = "default",
                            name: str | None = None) -> PersistentSession:
        """Get an existing session or create a new one."""
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            if session.alive:
                return session
            # Dead session — remove and recreate
            del self._sessions[session_id]

        session = PersistentSession(
            session_id=session_id,
            cwd=cwd,
            permission_mode=permission_mode,
            name=name,
        )
        await session.start()
        return session

    def register(self, session: PersistentSession):
        """Register a session after we know its ID."""
        if session.session_id:
            self._sessions[session.session_id] = session

    async def stop(self, session_id: str):
        """Stop a specific session."""
        if session_id in self._sessions:
            await self._sessions[session_id].stop()
            del self._sessions[session_id]

    async def stop_all(self):
        """Stop all sessions (for shutdown)."""
        for session in self._sessions.values():
            await session.stop()
        self._sessions.clear()

    @property
    def active_sessions(self) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.alive]
