# claude-web

Single-user web UI for Claude Code. Lets you send prompts to `claude --print` from any device (phone, laptop) via AWS Tunnels + AEA on a Cloud Desktop.

## What it is

- Tiny aiohttp app on `127.0.0.1:7780`
- One chat page, SSE streaming
- Auth: open `/auth?secret=<value>` once per device → HMAC-signed cookie → 30-day sticky session
- Each chat send spawns `claude --print --output-format stream-json --permission-mode default` and streams JSON events back

This is intentionally separate from MeshClaw. It does not depend on it and does not modify it.

## Setup

```bash
cd $HOME/workspace/projects/claude-web
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python server.py        # generates .env on first run if missing
```

In another tmux pane:

```bash
tunnel create 7780 --name claude-web
# → https://<alias>-claude-web.w.tunnels.<alias>.people.aws.dev
```

Once on each device, open:

```
https://<alias>-claude-web.w.tunnels.<alias>.people.aws.dev/auth?secret=<CLAUDE_WEB_SECRET>
```

The cookie sticks for 30 days. After that, just `/auth?secret=…` once more.

## Security posture

- The tunnel gates the URL with Midway via AEA; AEA only opens it on your devices.
- The app cookie HMAC + secret check ensures only someone with the one-time secret can ever pair a device.
- `claude --print` is invoked with `--permission-mode default`, so any tool use (write file, push commit, AWS call) prompts for permission instead of running silently — even though your global `~/.claude/settings.json` is `bypassPermissions`. **Do not change that flag in this server**; it's the load-bearing safety boundary for a remote-typed prompt.

## Files

- `server.py` — aiohttp app, ~150 lines
- `static/index.html` `static/app.js` `static/style.css` — chat UI
- `tests/test_server.py` — pytest

## Verification

See `~/.claude/plans/clever-waddling-mountain.md` for the full verification checklist.
