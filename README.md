# Claude Code Console (`claude-web`)

A web GUI for managing your [Claude Code](https://docs.hub.amazon.dev/claude-code) setup — sessions, memory, skills, projects, MCP servers, hooks, cron jobs, and more — all from a browser, reachable from any device.

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

The server runs locally on `127.0.0.1:9000` by default and serves a pre-built React frontend — no Node.js required at runtime.

---

## Installation

> Looking for a standalone, wiki-ready install guide to share? See
> [`docs/INSTALL.md`](docs/INSTALL.md).

> **TL;DR for consumers** — on an Amazon Linux Cloud Desktop, run:
> ```bash
> mwinit   # required first — or `mwinit -o` if it says "WebAuthn is not supported on this platform"
> bash <(curl -fsSL -b ~/.midway/cookie "https://code.amazon.com/packages/ClaudeCodeConsole/blobs/mainline/--/scripts/install.sh?raw=1")
> claude-web
> ```
> That's the whole thing — the script checks every prerequisite and installs
> what's missing. (`mwinit` is still required first: the curl reads your existing
> Midway cookie, it can't create one.)
>
> **Using it from a Mac?** Start the console on the desktop, then tunnel to it:
> ```bash
> # on the Cloud Desktop (`--no-browser` skips the no-op browser attempt; plain `claude-web` is fine too):
> claude-web start --no-browser
> # on your Mac (new terminal, leave running) — use YOUR desktop's hostname:
> ssh -N -L 9000:127.0.0.1:9000 dev-dsk-<you>-....amazon.com
> ```
> Then open <http://127.0.0.1:9000> in your Mac's browser. (Find your hostname by
> running `hostname -f` on the Cloud Desktop.)
>
> The detailed steps below are only if you want to do it by hand or something
> goes wrong.

> **New here?** Do the **Prerequisites** once, then use **Option A** (one
> command). You do **not** need to check out any code or know anything about how
> the console is built.

### Prerequisites

Do these once on your Cloud Desktop, in order. **For Option A you only need #1
and #2** — the script installs #3 and #4 for you; do them by hand only for
Option B (manual).

1. **An Amazon Cloud Desktop** running Amazon Linux (x86_64 or ARM/aarch64).
   This is where the console runs. (macOS is not yet supported.)
