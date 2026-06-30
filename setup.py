"""Setuptools build for the Claude Code Console.

The console-script entry point (`claude-web`) is declared here. The built
frontend (copied to server/static during the build) is shipped as package data
so the server can serve it at runtime with no Node present.
"""
import os

from setuptools import find_packages, setup


def _static_data_files():
    """Walk server/static and return data_files preserving directory structure."""
    data_files = []
    static_root = os.path.join("server", "static")
    if not os.path.isdir(static_root):
        return data_files
    for root, _dirs, files in os.walk(static_root):
        if not files:
            continue
        rel = os.path.relpath(root, "server")
        data_files.append((rel, [os.path.join(root, f) for f in files]))
    return data_files


setup(
    name="claude-web",
    version="0.2.0",
    description="Web UI for managing Claude Code sessions, memory, skills, and projects",
    packages=find_packages(include=["server", "server.*"]),
    include_package_data=True,
    package_data={"server": ["static/**/*"]},
    data_files=_static_data_files(),
    install_requires=[
        "aiohttp>=3.9",
        "python-dotenv>=1.0",
        "croniter>=2.0",
        "mcp>=1.27,<2",
    ],
    entry_points={
        "console_scripts": [
            "claude-web = server.cli:main",
        ],
    },
)
