"""WebSocket manager for pushing file-change events to connected clients."""
from __future__ import annotations

import asyncio
import json
import weakref
from pathlib import Path

from aiohttp import web, WSMsgType


class WebSocketManager:
    """Manages connected WebSocket clients and broadcasts change events."""

    def __init__(self):
        self._clients: weakref.WeakSet = weakref.WeakSet()

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.CLOSE:
                    break
        finally:
            self._clients.discard(ws)
        return ws

    async def broadcast(self, event_type: str, data: dict | None = None):
        """Push an event to all connected clients."""
        payload = json.dumps({"type": event_type, **(data or {})})
        closed = []
        for ws in self._clients:
            try:
                await ws.send_str(payload)
            except (ConnectionResetError, RuntimeError):
                closed.append(ws)
        for ws in closed:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)
