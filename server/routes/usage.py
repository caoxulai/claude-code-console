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

import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web


CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"

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

_CACHE_TTL = 60  # seconds


def register(app: web.Application):
    app.router.add_get("/api/usage", get_usage)
    app.router.add_get("/api/usage/heatmap", get_heatmap)
    app.router.add_get("/api/usage/tools", get_tools)


def _price_for(model: str) -> tuple[float, float]:
    return _PRICING.get(model, _DEFAULT_PRICE)


def _cost_for(model: str, inp: int, out: int, cache_read: int, cache_write: int) -> float:
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


_PROJECT_ALIASES = {
    "blackfalcon-oncall-agent": "oncall-agent",
    "GlennBlackFalconOncallDashboard": "oncall-kpi",
    "BlackFalconOncallDashboard": "oncall-kpi",
}


def _project_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    name = os.path.basename(cwd.rstrip("/")) or cwd
    return _PROJECT_ALIASES.get(name, name)


def _round_bucket(b: dict) -> dict:
    return {**b, "cost": round(b["cost"], 4)}


def _round_map(m: dict) -> dict:
    return {k: _round_bucket(v) for k, v in m.items()}


# ---------------------------------------------------------------------------
# Shared record cache — scan all files once, serve all 3 endpoints
# ---------------------------------------------------------------------------

@dataclass
class _Record:
    """Lightweight extracted fields from one assistant turn."""
    timestamp: str
    model: str
    inp: int
    out: int
    cache_read: int
    cache_write: int
    cwd: str | None
    is_sidechain: bool
    agent: str | None
    tool_names: list[str]
    tool_input_sizes: list[int]


@dataclass
class _Cache:
    records: list[_Record] = field(default_factory=list)
    responses: dict = field(default_factory=dict)  # pre-computed endpoint responses
    updated_at: float = 0.0


_cache = _Cache()
_cache_lock = asyncio.Lock()


def _scan_all_files() -> list[_Record]:
    """Parse all JSONL files and extract deduplicated assistant records."""
    if not CLAUDE_PROJECTS_BASE.is_dir():
        return []

    records: list[_Record] = []
    seen: set[str] = set()

    for f in CLAUDE_PROJECTS_BASE.rglob("*.jsonl"):
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

                uid = rec.get("uuid")
                if uid is not None:
                    if uid in seen:
                        continue
                    seen.add(uid)

                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue

                model = msg.get("model") or "unknown"
                if model == "<synthetic>":
                    continue

                usage = msg.get("usage")
                inp = out = cr = cw = 0
                if isinstance(usage, dict):
                    inp = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    cr = usage.get("cache_read_input_tokens", 0) or 0
                    cw = usage.get("cache_creation_input_tokens", 0) or 0

                # Extract tool_use names for the tools endpoint
                tool_names: list[str] = []
                tool_input_sizes: list[int] = []
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_names.append(block.get("name", "unknown"))
                            tool_inp = block.get("input")
                            tool_input_sizes.append(len(json.dumps(tool_inp)) if tool_inp else 0)

                records.append(_Record(
                    timestamp=rec.get("timestamp") or "",
                    model=model,
                    inp=inp,
                    out=out,
                    cache_read=cr,
                    cache_write=cw,
                    cwd=rec.get("cwd"),
                    is_sidechain=bool(rec.get("isSidechain")),
                    agent=rec.get("attributionAgent") if rec.get("isSidechain") else None,
                    tool_names=tool_names,
                    tool_input_sizes=tool_input_sizes,
                ))

    return records


