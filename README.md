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
| **Memory** | Browse and edit `~/.claude` memory files (typed: feedback / user / project / reference) with rendered markdown |
| **Skills** | List installed skills and read their `SKILL.md` content |
| **MCP Servers** | View configured MCP servers |
| **Hooks** | Inspect configured Claude Code hooks |
| **Cron Jobs** | View scheduled jobs |
| **Tasks** | Track tasks |
| **Plugins** | View installed plugins |
| **Settings** | Inspect Claude Code settings |

The server runs locally on `127.0.0.1:7780` by default and serves a pre-built React frontend — no Node.js required at runtime.

---

## Installation

> **TL;DR for consumers** — on an Amazon Linux Cloud Desktop, run:
> ```bash
> mwinit   # if you haven't authenticated today
> bash <(curl -fsSL -b ~/.midway/cookie "https://code.amazon.com/packages/ClaudeCodeConsole/blobs/mainline/--/scripts/install.sh?raw=1")
> claude-web
> ```
> That's the whole thing — the script checks every prerequisite and installs
> what's missing. The detailed steps below are only if you want to do it by hand
> or something goes wrong.

> **New here?** Follow Option A. It's the fastest path and needs nothing but a
> Cloud Desktop. You do **not** need to check out any code or know anything
> about how the console is built.

### Prerequisites

1. **An Amazon Cloud Desktop** running Amazon Linux (x86_64 or ARM/aarch64).
   This is where the console runs. (macOS is not yet supported.)
2. **Midway credentials.** Run `mwinit` if you haven't authenticated today —
   `toolbox` needs it to download the tool.
3. **Builder Toolbox installed.** Check with `toolbox --version`. If the command
   is not found, install it from
   <https://builderhub.corp.amazon.com/docs/builder-toolbox/user-guide/getting-started.html>,
   then open a new terminal.
4. **Claude Code installed and used at least once.** The console is a *viewer and
   manager* for the data Claude Code stores in `~/.claude/`. If you've never run
   `claude`, the pages will simply be empty (and the Chat page needs the `claude`
   binary on your `PATH`). Install Claude Code first:
   <https://docs.claude.com/en/docs/claude-code>.

### Option A — One-command install (recommended)

The install script checks all prerequisites (Midway, Builder Toolbox, Claude
Code) and installs anything missing, then installs claude-web itself:

```bash
bash <(curl -fsSL -b ~/.midway/cookie "https://code.amazon.com/packages/ClaudeCodeConsole/blobs/mainline/--/scripts/install.sh?raw=1")
```

