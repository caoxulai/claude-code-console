"""CLI entry point for claude-web."""
import argparse
import json
import logging
import sys
import webbrowser
from pathlib import Path

from aiohttp import web

from server.config import cfg


CONFIG_PATH = cfg.config_path

# Nothing in this process ever configured logging, so the root logger had no
# handlers and its default WARNING level: every logger.info() the server emits
# (notably create_app's "Frontend dist: ..." line, which tells the operator
# whether dev or packaged assets are live) was silently discarded, and WARNING+
# only escaped via logging's handler of last resort. cmd_start installs a real
# stderr handler once so those lines are actually visible.
_LOGGING_CONFIGURED = False


def _configure_logging() -> None:
    """Install one stderr handler and enable INFO for the `server` package.

    The INFO level is scoped to the `server` logger rather than the root logger
    on purpose: raising the root level would also switch on aiohttp's
    per-request access log, which this console has never printed.
    Idempotent — repeated calls (tests, re-entry) never stack handlers.
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[claude-web] %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger("server").setLevel(logging.INFO)
    _LOGGING_CONFIGURED = True


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# Valid --permission-mode tokens accepted by the installed claude CLI. Passing
# anything outside this set makes the subprocess fail to spawn, so we validate
# resolved values against it and fall back to a safe default rather than crash.
ALLOWED_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
}

DEFAULT_PERMISSION_MODE = "bypassPermissions"


def resolve_permission_mode() -> str:
    """Resolve the console's permission mode from cfg then config file.

    Uses the permission_mode already resolved by cfg (from CLAUDE_WEB_PERMISSION_MODE
    env var). cfg validates the env value against ALLOWED_PERMISSION_MODES and
    falls back to DEFAULT_PERMISSION_MODE if invalid/absent.

    Precedence:
      1. cfg.permission_mode (from env var) — if it is a valid non-default value,
         use it directly. If it is an invalid value (set explicitly to garbage),
         fall back to default (never leak garbage to the CLI).
      2. If cfg is at the default, check the config file's 'permissionMode' key.
      3. Otherwise, return DEFAULT_PERMISSION_MODE.

    A bad config string must never crash the server, so this never raises.
    Pure/importable so it can be unit-tested directly.
    """
    mode = cfg.permission_mode
    # If cfg resolved to a valid non-default value, the env was explicitly set
    # to something good — use it.
    if mode in ALLOWED_PERMISSION_MODES and mode != DEFAULT_PERMISSION_MODE:
        return mode
    # If cfg resolved to the default, the env was either absent or matched the
    # default. Check the config file for a user-chosen override.
    if mode == DEFAULT_PERMISSION_MODE:
        file_mode = load_config().get("permissionMode")
        if file_mode in ALLOWED_PERMISSION_MODES:
            return file_mode
        return DEFAULT_PERMISSION_MODE
    # cfg.permission_mode is an invalid/unknown string (e.g. test-injected
    # garbage) — fall back to the safe default.
    return DEFAULT_PERMISSION_MODE


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


def _is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def cmd_start(args):
    # Safety gate: the server has no authentication and exposes endpoints that
    # read/write ~/.claude and run shell commands (e.g. POST /api/hooks/test).
    # That is acceptable bound to loopback (same trust boundary as the CLI), but
    # binding to a routable interface without auth is unauthenticated RCE on the
    # network. Require an explicit opt-in for any non-loopback host.
    allow_remote = args.allow_remote or cfg.allow_remote
    if not _is_loopback(args.host) and not allow_remote:
        print(
            f"[claude-web] REFUSING to bind to non-loopback host {args.host!r}.\n"
            "  This server has no authentication and can run shell commands, so\n"
            "  exposing it on a network is unauthenticated remote code execution.\n"
            "  To reach it from another device, prefer an SSH tunnel / port-forward\n"
            "  to 127.0.0.1. If you understand the risk and control the network,\n"
            "  re-run with --allow-remote (or CLAUDE_WEB_ALLOW_REMOTE=1).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Before create_app(), so its startup lines (e.g. which frontend dist is
    # actually being served) reach the operator's terminal.
    _configure_logging()

    from server.app import create_app

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    print(f"[claude-web] listening on {url}")
    if not _is_loopback(args.host):
        print(
            f"[claude-web] WARNING: bound to {args.host} — anyone who can reach this "
            "host:port has full, unauthenticated access (including shell execution).",
            file=sys.stderr,
        )

    # Only auto-open a browser for loopback binds (a remote host has no local
    # browser to open, and the URL wouldn't resolve there anyway).
    if not args.no_browser and _is_loopback(args.host):
        webbrowser.open(url)

    web.run_app(app, host=args.host, port=args.port, print=lambda *_: None)


def cmd_setup(args):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    config = load_config()
    if "projectUrls" not in config:
        config["projectUrls"] = {}

    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"[claude-web] config written to {CONFIG_PATH}")
    print("Edit this file to add project URLs:")
    print('  "projectUrls": { "my-project": [{"url": "http://localhost:8080", "label": "Local", "type": "local"}] }')


def main():
    parser = argparse.ArgumentParser(
        prog="claude-web",
        description="Claude Code Console — web UI for managing Claude Code sessions",
    )
    sub = parser.add_subparsers(dest="command")

    # Default: start server (also runs if no subcommand given)
    start_parser = sub.add_parser("start", help="Start the web server")
    start_parser.add_argument("--port", type=int, default=cfg.port)
    start_parser.add_argument("--host", default="127.0.0.1")
    start_parser.add_argument("--no-browser", action="store_true", help="Don't open browser on start")
    start_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permit binding to a non-loopback host. UNSAFE: the server has no auth "
        "and can run shell commands. Prefer an SSH tunnel to 127.0.0.1 instead.",
    )

    # setup
    sub.add_parser("setup", help="Initialize config file")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "start":
        cmd_start(args)
    else:
        # No subcommand — default to start with defaults
        args.port = cfg.port
        args.host = "127.0.0.1"
        args.no_browser = False
        args.allow_remote = False
        cmd_start(args)


if __name__ == "__main__":
    main()
