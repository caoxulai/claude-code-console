"""Centralised application configuration for claude-web.

All env-var reads happen HERE, at import time, producing a frozen ``cfg``
singleton that the rest of the codebase imports.  This module must import
ONLY from the stdlib + python-dotenv (no server.* imports) to prevent
circular-import hazards.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# ── Load .env ────────────────────────────────────────────────────────────────

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH)

# ── Valid permission modes (mirrors server/cli.py ALLOWED_PERMISSION_MODES) ──

_ALLOWED_PERMISSION_MODES = frozenset({
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
})

_DEFAULT_PERMISSION_MODE = "bypassPermissions"

# ── Project root (the parent of the server/ package) ─────────────────────────

_PROJECT_ROOT = Path(__file__).parent.parent


# ── AppConfig dataclass ──────────────────────────────────────────────────────

@dataclass
class AppConfig:
    """Immutable application configuration derived from env vars at startup."""

    port: int
    default_cwd: Path
    my_email: str
    my_username: str
    workspace_dir: Path
    permission_mode: str
    data_dir: Path
    email_path: Path
    slack_path: Path
    tasks_path: Path
    runs_path: Path
    node24_bin_override: str
    config_path: Path
    allow_remote: bool
    slack_self_mention_ids: tuple[str, ...]
    slack_bot_senders: tuple[str, ...]


# ── Config-file reader (tolerant) ─────────────────────────────────────────────

def _read_config_file(path: Path) -> dict:
    """Load the user config JSON, degrading to {} on any read/parse failure.

    A missing or corrupt config file must never crash startup, so we swallow
    OSError (unreadable) and JSONDecodeError (malformed) and also ignore a
    non-dict top-level document.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_identity_list(env_value: str, file_value, *, lower: bool) -> tuple[str, ...]:
    """Resolve one identity list with env (comma-separated) > file (JSON array) > empty.

    Entries are stripped and empties dropped; non-string file items are ignored.
    ``lower=True`` lowercases each entry (bot senders), else case is preserved
    (self-mention IDs). A blank/whitespace-only env value falls through to the
    file rather than shadowing it with an empty list.
    """
    raw: list = []
    stripped_env = env_value.strip()
    if stripped_env:
        raw = stripped_env.split(",")
    elif isinstance(file_value, list):
        raw = file_value
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        val = item.strip()
        if not val:
            continue
        out.append(val.lower() if lower else val)
    return tuple(out)


# ── Build the singleton ──────────────────────────────────────────────────────

def _build_config() -> AppConfig:
    # port
    port = int(os.environ.get("CLAUDE_WEB_PORT", "9000"))

    # default_cwd
    default_cwd = Path(
        os.environ.get("CLAUDE_WEB_CWD", str(Path.home()))
    ).expanduser().resolve()

    # my_email
    my_email = os.environ.get("CLAUDE_WEB_MY_EMAIL", "").strip().lower()

    # my_username: explicit override, else local-part of my_email
    my_username = os.environ.get("CLAUDE_WEB_MY_USERNAME", "").strip().lower()
    if not my_username and my_email:
        my_username = my_email.split("@")[0].strip().lower()

    # workspace_dir
    workspace_env = os.environ.get("CLAUDE_WEB_WORKSPACE")
    workspace_dir = (
        Path(workspace_env) if workspace_env
        else Path.home() / "workspace" / "projects"
    ).resolve()

    # permission_mode: validate against known set
    raw_mode = os.environ.get("CLAUDE_WEB_PERMISSION_MODE", "")
    permission_mode = raw_mode if raw_mode in _ALLOWED_PERMISSION_MODES else _DEFAULT_PERMISSION_MODE

    # data_dir: project-root-relative default (NOT Path.cwd())
    data_dir_env = os.environ.get("CLAUDE_WEB_DATA_DIR")
    data_dir = Path(data_dir_env).resolve() if data_dir_env else (_PROJECT_ROOT / "data").resolve()

    # email_path
    email_path_env = os.environ.get("CLAUDE_WEB_EMAIL_PATH")
    email_path = Path(email_path_env) if email_path_env else data_dir / "email_threads.json"

    # slack_path
    slack_path_env = os.environ.get("CLAUDE_WEB_SLACK_PATH")
    slack_path = Path(slack_path_env) if slack_path_env else data_dir / "slack_threads.json"

    # tasks_path
    tasks_path_env = os.environ.get("CLAUDE_WEB_TASKS_PATH")
    tasks_path = Path(tasks_path_env) if tasks_path_env else data_dir / "scheduled_tasks.json"

    # runs_path: derives from tasks_path when no explicit env
    runs_path_env = os.environ.get("CLAUDE_WEB_CRON_RUNS_PATH")
    runs_path = Path(runs_path_env) if runs_path_env else tasks_path.parent / "cron_runs.json"

    # node24_bin_override
    node24_bin_override = os.environ.get("CLAUDE_WEB_NODE24_BIN", "")

    # config_path: user config file (default ~/.claude-web/config.json)
    config_path = Path(
        os.environ.get("CLAUDE_WEB_CONFIG", str(Path.home() / ".claude-web" / "config.json"))
    )

    # allow_remote: whether to permit binding to non-loopback hosts
    allow_remote = os.environ.get("CLAUDE_WEB_ALLOW_REMOTE") == "1"

    # Slack personal identity: env (comma-separated) > config-file key (JSON
    # array) > empty. Ships EMPTY so no personal identity lives in the repo; an
    # empty tuple means "never match" for the consumer (self-mention detection
    # disabled), NEVER "match everything".
    _config_data = _read_config_file(config_path)
    slack_self_mention_ids = _resolve_identity_list(
        os.environ.get("CLAUDE_WEB_SELF_MENTION_IDS", ""),
        _config_data.get("slackSelfMentionIds"),
        lower=False,
    )
    slack_bot_senders = _resolve_identity_list(
        os.environ.get("CLAUDE_WEB_BOT_SENDERS", ""),
        _config_data.get("slackBotSenders"),
        lower=True,
    )

    return AppConfig(
        port=port,
        default_cwd=default_cwd,
        my_email=my_email,
        my_username=my_username,
        workspace_dir=workspace_dir,
        permission_mode=permission_mode,
        data_dir=data_dir,
        email_path=email_path,
        slack_path=slack_path,
        tasks_path=tasks_path,
        runs_path=runs_path,
        node24_bin_override=node24_bin_override,
        config_path=config_path,
        allow_remote=allow_remote,
        slack_self_mention_ids=slack_self_mention_ids,
        slack_bot_senders=slack_bot_senders,
    )


cfg = _build_config()
