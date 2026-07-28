"""D-078 item 27f — bounded async-I/O sweep of the simple route modules.

The settings/hooks/mcp/plugins handlers used to do their sidecar JSON I/O
synchronously on the event loop. They now route it through the EXISTING
``filestore.async_read_json`` / ``async_write_json`` helpers (which are just
``asyncio.to_thread(read_json/write_json, ...)`` and return the same
``(parsed, etag)`` tuple / raise the same ``ConflictError``).

This file pins BOTH halves of "mechanical":

* behaviour — every converted handler keeps its exact envelope, the PUT 409
  conflict bodies still carry ``current`` + ``etag``, and a malformed
  installed_plugins.json still degrades to 200 rather than 500;
* the sweep is really wired — the handlers actually AWAIT the async helpers
  (patched-seam call counting, not a source grep), and the sweep stopped at its
  declared boundary: slack.py / email.py keep their own sync etag discipline
  (D-076/D-077) and memory.py/skills.py keep ``filestore.read_text`` because
  filestore has no async text sibling.

A dedicated module (not the test_server.py monolith) per the shared-file
write-contention hazard; the ~61 existing /api/settings, /api/hooks, /api/mcp
and /api/plugins tests in tests/test_server.py must pass UNMODIFIED.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:  # pragma: no cover
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))
from server.app import create_app  # noqa: E402
from server import filestore  # noqa: E402
import server.routes.hooks as hooks_mod  # noqa: E402
import server.routes.mcp as mcp_mod  # noqa: E402
import server.routes.plugins as plugins_mod  # noqa: E402
import server.routes.settings as settings_mod  # noqa: E402

SERVER_ROUTES = Path(__file__).parent.parent / "server" / "routes"


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    application["allowed_cwd_roots"] = [tmp_path]
    application["default_cwd"] = tmp_path
    application["permission_mode"] = "bypassPermissions"
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture(autouse=True)
def _reset_worker_primitives():
    """create_app() starts the slack+email workers; their lazily loop-bound
    asyncio primitives otherwise leak across tests ("bound to a different event
    loop" at the NEXT test's teardown)."""
    def _clear():
        for mod_name in ("server.routes.slack", "server.routes.email"):
            mod = sys.modules.get(mod_name)
            if mod is None:
                continue
            for attr in ("_scan_in_progress_lock", "_scan_wake_event", "_mcp_connect_lock"):
                if hasattr(mod, attr):
                    setattr(mod, attr, None)
    _clear()
    yield
    _clear()


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", p)
    return p


@pytest.fixture
def hooks_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "hooks-settings.json"
    monkeypatch.setattr(hooks_mod, "SETTINGS_PATH", p)
    return p


@pytest.fixture
def mcp_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / ".claude.json"
    monkeypatch.setattr(mcp_mod, "GLOBAL_STATE_PATH", p)
    monkeypatch.setattr(mcp_mod, "LEGACY_AGENT_MCP_PATH", tmp_path / "legacy.json")
    return p


@pytest.fixture
def io_calls(monkeypatch):
    """Count the async filestore helpers actually awaited by the handlers.

    Patching the filestore module attributes works because every route module
    calls them as ``filestore.async_read_json(...)`` (attribute lookup at call
    time), so this observes the REAL request path rather than grepping source.
    """
    calls = {"read": [], "write": []}
    real_read = filestore.async_read_json
    real_write = filestore.async_write_json

    async def counting_read(path, *a, **kw):
        calls["read"].append(Path(path))
        return await real_read(path, *a, **kw)

    async def counting_write(path, *a, **kw):
        calls["write"].append(Path(path))
        return await real_write(path, *a, **kw)

    monkeypatch.setattr(filestore, "async_read_json", counting_read)
    monkeypatch.setattr(filestore, "async_write_json", counting_write)
    return calls


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
async def test_get_settings_uses_async_read_and_keeps_envelope(client, settings_file, io_calls):
    body = await (await client.get("/api/settings")).json()
    assert body["data"] == {"theme": "dark"}
    assert body["etag"]
    assert settings_file in io_calls["read"]


async def test_put_settings_uses_async_write_and_keeps_envelope(client, settings_file, io_calls):
    etag = (await (await client.get("/api/settings")).json())["etag"]
    resp = await client.put("/api/settings", json={"data": {"theme": "light"}, "etag": etag})
    assert resp.status == 200
    body = await resp.json()
    assert body["data"] == {"theme": "light"}
    assert body["etag"]
    assert json.loads(settings_file.read_text())["theme"] == "light"
    assert settings_file in io_calls["write"]


async def test_put_settings_conflict_keeps_current_and_etag(client, settings_file, io_calls):
    etag = (await (await client.get("/api/settings")).json())["etag"]
    time.sleep(0.01)  # etags are mtime_ns-based; force a distinct mtime
    settings_file.write_text(json.dumps({"theme": "externally-set"}), encoding="utf-8")

    resp = await client.put("/api/settings", json={"data": {"theme": "mine"}, "etag": etag})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert body["current"] == {"theme": "externally-set"}
    assert body["etag"]
    # The conflict re-read is async too.
    assert io_calls["read"].count(settings_file) >= 2


async def test_get_settings_local_uses_async_read(client, tmp_path, monkeypatch, io_calls):
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps({"localKey": "v"}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_LOCAL_PATH", p)
    body = await (await client.get("/api/settings/local")).json()
    assert body["data"] == {"localKey": "v"} and body["etag"]
    assert p in io_calls["read"]


# --------------------------------------------------------------------------- #
# hooks
# --------------------------------------------------------------------------- #
async def test_get_hooks_uses_async_read_and_keeps_envelope(client, hooks_file, io_calls):
    body = await (await client.get("/api/hooks")).json()
    assert body == {"hooks": {}, "etag": None}
    assert hooks_file in io_calls["read"]


async def test_put_hooks_uses_async_write_and_preserves_other_keys(client, hooks_file, io_calls):
    hooks_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    etag = (await (await client.get("/api/hooks")).json())["etag"]

    new_hooks = {"PostToolUse": [{"matcher": "Bash"}]}
    resp = await client.put("/api/hooks", json={"hooks": new_hooks, "etag": etag})
    assert resp.status == 200
    body = await resp.json()
    assert body["hooks"] == new_hooks and body["etag"]
    on_disk = json.loads(hooks_file.read_text())
    assert on_disk["hooks"] == new_hooks
    assert on_disk["theme"] == "dark"
    assert hooks_file in io_calls["write"]


async def test_put_hooks_conflict_keeps_current_and_etag(client, hooks_file, io_calls):
    hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    etag = (await (await client.get("/api/hooks")).json())["etag"]
    time.sleep(0.01)
    hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "X"}]}}), encoding="utf-8")

    resp = await client.put("/api/hooks", json={"hooks": {"PostToolUse": []}, "etag": etag})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert body["current"] == {"hooks": {"PreToolUse": [{"matcher": "X"}]}}
    assert body["etag"]


