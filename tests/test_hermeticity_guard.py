"""Pin the conftest hermeticity trap itself (incident 2026-07-27).

If someone deletes tests/conftest.py or weakens the autouse guard, these tests
fail — they are the regression net over the net. See conftest.py's module
docstring for the incident these traps exist to prevent (real Slack messages
posted to real colleagues from approve-path tests).
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import ForbiddenExternalCallError  # noqa: E402

import server.routes.email as email_mod  # noqa: E402
import server.routes.slack as slack_mod  # noqa: E402


async def test_slack_mcp_connect_is_blocked_in_tests():
    """_connect_mcp (untouched by any stub) must die at the stdio_client trap
    BEFORE spawning anything — this is the exact path that posted real
    messages when a test missed its stub."""
    with pytest.raises(ForbiddenExternalCallError):
        await slack_mod._connect_mcp()
    assert slack_mod._mcp_state.session is None  # nothing half-opened


async def test_email_owa_write_connect_is_blocked_in_tests():
    with pytest.raises(ForbiddenExternalCallError):
        await email_mod._connect_owa_write()


async def test_claude_binary_spawn_is_blocked_in_tests():
    with pytest.raises(ForbiddenExternalCallError):
        await asyncio.create_subprocess_exec("claude", "-p", "hi")


async def test_slack_mcp_binary_spawn_is_blocked_by_basename():
    """Even a full path to the binary is refused (basename match)."""
    with pytest.raises(ForbiddenExternalCallError):
        await asyncio.create_subprocess_exec("/usr/local/bin/slack-mcp")


async def test_ordinary_subprocesses_still_work():
    """The guard is a denylist, not a blanket ban — hooks/oscron-style
    subprocess tests keep working."""
    proc = await asyncio.create_subprocess_exec(
        "echo", "ok", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    assert out.strip() == b"ok"
