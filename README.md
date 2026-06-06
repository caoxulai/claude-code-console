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

### Option A — Install via BuilderToolbox (recommended, Amazon-internal)

The console is vended as a Toolbox tool. On any Cloud Desktop (Amazon Linux x86 or ARM):

```bash
# 1. Add the registry (one time)
toolbox registry add s3://buildertoolbox-registry-claude-code-console-us-west-2/tools.json

# 2. Install the tool
toolbox install claude-web --channel head

# 3. Run it
claude-web
```

`claude-web` starts the server on `http://127.0.0.1:7780` and opens your browser.
The bundle is fully self-contained (bundled Python + frontend + dependencies) — no other prerequisites.

> The tool is currently on the `head` channel. Once promoted to `stable`, drop the `--channel head` flag.

**Commands:**
```bash
claude-web                      # start on :7780 and open the browser
claude-web start --port 8888    # custom port
claude-web start --no-browser   # don't auto-open the browser (e.g. on a remote host)
claude-web setup                # create ~/.claude-web/config.json
```

### Option B — Install from source (for development)

Requires Python ≥ 3.10 and Node.js (to build the frontend).

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

The server binds to `127.0.0.1`. To reach it from a phone or laptop, expose `:7780` through a tunnel from your Cloud Desktop (e.g. AWS Tunnels + AEA), then open the tunnel URL.

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