2. **Midway credentials.** Run `mwinit` (you'll need this within the last ~20h).
   - If it says *"WebAuthn is not supported on this platform"*, run `mwinit -o`
     instead: enter your PIN, then touch your security key.
3. **Builder Toolbox** *(auto-installed by Option A)*. For Option B, check with
   `toolbox --version`; if not found, install it from
   <https://builderhub.corp.amazon.com/docs/builder-toolbox/user-guide/getting-started.html>,
   then open a new terminal.
4. **Claude Code** *(auto-installed by Option A)*. The console is a *viewer and
   manager* for the data Claude Code stores in `~/.claude/`. If you've never run
   `claude`, the pages will simply be empty (and the Chat page needs the `claude`
   binary on your `PATH`). For Option B, install the **Amazon internal
   distribution** via Builder Toolbox (`toolbox install claude-code`) — do *not*
   use npm/Homebrew/public installers. See
   <https://docs.hub.amazon.dev/docs/claude-code/user-guide/getting-started.html>.

### Install it — pick one

The tool ships as a fully self-contained bundle (its own Python + the web UI +
all dependencies), so no code checkout is needed for Options A or B.

#### Option A — One command (recommended)

The one-liner downloads and runs a script that does the real work: it checks
every prerequisite, installs anything missing (including Builder Toolbox and
Claude Code), then runs `toolbox install claude-web` for you — which is why you
don't type `toolbox` yourself here.

```bash
bash <(curl -fsSL -b ~/.midway/cookie "https://code.amazon.com/packages/ClaudeCodeConsole/blobs/mainline/--/scripts/install.sh?raw=1")
```

> Both flags are required: `-b ~/.midway/cookie` sends your Midway session
> (without it you get a `401`), and `?raw=1` fetches the raw script instead of
> the HTML Code Browser page. Keep the URL in quotes.

#### Option B — Manual, step by step

Prefer to run each step yourself (or Option A failed)? Run these in order:

```bash
# 1. Register the tool registry (one time, ever)
toolbox registry add s3://buildertoolbox-registry-claude-code-console-us-west-2/tools.json

# 2. Install the tool
toolbox install claude-web
```

#### Option C — From source (for development only)

Use this only if you're modifying the console itself. Requires Python ≥ 3.10,
Node.js, and GitFarm access. (You then run with `.venv/bin/claude-web` instead
of the steps below.)

```bash
# 1. Clone
git clone ssh://git.amazon.com/pkg/ClaudeCodeConsole claude-web
cd claude-web

# 2. Build the frontend (produces frontend/dist/)
cd frontend && npm install && npm run build && cd ..

# 3. Install the package (editable)
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

For frontend hot-reload during development, run the Vite dev server (proxies
`/api` to the backend) alongside the backend:
```bash
.venv/bin/claude-web start --no-browser   # backend on :9000
cd frontend && npm run dev                 # frontend on :9001 (proxies /api to :9000)
```

### Run it

```bash
claude-web
```

The server starts on `http://127.0.0.1:9000` and your browser opens to it. Leave
the terminal running — closing it (or pressing `Ctrl-C`) stops the server.

> **Browsing from a Mac?** The console runs on your Cloud Desktop and binds to
> `127.0.0.1` there, so opening `127.0.0.1:9000` on your Mac won't reach it.
> Instead, on the Cloud Desktop run (`--no-browser` skips the no-op browser
> attempt on the headless host; plain `claude-web` works too):
> ```bash
> claude-web start --no-browser
> ```
> then on your Mac (new terminal, leave it running) forward the port over SSH —
> use your own desktop's hostname (`hostname -f` shows it):
> ```bash
> ssh -N -L 9000:127.0.0.1:9000 dev-dsk-<you>-....amazon.com
> ```
> and open <http://127.0.0.1:9000> in your Mac's browser. More detail and
> options in [Accessing from a Mac (Cloud Desktop → laptop)](#accessing-from-a-mac-cloud-desktop--laptop).

**Other useful commands:**
```bash
claude-web start --port 8888    # use a different port
claude-web start --no-browser   # don't auto-open a browser (headless/remote host)
claude-web setup                # create the optional config file (see Configuration)
```

### Updating

```bash
toolbox update claude-web
# or, if you used the Option A script: bash scripts/install.sh --update
```

> claude-web is published on the `stable` channel, so `toolbox install` /
> `toolbox update` just work. Preview builds go to `head` — add `--channel head`
> to opt in.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `toolbox: command not found` | Builder Toolbox isn't installed — see Prerequisite 3, then open a new terminal. |
| `mwinit`: *"WebAuthn is not supported on this platform"* | Use the OTP/security-key flow instead: `mwinit -o` (enter PIN, then touch your security key). |
| Install `curl: ... 401` | Your Midway session isn't being sent. Run `mwinit` (or `mwinit -o`), and make sure the curl includes `-b ~/.midway/cookie` and the URL ends in `?raw=1`. |
| `AccessDenied` / registry add fails | Run `mwinit` (or `mwinit -o`) and retry. |
| `claude-web: command not found` after install | Open a new terminal so `~/.toolbox/bin` is on your `PATH`, or run `~/.toolbox/bin/claude-web`. |
| Browser doesn't open (remote/headless) | Use `claude-web start --no-browser`, then open `http://127.0.0.1:9000` yourself (tunnel/port-forward if remote — see [Accessing from other devices](#accessing-from-other-devices)). |
| Pages are empty | You haven't used Claude Code yet, or your projects live somewhere other than `~/workspace/projects` — set `CLAUDE_WEB_WORKSPACE` (see [Configuration](#configuration)). |
| Chat page errors | The `claude` binary isn't on your `PATH`. Install Claude Code (Prerequisite 4). |

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
trusted channel: expose `:9000` through a tunnel from your Cloud Desktop (e.g.
AWS Tunnels + AEA) or an SSH port-forward, then open the tunnel URL.

### Accessing from a Mac (Cloud Desktop → laptop)

This is the common setup: the console runs on your Cloud Desktop, but you want
to use it in your Mac's browser. Because the server stays on `127.0.0.1` of the
Cloud Desktop, you reach it with an **SSH port-forward** — a secure tunnel that
maps a port on your Mac to `127.0.0.1:9000` on the desktop. Nothing is exposed
to the network.

1. **On the Cloud Desktop**, start the console. `--no-browser` skips the
   browser-open attempt (which no-ops on a headless host anyway — plain
   `claude-web` works the same for tunneling):
   ```bash
   claude-web start --no-browser
   ```
   Leave this terminal running.

2. **On your Mac**, open a second terminal and forward the port. Use the same
   host you normally SSH to your Cloud Desktop with:
   ```bash
   ssh -N -L 9000:127.0.0.1:9000 <your-cloud-desktop-host>
   ```
   - `<your-cloud-desktop-host>` is whatever you use today, e.g.
     `dev-dsk-$USER-...amazon.com` or an alias from your `~/.ssh/config`.
   - `-N` means "just forward, don't open a shell." Leave it running while you
     use the console; press `Ctrl-C` to disconnect.
   - If port `9000` is already taken on your Mac, map a different local port:
     `-L 9100:127.0.0.1:9000`, then use `:9100` in step 3.

3. **On your Mac**, open <http://127.0.0.1:9000> (or `:9100` if you remapped).
   You're now using the console running on your Cloud Desktop.

> **Tip:** if you connect through Midway/PCSK, make sure your SSH session is
> authenticated (`mwinit`, or `mwinit -o` if WebAuthn isn't supported on your
> platform) before step 2, or the tunnel will fail to establish. The forward
> adds no new auth of its own — anyone who can SSH to your desktop can already
> reach the port.

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
