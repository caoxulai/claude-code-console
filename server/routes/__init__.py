"""Shared helpers for route handlers."""
from __future__ import annotations

import json

from aiohttp import web


async def read_json_body(request: web.Request) -> dict:
    """Parse a JSON request body, returning 400 (not 500) on malformed input.

    aiohttp's request.json() raises json.JSONDecodeError on a non-JSON or empty
    body, which otherwise surfaces as an opaque 500. Handlers that expect a JSON
    object should use this so a bad client request is a clean 400. A body that
    parses to a non-object (list/scalar) is also rejected.
    """
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(reason="request body must be valid JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(reason="request body must be a JSON object")
    return data
