"""GET /api/plugins — read-only plugin inventory."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from server import filestore


PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def register(app: web.Application):
    app.router.add_get("/api/plugins", list_plugins)


async def list_plugins(request: web.Request) -> web.Response:
    # Installed plugins. async_read_json already degrades to ({}, None) for a
    # missing/unreadable/malformed file, so the endpoint tolerance is the
    # helper's contract; only a non-dict top level still needs a guard here.
    data, _etag = await filestore.async_read_json(PLUGINS_PATH)
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}

    installed = []
    for name, entries in (plugins.items() if isinstance(plugins, dict) else []):
        # Skip a single malformed entry (non-list value, non-dict entry)
        # instead of truncating every plugin listed after it.
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            installed.append({
                "name": name,
                "scope": entry.get("scope", ""),
                "version": entry.get("version", ""),
                "installPath": entry.get("installPath", ""),
                "installedAt": entry.get("installedAt"),
            })

    # Enabled state from settings.json
    settings, _settings_etag = await filestore.async_read_json(SETTINGS_PATH)
    enabled_map = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
    if not isinstance(enabled_map, dict):
        enabled_map = {}

    for p in installed:
        p["enabled"] = enabled_map.get(p["name"], True)

    return web.json_response(installed)
