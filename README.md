# Claude Code Console (`claude-web`)

A web GUI for managing your [Claude Code](https://docs.claude.com/en/docs/claude-code) setup — sessions, memory, skills, projects, MCP servers, hooks, cron jobs, and more — all from a browser, reachable from any device.

Claude Code stores everything on disk under `~/.claude/` (session transcripts, memory files, skills, settings). This console reads and manages that data through a clean web interface instead of the CLI and a text editor.

---

## What it does

A sidebar-navigated single-page app backed by a small aiohttp server. Each page maps to a slice of your Claude Code environment:

| Page | What it shows |
|------|---------------|
| **Dashboard** | At-a-glance counts (sessions, live sessions, memory files, skills, MCP servers, cron jobs, projects) + recent projects with quick links |
| **Projects** | Every project in your workspace with its session/memory counts, CLAUDE.md, memory files, and SOP/skill docs — plus configurable live/deployed app URLs |
| **Sessions** | All Claude Code sessions across projects — browse, view transcripts, rename, resume, or delete |
| **Usage** | Token/cost analytics across all transcripts — totals, daily trend, a GitHub-style activity heatmap with week drill-down, and breakdowns by model, project, and agent (all day boundaries in Pacific time) |
| **Chat** | Send a prompt to `claude` and stream the response (SSE), with per-request working-directory selection |
| **Slack** | Triage queue for unread Slack messages — auto-classifies and drafts replies, lets you edit/approve, and posts the approved reply (background scan/classify/draft workers) |
| **Email** | Triage queue for unread Outlook email — auto-classifies and drafts reply-all responses you can edit/approve; approving saves an Outlook **draft** (never sends), with the same background workers as Slack |
| **Memory** | Browse and edit `~/.claude` memory files (typed: feedback / user / project / reference) with rendered markdown |
| **Agents** | Browse global and per-project role agents (`.claude/agents/*.md`) and their accumulated context |
| **Skills** | List installed skills and read their `SKILL.md` content |
| **MCP Servers** | View configured MCP servers |
| **Hooks** | Inspect configured Claude Code hooks |
| **Cron Jobs** | View scheduled jobs |
| **Workers** | Live status of background workers (scan/classify/draft, session reapers) — running / stopped / errored, with last-run times |
| **Tasks** | Track tasks |
| **Plugins** | View installed plugins |
| **Settings** | Inspect Claude Code settings |

The server runs locally on `127.0.0.1:9000` by default and serves a pre-built React frontend — no Node.js required at runtime.

---

## Installation

### Prerequisites

1. **Python ≥ 3.10** and **Node.js** (for building the frontend).
2. **[Claude Code](https://docs.claude.com/en/docs/claude-code)** installed and used at least once. The console is a *viewer and manager* for the data Claude Code stores in `~/.claude/`; if you've never run `claude`, the pages will simply be empty (and the Chat page needs the `claude` binary on your `PATH`).

### Install from source

```bash
# 1. Clone
git clone https://github.com/caoxulai/claude-code-console.git claude-web
cd claude-web

# 2. Build the frontend (produces frontend/dist/, then copies it to server/static/)
cd frontend && npm install && npm run build && cd ..

# 3. Install the package
python3 -m venv .venv
.venv/bin/pip install -e .
```

Step 2 is what ships the UI: `npm run build` writes `frontend/dist/`, and its
`postbuild` hook (`scripts/copy-dist.mjs`) copies that into `server/static/`,
which `pip install` picks up as package data. Build before installing, or the
installed server has no UI to serve.

For frontend hot-reload during development, run the Vite dev server (proxies
`/api` to the backend) alongside the backend:
```bash
.venv/bin/claude-web start --no-browser   # backend on :9000
cd frontend && npm run dev                 # frontend on :9001 (proxies /api to :9000)
```

### Run it

```bash
.venv/bin/claude-web
```

The server starts on `http://127.0.0.1:9000` and your browser opens to it. Leave
the terminal running — closing it (or pressing `Ctrl-C`) stops the server.

**Other useful commands:**
```bash
claude-web start --port 8888    # use a different port
claude-web start --no-browser   # don't auto-open a browser (headless/remote host)
claude-web setup                # create the optional config file (see Configuration)
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `claude-web: command not found` | Activate the venv (`source .venv/bin/activate`) or run `.venv/bin/claude-web`. |
| Browser doesn't open (remote/headless) | Use `claude-web start --no-browser`, then open `http://127.0.0.1:9000` yourself (tunnel/port-forward if remote — see [Accessing from other devices](#accessing-from-other-devices)). |
| Pages are empty | You haven't used Claude Code yet, or your projects live somewhere other than `~/workspace/projects` — set `CLAUDE_WEB_WORKSPACE` (see [Configuration](#configuration)). |
| Chat page errors | The `claude` binary isn't on your `PATH`. Install Claude Code (Prerequisite 2). |

---

## Configuration

Optional config at `~/.claude-web/config.json` (or set `CLAUDE_WEB_CONFIG` to point elsewhere). Run `claude-web setup` to scaffold it.

```json
{
  "projectUrls": {
    "my-project": [
      { "url": "http://127.0.0.1:8080", "label": "Local", "type": "local" },
      { "url": "https://my-app.example.com", "label": "Deployed", "type": "deployed" }
    ]
  }
}
```

`projectUrls` keys are matched as substrings against project names; matched projects show clickable URL pills on the Dashboard and Projects pages.

### Permission mode (no per-edit approval prompt)

The web console has no interactive approval channel, so console-driven sessions
run without the per-edit confirmation prompt you'd see in the CLI — Claude
executes tool calls, including file edits and writes, without asking. The mode
is configurable via the `permissionMode` config key or the
`CLAUDE_WEB_PERMISSION_MODE` environment variable (the env var takes
precedence):

```json
{
  "permissionMode": "bypassPermissions"
}
```

Allowed values are `acceptEdits`, `auto`, `bypassPermissions`, `default`,
`dontAsk`, and `plan`. The default is **`bypassPermissions`**; a missing or
unrecognized value falls back to `bypassPermissions` rather than being passed
through to the CLI.

> **Safety caveat:** with the default `bypassPermissions`, the console makes
> changes to your filesystem (and runs commands) without prompting. Only run
> goals you trust, and prefer a more restrictive mode (e.g. `plan` or
> `acceptEdits`) if you want a tighter blast radius.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_WEB_PORT` | `9000` | Server port |
| `CLAUDE_WEB_WORKSPACE` | `~/workspace/projects` | Where to look for projects (symlink-resolved) |
| `CLAUDE_WEB_CONFIG` | `~/.claude-web/config.json` | Config file location |
| `CLAUDE_WEB_CWD` | `$HOME` | Default working directory for chat |
| `CLAUDE_WEB_PERMISSION_MODE` | `bypassPermissions` | Tool-call approval mode for console runs (see [Permission mode](#permission-mode-no-per-edit-approval-prompt)) |

---

## Accessing from other devices

The server binds to `127.0.0.1` and has **no authentication** — it can read/write
your `~/.claude` files and run shell commands. The recommended way to reach it
from a phone or laptop is to keep it on loopback and forward the port over a
trusted channel: an SSH port-forward from the remote host running the console,
then open the forwarded URL.

### Accessing from a laptop (remote host → laptop)

This is the common setup: the console runs on a remote host (e.g. a dev server),
but you want to use it in your laptop's browser. Because the server stays on
`127.0.0.1` of the remote host, you reach it with an **SSH port-forward** — a
secure tunnel that maps a port on your laptop to `127.0.0.1:9000` on the host.
Nothing is exposed to the network.

1. **On the remote host**, start the console. `--no-browser` skips the
   browser-open attempt (which no-ops on a headless host anyway — plain
   `claude-web` works the same for tunneling):
   ```bash
   claude-web start --no-browser
   ```
   Leave this terminal running.

2. **On your laptop**, open a second terminal and forward the port. Use the same
   host you normally SSH to:
   ```bash
   ssh -N -L 9000:127.0.0.1:9000 <your-remote-host>
   ```
   - `<your-remote-host>` is whatever you use today, e.g. a hostname or an alias
     from your `~/.ssh/config`.
   - `-N` means "just forward, don't open a shell." Leave it running while you
     use the console; press `Ctrl-C` to disconnect.
   - If port `9000` is already taken on your laptop, map a different local port:
     `-L 9100:127.0.0.1:9000`, then use `:9100` in step 3.

3. **On your laptop**, open <http://127.0.0.1:9000> (or `:9100` if you remapped).
   You're now using the console running on the remote host.

> **Tip:** the forward adds no new auth of its own — anyone who can SSH to your
> host can already reach the port. Make sure your SSH session is established
> before step 2.

### Binding to a routable interface (not recommended)

Binding directly to a routable interface (`--host 0.0.0.0`) is refused by
default, because an unauthenticated server that can execute shell commands is
remote code execution for anyone who can reach the port. If you genuinely
control the network and accept that risk, pass `--allow-remote` (or set
`CLAUDE_WEB_ALLOW_REMOTE=1`); the server then prints a warning and binds anyway.

---

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # run tests
cd frontend && npm run build             # rebuild the UI
```

The build is a two-step hybrid: `npm run build` produces the static frontend
under `frontend/dist/`, then its `postbuild` hook runs `scripts/copy-dist.mjs`,
which wipes and repopulates `server/static/` from it. `server/static/` is
gitignored (build output) but shipped as package data, so the installed server
serves the UI at runtime with no Node present. In dev mode the server prefers
`frontend/dist/` and logs which directory it is serving at startup.

Packaging metadata lives entirely in `pyproject.toml` — version, dependencies,
the `claude-web` console script, and package data. There is no `setup.py`;
bump the version in `pyproject.toml` only.

### One-command check gate

Before opening a change, run the repo check gate:

```bash
bash scripts/check.sh
```

It runs, in order, and exits non-zero on the first failure:

1. **Backend tests** — `.venv/bin/python -m pytest tests/ -q` *(fatal)*
2. **Frontend tests** — `cd frontend && npm test` *(fatal)*
3. **Frontend build** — `cd frontend && npm run build` *(fatal)*
4. **Lint** — `npx eslint .` *(non-fatal, reported only)*

The lint step is intentionally **non-fatal**: the repo carries ~50 pre-existing
lint errors scheduled for a later clean-up batch, so its result is printed but
does not fail the gate. Once that batch lands, an in-script comment marks
exactly where to flip lint to fatal. The script resolves the repo root from its
own location, so it works from any working directory.

## Project layout

```
server/                 aiohttp backend
  app.py                application factory + SPA serving
  cli.py                claude-web CLI (start / setup)
  routes/               one module per API area (sessions, memory, skills, …)
frontend/               React + Vite single-page app
  src/pages/            one component per console page
tests/                  pytest (server + routes)
```