# --------------------------------------------------------------------------- #
# mcp
# --------------------------------------------------------------------------- #
async def test_list_mcp_uses_async_read_and_keeps_envelope(client, mcp_file, io_calls):
    mcp_file.write_text(json.dumps({
        "numStartups": 7,
        "mcpServers": {"s": {"command": "x", "env": {"API_KEY": "shh", "REGION": "r"}}},
    }), encoding="utf-8")

    body = await (await client.get("/api/mcp")).json()
    assert body["etag"]
    (entry,) = body["servers"]
    assert entry["name"] == "s"
    assert entry["env"] == {"API_KEY": "***", "REGION": "r"}
    assert mcp_file in io_calls["read"]


async def test_mcp_add_update_delete_use_async_io(client, mcp_file, io_calls):
    mcp_file.write_text(json.dumps({"numStartups": 7, "mcpServers": {}}), encoding="utf-8")

    add = await client.post("/api/mcp", json={"name": "srv", "config": {"command": "z"}})
    assert add.status == 201
    assert (await add.json())["name"] == "srv"

    etag = (await (await client.get("/api/mcp")).json())["etag"]
    upd = await client.put("/api/mcp/srv", json={"config": {"command": "new"}, "etag": etag})
    assert upd.status == 200
    assert json.loads(mcp_file.read_text())["mcpServers"]["srv"]["command"] == "new"
    # Unrelated ~/.claude.json keys survive the read-modify-write.
    assert json.loads(mcp_file.read_text())["numStartups"] == 7

    etag = (await (await client.get("/api/mcp")).json())["etag"]
    dele = await client.delete("/api/mcp/srv", json={"etag": etag})
    assert dele.status == 200
    assert (await dele.json())["deleted"] == "srv"
    assert json.loads(mcp_file.read_text())["mcpServers"] == {}

    assert io_calls["write"].count(mcp_file) == 3


async def test_mcp_conflict_still_409(client, mcp_file, io_calls):
    mcp_file.write_text(json.dumps({"mcpServers": {"s": {"command": "x"}}}), encoding="utf-8")
    etag = (await (await client.get("/api/mcp")).json())["etag"]
    time.sleep(0.01)
    mcp_file.write_text(json.dumps({"mcpServers": {"s": {"command": "external"}}}), encoding="utf-8")

    resp = await client.put("/api/mcp/s", json={"config": {"command": "mine"}, "etag": etag})
    assert resp.status == 409
    assert (await resp.json())["error"] == "conflict"


