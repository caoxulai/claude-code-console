"""GET /api/usage — aggregate Claude Code token usage across all transcripts.

Every assistant turn Claude Code writes carries a `message.usage` block
(input/output/cache tokens) and a `message.model`. Main-thread turns live in
~/.claude/projects/<slug>/*.jsonl; subagent turns live under subagents/ and are
marked `isSidechain: true` with an `attributionAgent`. The same logical record
can be written to more than one file (e.g. a transcripts/ export), so we dedupe
globally by the record `uuid` and count each unique turn exactly once.

This endpoint sums every unique usage record and returns totals plus breakdowns
by model, project (from each record's cwd), agent (main vs each subagent), and
day, with estimated cost.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from aiohttp import web


CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"

# Per-model price per 1M tokens: (input, output). Cache reads bill at ~0.1x the
# input rate and cache writes (5-minute TTL) at ~1.25x — the standard Claude
# pricing multipliers. Unknown/unlisted models fall back to Opus-tier pricing
# so cost is over- rather than under-estimated.
_PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25


def register(app: web.Application):
    app.router.add_get("/api/usage", get_usage)


def _price_for(model: str) -> tuple[float, float]:
    return _PRICING.get(model, _DEFAULT_PRICE)


def _cost_for(model: str, inp: int, out: int, cache_read: int, cache_write: int) -> float:
    """Estimated USD cost for one usage record.

    Cache reads bill at 0.1x and cache writes at 1.25x the model's input rate;
    output bills at the model's output rate.
    """
    pin, pout = _price_for(model)
    return (
        inp * pin
        + out * pout
        + cache_read * pin * _CACHE_READ_MULT
        + cache_write * pin * _CACHE_WRITE_MULT
    ) / 1e6


def _empty_bucket() -> dict:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "messages": 0,
        "cost": 0.0,
    }


def _add(bucket: dict, model: str, inp: int, out: int, cr: int, cw: int) -> None:
    bucket["inputTokens"] += inp
    bucket["outputTokens"] += out
    bucket["cacheReadTokens"] += cr
    bucket["cacheWriteTokens"] += cw
    bucket["messages"] += 1
    bucket["cost"] += _cost_for(model, inp, out, cr, cw)


def _all_jsonl_files() -> list[Path]:
    """Every transcript under ~/.claude/projects, recursively.

    Includes main-thread project dirs, subagents/, transcripts/, and wf_* —
    global uuid dedup (below) handles records that appear in more than one file,
    so we don't have to guess which directories to include.
    """
    if not CLAUDE_PROJECTS_BASE.is_dir():
        return []
    return list(CLAUDE_PROJECTS_BASE.rglob("*.jsonl"))


def _project_from_cwd(cwd: str | None) -> str:
    """Project label for a record, taken from its cwd basename.

    Using the record's own cwd (rather than the containing directory) means
    subagent and transcript records attribute to the right project regardless
    of which folder they were written to.
    """
    if not cwd:
        return "(unknown)"
    return os.path.basename(cwd.rstrip("/")) or cwd


def _round_bucket(b: dict) -> dict:
    return {**b, "cost": round(b["cost"], 4)}


def _round_map(m: dict) -> dict:
    return {k: _round_bucket(v) for k, v in m.items()}


async def get_usage(request: web.Request) -> web.Response:
    total = _empty_bucket()
    by_model: dict[str, dict] = defaultdict(_empty_bucket)
    by_agent: dict[str, dict] = defaultdict(_empty_bucket)
    by_project: dict[str, dict] = defaultdict(_empty_bucket)
    by_day: dict[str, dict] = defaultdict(_empty_bucket)

    seen: set[str] = set()  # assistant-record uuids already counted

    for f in _all_jsonl_files():
        try:
            fh = open(f)
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                model = msg.get("model") or "unknown"
                if model == "<synthetic>":
                    continue  # synthetic API-error placeholders carry no real usage

                # Dedupe: the same turn can be written to multiple files.
                uid = rec.get("uuid")
                if uid is not None:
                    if uid in seen:
                        continue
                    seen.add(uid)

                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                cr = usage.get("cache_read_input_tokens", 0) or 0
                cw = usage.get("cache_creation_input_tokens", 0) or 0

                _add(total, model, inp, out, cr, cw)
                _add(by_model[model], model, inp, out, cr, cw)

                agent = rec.get("attributionAgent") if rec.get("isSidechain") else None
                _add(by_agent[agent or "main"], model, inp, out, cr, cw)

                _add(by_project[_project_from_cwd(rec.get("cwd"))], model, inp, out, cr, cw)

                ts = rec.get("timestamp")
                if isinstance(ts, str) and len(ts) >= 10:
                    _add(by_day[ts[:10]], model, inp, out, cr, cw)

    # Recent daily series (last 30 active days), oldest→newest.
    recent_days = sorted(by_day.keys())[-30:]
    daily = [{"date": day, **_round_bucket(by_day[day])} for day in recent_days]

    return web.json_response({
        "total": _round_bucket(total),
        "byModel": _round_map(by_model),
        "byAgent": _round_map(by_agent),
        "byProject": _round_map(by_project),
        "daily": daily,
    })
