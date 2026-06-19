"""Atomic file read/write with etag-based optimistic concurrency.

Every managed file gets an etag derived from its mtime. Writes use
tmp+rename for atomicity. Concurrent edits (from CLI or another GUI tab)
are detected via If-Match and rejected with 409.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def etag_for(path: Path) -> str | None:
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}"
    except OSError:
        return None


def read_text(path: Path) -> tuple[str, str | None]:
    """Return (content, etag). If file doesn't exist, return ('', None)."""
    try:
        content = path.read_text(encoding="utf-8")
        return content, etag_for(path)
    except OSError:
        return "", None


def read_json(path: Path) -> tuple[Any, str | None]:
    """Return (parsed_json, etag). If missing/invalid, return ({}, None)."""
    text, etag = read_text(path)
    if not text:
        return {}, None
    try:
        return json.loads(text), etag
    except json.JSONDecodeError:
        return {}, etag


def write_text(path: Path, content: str, expected_etag: str | None = None) -> str:
    """Atomic write with optional optimistic concurrency check.

    Args:
        path: Target file.
        content: New content.
        expected_etag: If provided, the write is rejected (raises ConflictError)
            if the file's current etag doesn't match.

    Returns:
        New etag after write.

    Raises:
        ConflictError: File was modified since the client last read it.
    """
    if expected_etag is not None:
        current = etag_for(path)
        if current is not None and current != expected_etag:
            raise ConflictError(path, expected_etag, current)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    closed = False
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        closed = True
        os.replace(tmp, str(path))
    except BaseException:
        # Only close if we haven't already (a failure in os.replace happens AFTER
        # the close above — re-closing would hit EBADF and mask the real error).
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return etag_for(path) or ""


def write_json(path: Path, data: Any, expected_etag: str | None = None) -> str:
    """Atomic JSON write with optional concurrency check."""
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return write_text(path, content, expected_etag)


def delete_file(path: Path) -> None:
    """Delete a file if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class ConflictError(Exception):
    """Raised when a write conflicts with a concurrent external modification."""

    def __init__(self, path: Path, expected: str, actual: str):
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Conflict on {path.name}: expected etag {expected}, found {actual}"
        )