def _compute_usage(records: list[_Record]) -> dict:
    """Pre-compute the /api/usage response from cached records."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    seattle_tz = ZoneInfo("America/Los_Angeles")

    total = _empty_bucket()
    by_model: dict[str, dict] = defaultdict(_empty_bucket)
    by_agent: dict[str, dict] = defaultdict(_empty_bucket)
    by_project: dict[str, dict] = defaultdict(_empty_bucket)
    by_day: dict[str, dict] = defaultdict(_empty_bucket)

    for r in records:
        if not (r.inp or r.out or r.cache_read or r.cache_write):
            continue
        _add(total, r.model, r.inp, r.out, r.cache_read, r.cache_write)
        _add(by_model[r.model], r.model, r.inp, r.out, r.cache_read, r.cache_write)
        _add(by_agent[r.agent or "main"], r.model, r.inp, r.out, r.cache_read, r.cache_write)
        _add(by_project[_project_from_cwd(r.cwd)], r.model, r.inp, r.out, r.cache_read, r.cache_write)

        ts = r.timestamp
        if ts and len(ts) >= 16:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                day_str = dt.astimezone(seattle_tz).strftime("%Y-%m-%d")
                _add(by_day[day_str], r.model, r.inp, r.out, r.cache_read, r.cache_write)
            except ValueError:
                pass

    recent_days = sorted(by_day.keys())[-30:]
    daily = [{"date": day, **_round_bucket(by_day[day])} for day in recent_days]

    return {
        "total": _round_bucket(total),
        "byModel": _round_map(by_model),
        "byAgent": _round_map(by_agent),
        "byProject": _round_map(by_project),
        "daily": daily,
    }


def _compute_heatmap(records: list[_Record]) -> dict:
    """Pre-compute the /api/usage/heatmap response from cached records."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    seattle_tz = ZoneInfo("America/Los_Angeles")
    cutoff = datetime.now(seattle_tz) - timedelta(weeks=12)

    grid = [[0] * 24 for _ in range(7)]
    day_counts: dict[str, int] = defaultdict(int)
    daily_hours: dict[str, list[int]] = defaultdict(lambda: [0] * 24)

    for r in records:
        ts = r.timestamp
        if not ts or len(ts) < 16:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        local_dt = dt.astimezone(seattle_tz)
        if local_dt < cutoff:
            continue
        date_str = local_dt.strftime("%Y-%m-%d")
        grid[local_dt.weekday()][local_dt.hour] += 1
        day_counts[date_str] += 1
        daily_hours[date_str][local_dt.hour] += 1

    return {
        "grid": grid,
        "days": dict(day_counts),
        "dailyHours": dict(daily_hours),
    }


def _compute_tools(records: list[_Record]) -> dict:
    """Pre-compute the /api/usage/tools response from cached records."""
    tool_stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "totalInputSize": 0})

    for r in records:
        for i, name in enumerate(r.tool_names):
            tool_stats[name]["calls"] += 1
            tool_stats[name]["totalInputSize"] += r.tool_input_sizes[i]

    rows = [
        {"name": name, "calls": s["calls"], "avgInputSize": round(s["totalInputSize"] / s["calls"]) if s["calls"] else 0}
        for name, s in tool_stats.items()
    ]
    rows.sort(key=lambda x: x["calls"], reverse=True)

    return {"tools": rows}


def _build_all_responses(records: list[_Record]) -> dict:
    """Compute all endpoint responses in one pass over the thread."""
    return {
        "usage": _compute_usage(records),
        "heatmap": _compute_heatmap(records),
        "tools": _compute_tools(records),
    }


async def _ensure_cache() -> dict:
    """Return pre-computed responses, refreshing if stale."""
    global _cache
    now = time.monotonic()
    if now - _cache.updated_at < _CACHE_TTL and _cache.responses:
        return _cache.responses

    async with _cache_lock:
        # Double-check after acquiring lock
        now = time.monotonic()
        if now - _cache.updated_at < _CACHE_TTL and _cache.responses:
            return _cache.responses

        def _scan_and_compute():
            records = _scan_all_files()
            return records, _build_all_responses(records)

        records, responses = await asyncio.to_thread(_scan_and_compute)
        _cache = _Cache(records=records, responses=responses, updated_at=time.monotonic())
        return responses


# ---------------------------------------------------------------------------
# Endpoints — return pre-computed cached responses
# ---------------------------------------------------------------------------

async def get_usage(request: web.Request) -> web.Response:
    responses = await _ensure_cache()
    return web.json_response(responses["usage"])


async def get_heatmap(request: web.Request) -> web.Response:
    responses = await _ensure_cache()
    return web.json_response(responses["heatmap"])


async def get_tools(request: web.Request) -> web.Response:
    responses = await _ensure_cache()
    return web.json_response(responses["tools"])