# --------------------------------------------------------------------------- #
# plugins
# --------------------------------------------------------------------------- #
async def test_list_plugins_uses_async_read_and_keeps_shape(client, tmp_path, monkeypatch, io_calls):
    plugins_path = tmp_path / "installed_plugins.json"
    settings_path = tmp_path / "plugin-settings.json"
    plugins_path.write_text(json.dumps({"plugins": {
        "alpha": [{"scope": "user", "version": "1.0", "installPath": "/a", "installedAt": 1}],
        "beta": [{"scope": "user", "version": "2.0", "installPath": "/b", "installedAt": 2}],
    }}), encoding="utf-8")
    settings_path.write_text(json.dumps({"enabledPlugins": {"beta": False}}), encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", plugins_path)
    monkeypatch.setattr(plugins_mod, "SETTINGS_PATH", settings_path)

    rows = await (await client.get("/api/plugins")).json()
    by_name = {p["name"]: p for p in rows}
    assert by_name["alpha"]["enabled"] is True
    assert by_name["beta"]["enabled"] is False
    assert by_name["alpha"]["version"] == "1.0"
    assert plugins_path in io_calls["read"]
    assert settings_path in io_calls["read"]


@pytest.mark.parametrize("payload", [
    '{"plugins": "not-a-dict"}',   # plugins section is not a mapping
    '["not", "a", "dict"]',        # top-level is not a mapping
    '{not json at all',            # unparseable
    '',                            # empty file
])
async def test_list_plugins_tolerates_malformed_file(client, tmp_path, monkeypatch, payload):
    """A malformed installed_plugins.json still yields 200 + [], never a 500."""
    bad = tmp_path / "installed_plugins.json"
    bad.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", bad)
    monkeypatch.setattr(plugins_mod, "SETTINGS_PATH", tmp_path / "absent.json")

    resp = await client.get("/api/plugins")
    assert resp.status == 200
    assert await resp.json() == []


async def test_list_plugins_tolerates_malformed_settings(client, tmp_path, monkeypatch):
    """A malformed settings.json must not 500 the listing; entries default to enabled."""
    plugins_path = tmp_path / "installed_plugins.json"
    plugins_path.write_text(json.dumps({"plugins": {
        "alpha": [{"scope": "user", "version": "1.0"}],
    }}), encoding="utf-8")
    bad_settings = tmp_path / "settings.json"
    bad_settings.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", plugins_path)
    monkeypatch.setattr(plugins_mod, "SETTINGS_PATH", bad_settings)

    rows = await (await client.get("/api/plugins")).json()
    assert [r["name"] for r in rows] == ["alpha"]
    assert rows[0]["enabled"] is True


async def test_list_plugins_skips_single_malformed_entry(client, tmp_path, monkeypatch):
    """A bad entry is skipped, never truncating the plugins listed after it."""
    plugins_path = tmp_path / "installed_plugins.json"
    plugins_path.write_text(json.dumps({"plugins": {
        "alpha": "not-a-list",
        "beta": ["not-a-dict", {"scope": "user", "version": "2.0"}],
        "gamma": [{"scope": "project", "version": "3.0"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", plugins_path)
    monkeypatch.setattr(plugins_mod, "SETTINGS_PATH", tmp_path / "absent.json")

    rows = await (await client.get("/api/plugins")).json()
    assert sorted(r["name"] for r in rows) == ["beta", "gamma"]


# --------------------------------------------------------------------------- #
# Sweep boundary (anti-scope-creep pins)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", ["settings.py", "hooks.py", "mcp.py", "plugins.py"])
def test_converted_modules_use_only_the_async_helpers(module):
    src = (SERVER_ROUTES / module).read_text(encoding="utf-8")
    assert "filestore.async_read_json" in src, f"{module} was not converted"
    assert "filestore.read_json(" not in src, f"{module} still reads JSON synchronously"
    assert "filestore.write_json(" not in src, f"{module} still writes JSON synchronously"


@pytest.mark.parametrize("module", ["slack.py", "email.py"])
def test_queue_modules_are_untouched_by_the_sweep(module):
    """slack.py / email.py keep their own sync etag/concurrency discipline
    (D-076/D-077) — the sweep must not have reached them."""
    src = (SERVER_ROUTES / module).read_text(encoding="utf-8")
    assert "filestore.read_json(" in src, f"{module} sync sidecar I/O was swept — out of scope"


@pytest.mark.parametrize("module", ["memory.py", "skills.py"])
def test_text_sites_deliberately_left_synchronous(module):
    """filestore has no async text sibling, so read_text/write_text sites stay
    as-is rather than inventing one."""
    src = (SERVER_ROUTES / module).read_text(encoding="utf-8")
    assert "filestore.read_text(" in src
    assert not hasattr(filestore, "async_read_text")


def test_omission_is_recorded_in_source():
    """The skipped text-I/O site carries a one-line note so the omission reads
    as intentional rather than an oversight."""
    src = (SERVER_ROUTES / "memory.py").read_text(encoding="utf-8")
    assert "async text sibling" in src