> The `-b ~/.midway/cookie` sends your Midway session (run `mwinit` first, or
> you'll get a `401`), and `?raw=1` fetches the raw script instead of the HTML
> Code Browser page. Both are required.

Or if you already have the repo cloned:
```bash
bash scripts/install.sh
```

**Updating later:**
```bash
bash scripts/install.sh --update
# or simply:
toolbox update claude-web
```

### Option A (manual) — Install via Builder Toolbox step by step

No code checkout required — the tool ships as a fully self-contained bundle
(its own Python + the web UI + all dependencies).

```bash
# 1. Authenticate with Midway (skip if you've already run it today)
mwinit

# 2. Register the tool's registry (one time, ever)
toolbox registry add s3://buildertoolbox-registry-claude-code-console-us-west-2/tools.json

# 3. Install the tool
toolbox install claude-web

# 4. Start the console
claude-web
```

**What happens on step 4:** the server starts on `http://127.0.0.1:7780` and your
browser opens to it. Leave the terminal running — closing it stops the server.
To stop, press `Ctrl-C` in that terminal.

> **Browsing from a Mac?** The console runs on your Cloud Desktop and binds to
> `127.0.0.1` there, so opening `127.0.0.1:7780` on your Mac won't reach it. Run
> the console with `claude-web start --no-browser`, then forward the port over
> SSH from your Mac and open the link locally — see
> [Accessing from a Mac (Cloud Desktop → laptop)](#accessing-from-a-mac-cloud-desktop--laptop).

> The tool is published on the `stable` channel, so `toolbox install claude-web`
> just works. (Early/preview builds go to `head` — add `--channel head` to opt in.)

**Updating later:**
```bash
toolbox update claude-web
```

**Other commands:**
```bash
claude-web                      # start on :7780 and open the browser
claude-web start --port 8888    # use a different port
claude-web start --no-browser   # don't auto-open a browser (e.g. on a headless/remote host)
claude-web setup                # create the optional config file (see Configuration)
```

#### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `toolbox: command not found` | Builder Toolbox isn't installed — see Prerequisite 3, then open a new terminal. |
| `AccessDenied` / registry add fails | Run `mwinit` and retry. |
| `claude-web: command not found` after install | Open a new terminal so `~/.toolbox/bin` is on your `PATH`, or run `~/.toolbox/bin/claude-web`. |
| Browser doesn't open (remote/headless) | Use `claude-web start --no-browser`, then open `http://127.0.0.1:7780` yourself (tunnel/port-forward if remote — see [Accessing from other devices](#accessing-from-other-devices)). |
| Pages are empty | You haven't used Claude Code yet, or your projects live somewhere other than `~/workspace/projects` — set `CLAUDE_WEB_WORKSPACE` (see [Configuration](#configuration)). |
| Chat page errors | The `claude` binary isn't on your `PATH`. Install Claude Code (Prerequisite 4). |

### Option B — Install from source (for development)

Use this only if you're modifying the console itself. Requires Python ≥ 3.10,
Node.js, and GitFarm access.

```bash
# 1. Clone
git clone ssh://git.amazon.com/pkg/ClaudeCodeConsole claude-web
cd claude-web

# 2. Build the frontend (produces frontend/dist/)
cd frontend && npm install && npm run build && cd ..

# 3. Install the package (editable)
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# 4. Run
.venv/bin/claude-web start --port 7780
```

Open <http://127.0.0.1:7780>.

For frontend hot-reload during development, run the Vite dev server (proxies `/api` to the backend):
```bash
.venv/bin/claude-web start --no-browser   # backend on :7780
cd frontend && npm run dev                 # frontend on :5173
```

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

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_WEB_PORT` | `7780` | Server port |
| `CLAUDE_WEB_WORKSPACE` | `~/workspace/projects` | Where to look for projects (symlink-resolved) |
| `CLAUDE_WEB_CONFIG` | `~/.claude-web/config.json` | Config file location |
| `CLAUDE_WEB_CWD` | `$HOME` | Default working directory for chat |

---

## Accessing from other devices

The server binds to `127.0.0.1` and has **no authentication** — it can read/write
your `~/.claude` files and run shell commands. The recommended way to reach it
from a phone or laptop is to keep it on loopback and forward the port over a
trusted channel: expose `:7780` through a tunnel from your Cloud Desktop (e.g.
AWS Tunnels + AEA) or an SSH port-forward, then open the tunnel URL.

### Accessing from a Mac (Cloud Desktop → laptop)

This is the common setup: the console runs on your Cloud Desktop, but you want
to use it in your Mac's browser. Because the server stays on `127.0.0.1` of the
Cloud Desktop, you reach it with an **SSH port-forward** — a secure tunnel that
maps a port on your Mac to `127.0.0.1:7780` on the desktop. Nothing is exposed
to the network.

1. **On the Cloud Desktop**, start the console without trying to open a browser
   there:
   ```bash
   claude-web start --no-browser
   ```
   Leave this terminal running.

2. **On your Mac**, open a second terminal and forward the port. Use the same
   host you normally SSH to your Cloud Desktop with:
   ```bash
   ssh -N -L 7780:127.0.0.1:7780 <your-cloud-desktop-host>
   ```
   - `<your-cloud-desktop-host>` is whatever you use today, e.g.
     `dev-dsk-$USER-...amazon.com` or an alias from your `~/.ssh/config`.
   - `-N` means "just forward, don't open a shell." Leave it running while you
     use the console; press `Ctrl-C` to disconnect.
   - If port `7780` is already taken on your Mac, map a different local port:
     `-L 9000:127.0.0.1:7780`, then use `:9000` in step 3.

3. **On your Mac**, open <http://127.0.0.1:7780> (or `:9000` if you remapped).
   You're now using the console running on your Cloud Desktop.

> **Tip:** if you connect through Midway/PCSK, make sure your SSH session is
> authenticated (`mwinit` / `mwinit -s`) before step 2, or the tunnel will fail
> to establish. The forward adds no new auth of its own — anyone who can SSH to
> your desktop can already reach the port.

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

The package builds in Brazil as `ClaudeCodeConsole` (a `custom-build` hybrid that runs the Vite build then `brazilpython`). See [`docs/TOOLBOX_VENDING.md`](docs/TOOLBOX_VENDING.md) for the full build + vending runbook.

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
