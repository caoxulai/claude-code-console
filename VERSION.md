# Claude Code Console — Version History

## v1.0 (2026-06-18)

First stable release. A full-featured web console for managing Claude Code sessions, projects, and automation from a browser.

### Core Platform
- **Dashboard** — at-a-glance stats: active sessions, tasks, projects, usage
- **Multi-project discovery** — auto-detects GitFarm repos in nested Brazil workspaces
- **Command Palette** (Cmd+K) — fuzzy search across pages, sessions, projects, and memory
- **Grouped sidebar navigation** — Overview / Connect / Work / AI Core / Extend / System

### Session & Task Management
- **Sessions browser** — list, search, and inspect Claude Code sessions across projects
- **Task creation** — create tasks with project/global attribution directly from the console
- **Per-session locking** — prevents concurrent writes to the same session

### AI Core
- **Role Agents framework** — per-project agents with persistent context (`.claude/agents/`)
- **Agent Context Panel** — view and manage accumulated agent context per role
- **Skills viewer** — browse available skills and their definitions
- **Memory explorer** — read/search the `.claude/` memory file system
- **Hooks manager** — view and test configured hooks

### Slack Integration
- **In-process scan worker** — pulls new Slack messages every 5 minutes (no external cron needed)
- **LLM draft worker** — auto-drafts replies using Claude
- **Undo window on send** — brief cancel period before Slack messages are posted
- **Seam health monitoring** — surfaces connection/auth issues on the Slack page
- **Draft-vs-final diff preview** — compare LLM draft to the posted version

### Scheduling & Automation
- **Cron job management** — create, list, enable/disable scheduled jobs via the console
- **Run-history sidecar** — execution logs with UTC-correct next-fire estimates
- **OS Crons viewer** — inspect the system crontab from the UI

### Extensibility
- **MCP Servers page** — view connected MCP server status and available tools
- **Plugins page** — browse installed plugins
- **Chat interface** — interactive Claude chat from the console

### Usage & Settings
- **Usage analytics** — track token consumption and session activity
- **Configurable permission mode** — `bypassPermissions` default, changeable via env or config
- **Settings page** — manage console configuration from the UI

### Security & Reliability
- **Loopback-only binding** — refuses non-loopback hosts without explicit `--allow-remote`
- **Path-traversal protection** — hardened project read/write endpoints
- **ETag concurrency control** — prevents lost updates on shared resources
- **Graceful auth failure handling** — bounded retry on Claude auth errors, not raw 403s
- **Input validation** — backend listing endpoints reject malformed input
- **Frontend error guards** — list fetches handle error/non-array responses safely
