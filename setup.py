"""Setuptools build for the Claude Code Console Brazil package.

The console-script entry point (`claude-web`) is declared here so BrazilPython
auto-generates the `bin/claude-web` wrapper in the runtime farm. The built
frontend (copied to server/static by build-tools/bin/custom-build) is shipped
as package data so the server can serve it at runtime with no Node present.

This package self-publishes to BuilderToolbox via:
    brazil-build toolbox_bundler -o alinux \
        --publish -a <account> -b claude-web -c head
(see ToolboxBundlerCommand below, modeled on AlSwMCP).
"""
import os
import shutil
import subprocess

from setuptools import Command, find_packages, setup

# Brazil Python only reliably supports Amazon Linux platforms — many native
# Python deps lack Brazil builds for macOS. Cloud Desktops are Amazon Linux.
TOOLBOX_SUPPORTED_OS = ["alinux", "alinux_aarch64"]


def _run(command):
    """Run a command and return its decoded, stripped stdout."""
    return subprocess.check_output(command).decode().strip()


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


class ToolboxBundlerCommand(Command):
    description = "Bundle (and optionally publish) the tool for BuilderToolbox"
    user_options = [
        ("toolbox-os=", "o", "Toolbox supported operating system."),
        ("publish", "p", "Publish to repository. Default is false."),
        ("repository-account=", "a", "AWS account hosting the tool repository."),
        ("repository-role=", "r", "Repository account role name."),
        ("repository-name=", "b", "Repository name (bucket suffix)."),
        ("channel=", "c", "Repository channel name (head/stable)."),
    ]

    def initialize_options(self):
        self.toolbox_os = None
        self.publish = False
        self.repository_account = None
        self.repository_role = "toolbox-publish-role"
        self.repository_name = None
        self.channel = None

    def finalize_options(self):
        if self.toolbox_os is None:
            raise ValueError("--toolbox-os is required")
        if self.toolbox_os not in TOOLBOX_SUPPORTED_OS:
            raise ValueError(f"--toolbox-os must be one of: {TOOLBOX_SUPPORTED_OS}")
        if self.publish:
            for opt in ("repository_account", "repository_role", "repository_name", "channel"):
                if getattr(self, opt) is None:
                    raise ValueError(f"--{opt.replace('_', '-')} is required when publishing")

    def run(self):
        bundler_farm = _run(["brazil-path", "[BuilderToolboxBundler]pkg.runtimefarm"])
        bundler_bin = f"{bundler_farm}/bin"
        bundler_cmd = f"{bundler_bin}/toolbox-bundler"
        publisher_cmd = f"{bundler_bin}/toolbox-publisher"

        runtime_farm = _run(["brazil-bootstrap", "--farmType", "copy"])

        try:
            import pkg_resources
            version = pkg_resources.require("claude-web")[0].version
        except Exception:
            version = "0.1.0"

        output_dir = "./build/private/tool-bundle"
        shutil.rmtree(output_dir, ignore_errors=True)

        bundle_output_dir = _run([
            bundler_cmd,
            "--root", runtime_farm,
            "--os", self.toolbox_os,
            "--tool-version", version,
            "--metadata", "./configuration/toolbox/metadata.json",
            "--output-dir", output_dir,
            "--verbose",
        ])

        if self.publish:
            if self.channel == "stable":
                if _run(["git", "status", "--porcelain"]) != "":
                    raise RuntimeError("Can't publish to stable with pending changes.")
                if _run(["git", "branch", "-r", "--contains", "HEAD"]) == "":
                    raise RuntimeError("Commit must be merged to a remote branch before publishing to stable.")

            _run([
                "ada", "credentials", "update",
                "--account", self.repository_account,
                "--role", self.repository_role,
                "--once",
            ])

            _run([
                publisher_cmd,
                "--source", f"{bundle_output_dir}/{version}",
                "--publish-to", f"s3://buildertoolbox-{self.repository_name}-us-west-2",
                "--channel", self.channel,
                "--make-current",
                "--verbose",
            ])


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
    ],
    entry_points={
        "console_scripts": [
            "claude-web = server.cli:main",
        ],
    },
    # Land the console script under $ENVROOT/bin using the default interpreter.
    root_script_source_version="default-only",
    cmdclass={
        "toolbox_bundler": ToolboxBundlerCommand,
    },
)
