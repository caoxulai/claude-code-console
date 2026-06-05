"""Entry point for the Claude Code Console server."""
import os
import sys

from aiohttp import web

# Ensure the project root is importable
sys.path.insert(0, os.path.dirname(__file__))

from server.app import create_app


def main():
    port = int(os.environ.get("CLAUDE_WEB_PORT", "7780"))
    app = create_app()
    print(f"[claude-web] listening on 127.0.0.1:{port}")
    web.run_app(app, host="127.0.0.1", port=port, print=lambda *_: None)


if __name__ == "__main__":
    main()
