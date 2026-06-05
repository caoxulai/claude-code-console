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


def cmd_start(args):
    from server.app import create_app

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    print(f"[claude-web] listening on {url}")

    if not args.no_browser:
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
        cmd_start(args)


if __name__ == "__main__":
    main()
