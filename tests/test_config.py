"""Config resolution tests — Slack identity (D-077, spec item 4, resolution side).

`server.config._build_config()` is a PURE builder that reads env vars and the
config file at the CLAUDE_WEB_CONFIG path. These tests drive it directly: set
env vars + a tmp config.json via monkeypatch and assert the resolved
``slack_self_mention_ids`` / ``slack_bot_senders`` tuples.

INTERFACE CONTRACT (shared with the slack.py consumer, task T1):
  * ``slack_self_mention_ids`` — tuple of stripped non-empty strings, CASE PRESERVED.
  * ``slack_bot_senders``      — tuple of stripped non-empty LOWERCASED strings.
  * both default to empty tuples (NO personal identity remains in repo code).

Precedence per field: env var (comma-separated) > config-file key (JSON array) > empty.

These tests NEVER touch the real ~/.claude-web/config.json — CLAUDE_WEB_CONFIG is
always repointed at a tmp path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.config import _build_config  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch, tmp_path):
    """Isolate every env var _build_config reads for identity resolution.

    CLAUDE_WEB_SECRET is required (else _build_config raises); the two identity
    vars and CLAUDE_WEB_CONFIG start CLEARED so each test opts in explicitly and
    the real user config is never read.
    """
    monkeypatch.setenv("CLAUDE_WEB_SECRET", "test-secret")
    monkeypatch.delenv("CLAUDE_WEB_SELF_MENTION_IDS", raising=False)
    monkeypatch.delenv("CLAUDE_WEB_BOT_SENDERS", raising=False)
    # Point config at a tmp path that does NOT exist by default (absent file).
    monkeypatch.setenv("CLAUDE_WEB_CONFIG", str(tmp_path / "config.json"))


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


# ─── defaults / absent ────────────────────────────────────────────────────────

def test_identity_defaults_empty_when_env_and_file_absent():
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ()
    assert cfg.slack_bot_senders == ()


def test_identity_empty_when_config_file_present_but_lacks_keys(tmp_path):
    _write_config(tmp_path, {"projectUrls": {"x": []}})
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ()
    assert cfg.slack_bot_senders == ()


# ─── file-only ──────────────────────────────────────────────────────────────

def test_identity_from_config_file(tmp_path):
    _write_config(
        tmp_path,
        {
            "projectUrls": {},
            "slackSelfMentionIds": ["W0187CHBRU0", "U06PZ036D98"],
            "slackBotSenders": ["Slackbot", "Amazon Meetings"],
        },
    )
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ("W0187CHBRU0", "U06PZ036D98")
    # bot senders lowercased
    assert cfg.slack_bot_senders == ("slackbot", "amazon meetings")


def test_identity_config_non_list_values_tolerated(tmp_path):
    # A malformed non-list value must degrade to empty, never crash.
    _write_config(
        tmp_path,
        {"slackSelfMentionIds": "not-a-list", "slackBotSenders": 42},
    )
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ()
    assert cfg.slack_bot_senders == ()


# ─── env wins over file ───────────────────────────────────────────────────────

def test_identity_env_wins_over_file(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        {
            "slackSelfMentionIds": ["FILEID1"],
            "slackBotSenders": ["file bot"],
        },
    )
    monkeypatch.setenv("CLAUDE_WEB_SELF_MENTION_IDS", "ENVID1,ENVID2")
    monkeypatch.setenv("CLAUDE_WEB_BOT_SENDERS", "Env Bot,Other Bot")
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ("ENVID1", "ENVID2")
    assert cfg.slack_bot_senders == ("env bot", "other bot")


def test_identity_blank_env_falls_through_to_file(tmp_path, monkeypatch):
    # A whitespace-only env value must NOT shadow a real file value (mirrors the
    # my_username env>derived pattern where empty env falls through).
    _write_config(tmp_path, {"slackSelfMentionIds": ["FILEID1"]})
    monkeypatch.setenv("CLAUDE_WEB_SELF_MENTION_IDS", "   ")
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ("FILEID1",)


# ─── corrupt file ─────────────────────────────────────────────────────────────

def test_identity_corrupt_file_yields_empty_not_crash(tmp_path):
    (tmp_path / "config.json").write_text("{ this is not json ")
    cfg = _build_config()  # must not raise
    assert cfg.slack_self_mention_ids == ()
    assert cfg.slack_bot_senders == ()


# ─── whitespace / case normalization ─────────────────────────────────────────

def test_identity_env_whitespace_and_empty_entries_stripped(monkeypatch):
    monkeypatch.setenv("CLAUDE_WEB_SELF_MENTION_IDS", "  AbC123 , U06PZ036D98 ,, ")
    monkeypatch.setenv("CLAUDE_WEB_BOT_SENDERS", "  SlackBot , Amazon Meetings ,, ")
    cfg = _build_config()
    # self-mention: whitespace stripped, empty entries dropped, CASE PRESERVED
    assert cfg.slack_self_mention_ids == ("AbC123", "U06PZ036D98")
    # bot senders: whitespace stripped, empty entries dropped, LOWERCASED
    assert cfg.slack_bot_senders == ("slackbot", "amazon meetings")


def test_identity_file_whitespace_and_empty_entries_stripped(tmp_path):
    _write_config(
        tmp_path,
        {
            "slackSelfMentionIds": ["  W1 ", "", "  ", "W2"],
            "slackBotSenders": ["  CRUX ", "", "Synerq AI"],
        },
    )
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ("W1", "W2")
    assert cfg.slack_bot_senders == ("crux", "synerq ai")


def test_identity_file_non_string_items_dropped(tmp_path):
    _write_config(
        tmp_path,
        {"slackSelfMentionIds": ["W1", 5, None, "W2"], "slackBotSenders": ["bot", 1]},
    )
    cfg = _build_config()
    assert cfg.slack_self_mention_ids == ("W1", "W2")
    assert cfg.slack_bot_senders == ("bot",)
