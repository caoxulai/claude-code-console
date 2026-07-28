"""Suite-wide hermeticity guard — tests must NEVER reach real external processes.

Incident (2026-07-27, during D-077): approve-path tests carrying routable
real conversation ids reached the REAL slack-mcp and posted messages to real
colleagues from the operator's account. Hermeticity relied entirely on
per-test stubbing (_stub_gated_write / _stub_owa_write), so one missed stub in
one intermediate test version was enough to send. These autouse traps make the
whole suite structurally incapable of spawning a real MCP server or the real
claude CLI, regardless of what any individual test forgets:

* ``stdio_client`` is replaced in both assistant modules — every MCP connect
  path (slack read/write, email read, graph, OWA write, one-shots) funnels
  through it, so a real spawn attempt fails the test loudly instead of
  authenticating as the operator.
* ``asyncio.create_subprocess_exec`` refuses argv[0] basenames that belong to
  real agent/MCP binaries; everything else (sh, crontab, systemctl fakes …)
  passes through untouched.

Tests that exercise connect/spawn plumbing keep working: their own
``monkeypatch.setattr(slack_mod, "stdio_client", ...)`` / fake-exec stubs
simply override the trap for that test and are restored afterwards.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

# Repo root importable before any test module loads (test files repeat this
# hack individually today; conftest runs first so collection never depends on
# which file imports first).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class ForbiddenExternalCallError(AssertionError):
    """A test tried to spawn a real external process (MCP server / claude CLI)."""


_BLOCKED_BASENAMES = {"claude", "slack-mcp", "aws-outlook-mcp"}


def _blocked_stdio_client(params, *args, **kwargs):
    command = getattr(params, "command", params)
    raise ForbiddenExternalCallError(
        f"stdio_client() would spawn a REAL MCP server ({command!r}) from a "
        "test — this is how real Slack messages got sent to real people on "
        "2026-07-27. Stub the seam (_stub_gated_write / _stub_owa_write) or "
        "monkeypatch stdio_client with a fake instead."
    )


@pytest.fixture(autouse=True)
def _no_real_external_processes(monkeypatch):
    import server.routes.email as email_mod
    import server.routes.slack as slack_mod

    monkeypatch.setattr(slack_mod, "stdio_client", _blocked_stdio_client)
    monkeypatch.setattr(email_mod, "stdio_client", _blocked_stdio_client)

    real_exec = asyncio.create_subprocess_exec

    async def guarded_exec(program, *args, **kwargs):
        if os.path.basename(str(program)) in _BLOCKED_BASENAMES:
            raise ForbiddenExternalCallError(
                f"create_subprocess_exec({str(program)!r}) would spawn a real "
                "agent/MCP binary from a test. Stub the agent seam "
                "(_run_slack_agent / _run_email_agent) or fake the exec instead."
            )
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)
