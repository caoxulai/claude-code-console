"""GET /api/plugins — read-only plugin inventory."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web


PLUGINS_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def register(app: web.Application):
    app.router.add_get("/api/plugins", list_plugins)


async def list_plugins(request: web.Request) -> web.Response:
    # Installed plugins
    installed = []
    if PLUGINS_PATH.exists():
        try:
            data = json.loads(PLUGINS_PATH.read_text())
            plugins = data.get("plugins", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            # Tolerate a malformed installed_plugins.json (unreadable or a
            # non-dict top level) rather than 500 the endpoint.
            plugins = {}
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
    enabled_map = {}
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text())
            enabled_map = settings.get("enabledPlugins", {})
        except (OSError, json.JSONDecodeError):
            pass

    for p in installed:
        p["enabled"] = enabled_map.get(p["name"], True)

    return web.json_response(installed)
