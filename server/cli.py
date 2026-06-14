"""CLI entry point for claude-web."""
import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from aiohttp import web


CONFIG_PATH = Path(os.environ.get("CLAUDE_WEB_CONFIG", Path.home() / ".claude-web" / "config.json"))


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
    """Resolve the console's permission mode from env then config.

    Read order: CLAUDE_WEB_PERMISSION_MODE env var first, then the
    'permissionMode' key in config.json (~/.claude-web/config.json). A missing
    or invalid value falls back to 'bypassPermissions' — a bad config string
    must never crash the server, so this never raises. Pure/importable so it
    can be unit-tested directly.
    """
    mode = os.environ.get("CLAUDE_WEB_PERMISSION_MODE") or load_config().get("permissionMode")
    if mode in ALLOWED_PERMISSION_MODES:
        return mode
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
    allow_remote = args.allow_remote or os.environ.get("CLAUDE_WEB_ALLOW_REMOTE") == "1"
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
    start_parser.add_argument("--port", type=int, default=int(os.environ.get("CLAUDE_WEB_PORT", "7780")))
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
        args.port = int(os.environ.get("CLAUDE_WEB_PORT", "7780"))
        args.host = "127.0.0.1"
        args.no_browser = False
        args.allow_remote = False
        cmd_start(args)


if __name__ == "__main__":
    main()
