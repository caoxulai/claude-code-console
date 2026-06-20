"""READ-ONLY /api/oscron — mirror the OS-level schedulers actually running.

Unlike /api/crons (the Claude Code harness scheduler, which only fires while a
session is active), this surfaces the reliable, unattended schedulers running on
this box: the user's crontab and systemd timers. The claude-web server runs ON
this machine AS the user, so ``crontab -l`` and ``systemctl`` return the real
state — there is no remote/bridge concern.

HARD CONSTRAINT — this module is strictly READ-ONLY. It NEVER writes, edits,
enables, disables, instruments, or deletes a crontab entry or a systemd unit. It
only shells out to read-only commands (``crontab -l``, ``systemctl list-timers``)
and reads job redirect logs. There are NO POST/PUT/DELETE routes.

Honesty over confidence (cf. feedback_principle_fail_loud_on_missing_input and
crons.py's "result not captured" philosophy): cron records no run OUTCOME, only a
schedule. We therefore mark each job's ``runHistory`` honestly ("logs_only" when a
``>>`` redirect log exists, else "none") and surface the last-execution result
ONLY from the tail of that redirect log — never a fabricated outcome. The next-run
time is computed DETERMINISTICALLY with croniter (never via an LLM); an
unparseable expression yields ``nextRun: null`` so the UI can show "unknown".

The subprocess invocations are isolated behind small async helpers
(``_run_crontab`` / ``_run_systemctl_user`` / ``_run_systemctl_system``) so tests
can monkeypatch them without execing real commands, and ``LOCAL_TZ_NAME`` is a
module attribute so the default cron timezone is overridable in tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

# cron uses literal wall-clock time, so we must be explicit about the timezone we
# compute next-runs in. A ``CRON_TZ=`` line wins when present; otherwise we default
# to America/Los_Angeles (the box's local zone), via zoneinfo so DST is correct.
LOCAL_TZ_NAME = "America/Los_Angeles"

# Lines/text that may carry credentials or cookie material — never surfaced.
# Mirrors crons.py's _SECRET_MARKERS (replicated, not cross-imported).
_SECRET_MARKERS = ("~/.midway", ".midway", "cookie", "mwinit", "aws_secret", "authorization:")

_DEFAULT_TAIL = 200
_MAX_TAIL = 500
# systemd timestamps are microseconds since epoch; epoch ms is what the frontend
# fmtWhen pattern expects, so we divide by 1000.
_SD_USEC_TO_MS = 1000

# Cap on how many recent runs we surface per job.
_MAX_RUNS = 20
# A crontab run with a start marker but NO finish marker is reported "running" only
# if its start is within this window; older orphaned starts are "unknown" (we never
# silently call them "succeeded" — cf. feedback_principle_fail_loud_on_missing_input).
_RUNNING_RECENCY_MS = 30 * 60 * 1000  # 30 minutes


def _looks_secret(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


def register(app: web.Application):
    # READ-ONLY: only GET routes, no mutating endpoints.
    app.router.add_get("/api/oscron", list_oscron)
    app.router.add_get("/api/oscron/{id}/runs", get_runs)
    app.router.add_get("/api/oscron/{id}/log", get_log)


# --------------------------------------------------------------------------- #
# Subprocess seams (monkeypatched in tests). Each returns (returncode, stdout).
# All are READ-only commands.
# --------------------------------------------------------------------------- #
async def _run(argv: list[str]) -> tuple[int, str]:
    """Run a command OFF the event loop; return (returncode, decoded stdout).

    Returns (127, "") if the binary is missing so callers degrade gracefully into
    an empty list instead of raising / 500-ing.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return 127, ""
    out, _err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def _run_crontab() -> tuple[int, str]:
    return await _run(["crontab", "-l"])


async def _run_systemctl_user() -> tuple[int, str]:
    return await _run(["systemctl", "--user", "list-timers", "--all", "--output=json"])


async def _run_systemctl_system() -> tuple[int, str]:
    return await _run(["systemctl", "list-timers", "--all", "--output=json"])


# Per-run history seams (READ-only). journald gives per-invocation Starting/Finished
# events; `systemctl show` gives the single most-recent invocation as a fallback.
# `--timestamp=unix` renders ExecMain* timestamps as ``@<epoch-seconds>`` which is
# deterministically parseable (the default human string is locale/format-fragile).
async def _run_journalctl_user(unit: str) -> tuple[int, str]:
    return await _run(["journalctl", "--user", "-u", unit, "-o", "json", "--no-pager"])


async def _run_journalctl_system(unit: str) -> tuple[int, str]:
    return await _run(["journalctl", "-u", unit, "-o", "json", "--no-pager"])


async def _run_systemctl_show_user(unit: str) -> tuple[int, str]:
    return await _run([
        "systemctl", "--user", "show", unit, "--timestamp=unix",
        "-p", "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp",
        "-p", "ExecMainStatus", "-p", "Result",
    ])


async def _run_systemctl_show_system(unit: str) -> tuple[int, str]:
    return await _run([
        "systemctl", "show", unit, "--timestamp=unix",
        "-p", "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp",
        "-p", "ExecMainStatus", "-p", "Result",
    ])


# --------------------------------------------------------------------------- #
# Parsing helpers (pure / deterministic).
# --------------------------------------------------------------------------- #
def _job_id(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]


# A redirect-to-log: ``>> /path/log`` or ``> /path/log`` (we don't treat ``2>&1``
# or ``2>`` stderr-only as the primary result log).
_REDIRECT_RE = re.compile(r"(?<![\d2])>>?\s*([^\s>&|;]+)")

# A crontab schedule is 5 whitespace-separated fields, then the command. We also
# accept @-shortcuts (@daily etc.) which croniter understands.
_CRON_LINE_RE = re.compile(r"^\s*(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$")
_CRON_TZ_RE = re.compile(r"^\s*CRON_TZ\s*=\s*(\S+)\s*$")
_AT_SHORTCUTS = {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly", "@reboot"}


def _parse_log_path(command: str) -> str | None:
    """Extract the first ``>>``/``>`` redirect target from a command, else None."""
    m = _REDIRECT_RE.search(command)
    if not m:
        return None
    return m.group(1)


def _resolve_tz(cron_tz: str | None) -> ZoneInfo:
    name = cron_tz or LOCAL_TZ_NAME
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        try:
            return ZoneInfo(LOCAL_TZ_NAME)
        except Exception:
            return ZoneInfo("UTC")


def _compute_next_runs(schedule: str, cron_tz: str | None, count: int = 3) -> tuple[int | None, list[int]]:
    """Deterministically compute (nextRun_ms, [next up to ``count`` fire times]).

    Returns (None, []) on an unparseable expression — never a fabricated time.
    """
    from croniter import croniter, CroniterBadCronError, CroniterNotAlphaError

    tz = _resolve_tz(cron_tz)
    try:
        base = datetime.now(tz)
        if not (schedule in _AT_SHORTCUTS or croniter.is_valid(schedule)):
            return None, []
        it = croniter(schedule, base)
        fires: list[int] = []
        for _ in range(max(1, count)):
            nxt = it.get_next(datetime)
            fires.append(int(nxt.timestamp() * 1000))
        return fires[0], fires
    except (CroniterBadCronError, CroniterNotAlphaError, ValueError, KeyError, AttributeError):
        return None, []


def _parse_crontab(text: str) -> list[dict]:
    """Parse ``crontab -l`` output into job dicts. Pure / deterministic.

    Tracks a running ``CRON_TZ=`` and accumulates preceding ``#`` comment lines for
    the next schedule line: the first comment becomes the short ``title``, the rest
    the longer ``description``. Secret-bearing command lines are dropped entirely
    (never surfaced).
    """
    jobs: list[dict] = []
    cron_tz: str | None = None
    comments: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            comments = []
            continue

        tz_m = _CRON_TZ_RE.match(line)
        if tz_m:
            cron_tz = tz_m.group(1)
            continue

        if stripped.startswith("#"):
            comment = stripped.lstrip("#").strip()
            # Skip credential-bearing comment lines (e.g. "needs ~/.midway/cookie")
            # so they never reach the surfaced description.
            if comment and not _looks_secret(comment):
                comments.append(comment)
            continue

        # Environment assignment (FOO=bar) that isn't a schedule — skip it.
        parts = stripped.split()
        if parts and "=" in parts[0] and not parts[0][0].isdigit() and parts[0][0] != "@":
            continue

        schedule: str | None = None
        command = ""
        first = stripped.split(None, 1)
        if first and first[0] in _AT_SHORTCUTS:
            schedule = first[0]
            command = first[1] if len(first) > 1 else ""
        else:
            m = _CRON_LINE_RE.match(line)
            if m:
                schedule = m.group(1).strip()
                command = m.group(2).strip()

        if schedule is None:
            comments = []
            continue

        if _looks_secret(command):
            # Drop credential-bearing commands entirely rather than risk leaking.
            comments = []
            continue

        next_run, next_runs = _compute_next_runs(schedule, cron_tz)
        log_path = _parse_log_path(command)
        # Cron has no title field; the convention is the comment block above the
        # entry. We treat the FIRST comment line as a short title (shown in the
        # list) and the remaining lines as the longer description (shown on expand).
        title = comments[0] if comments else None
        if title and _looks_secret(title):
            title = None
        description = " ".join(comments[1:]).strip() or None
        if description and _looks_secret(description):
            description = None
        jobs.append({
            "id": _job_id(raw_line),
            "source": "crontab",
            "schedule": schedule,
            "command": command,
            "cronTz": cron_tz,
            "title": title,
            "description": description,
            "nextRun": next_run,
            "nextRuns": next_runs,
            "lastRun": None,  # crontab records no last-run time.
            "logPath": log_path,
            "runHistory": "logs_only" if log_path else "none",
        })
        comments = []

    return jobs


def _usec_to_ms(value) -> int | None:
    """systemd timestamps are usec since epoch; 0/None means 'not scheduled'."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value) // _SD_USEC_TO_MS


def _parse_systemd_timers(text: str, scope: str) -> list[dict]:
    """Map ``systemctl list-timers --output=json`` into job dicts.

    Degrades to an empty list on unparseable JSON (never raises).
    """
    try:
        rows = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []

    jobs: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        unit = row.get("unit") or ""
        activates = row.get("activates") or ""
        if _looks_secret(unit) or _looks_secret(activates):
            continue
        next_run = _usec_to_ms(row.get("next"))
        last_run = _usec_to_ms(row.get("last"))
        jobs.append({
            "id": _job_id(f"systemd:{scope}:{unit}"),
            "source": "systemd",
            "unit": unit,
            "scope": scope,
            "schedule": "—",  # em dash: calendar spec not in list-timers JSON.
            "command": activates,
            "description": unit or activates,
            "nextRun": next_run,
            "nextRuns": [next_run] if next_run is not None else [],
            "lastRun": last_run,
            "logPath": None,
            "runHistory": "logs_only",  # journald-backed run history.
        })
    return jobs


# --------------------------------------------------------------------------- #
# Execution-history derivation (READ-only). Pure parsers + thin async wrappers.
# --------------------------------------------------------------------------- #

# A leading log timestamp: ``YYYY-MM-DD HH:MM:SS`` (optionally ``,mmm`` / ``.mmm``).
# We only need its presence as part of the start-marker contract; the authoritative
# epoch comes from the ISO timestamp inside the marker.
_LEADING_TS_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
# Run-start cue: ``=== ... starting at <ISO> ===`` (refresh_cron.py reference format).
_RUN_START_RE = re.compile(r"===.*\bstarting at\s+(\S+?)\s*===")
# Run-finish cue: ``finished: state=<succeeded|failed> ... finishedAt=<ISO>``.
_RUN_FINISH_RE = re.compile(
    r"finished:\s*state=(succeeded|failed)\b.*?finishedAt=(\S+)"
)
# A fatal/auth error line (no finish marker emitted) => the run failed.
_FATAL_RE = re.compile(r"\[(?:ERROR|FATAL|CRITICAL)\]\s*(?:AUTH\b|.*\bAUTH\b)", re.IGNORECASE)
_FATAL_GENERIC_RE = re.compile(r"\b(?:Traceback \(most recent call last\)|\[FATAL\]|\[CRITICAL\])")


def _iso_to_ms(value: str) -> int | None:
    """Parse an ISO-8601 timestamp into epoch ms. None on anything unparseable.

    Naive timestamps (no tz) are interpreted as UTC so a duration is still derivable
    consistently; we never fabricate a value on a parse failure.
    """
    if not value:
        return None
    text = value.strip().rstrip("=").strip()
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _segment_crontab_runs(lines: list[str], *, now_ms: int | None = None) -> list[dict]:
    """Segment a redirect log into runs by HEURISTIC. Pure / deterministic.

    Returns a list of run dicts (oldest-first); the caller reverses + caps. A run is
    bounded by a start marker and either an explicit finish marker, a fatal/auth
    error before the next start, or the next start (orphan). lineStart/lineEnd are
    1-based offsets so the log-slice endpoint can fetch exactly that run's lines.

    NEVER fabricates a status: a finish marker sets succeeded/failed, a fatal line
    sets failed, and an orphaned start is "running" (only if recent) or "unknown".
    Returns [] when NO start markers are present so the caller reports parsed:false.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    runs: list[dict] = []
    cur: dict | None = None

    def _close(end_line: int) -> None:
        nonlocal cur
        if cur is None:
            return
        cur["lineEnd"] = end_line
        if cur["status"] == "running":
            # Orphan (no finish/fatal seen): recent => running, else unknown.
            started = cur["startedAt"]
            if started is None or (now_ms - started) > _RUNNING_RECENCY_MS:
                cur["status"] = "unknown"
        runs.append(cur)
        cur = None

    for idx, raw in enumerate(lines, start=1):
        start_m = _RUN_START_RE.search(raw)
        if start_m and _LEADING_TS_RE.match(raw):
            # A new start closes the previous (still-open) run as an orphan.
            _close(idx - 1)
            cur = {
                "startedAt": _iso_to_ms(start_m.group(1)),
                "finishedAt": None,
                "durationMs": None,
                "status": "running",  # provisional; resolved at close.
                "trigger": None,
                "lineStart": idx,
                "lineEnd": idx,
            }
            continue

        if cur is None:
            continue

        # Capture trigger from the Claimed line, e.g. "(trigger=cron)".
        if cur["trigger"] is None:
            trig_m = re.search(r"trigger=([A-Za-z0-9_-]+)", raw)
            if trig_m:
                cur["trigger"] = trig_m.group(1)

        fin_m = _RUN_FINISH_RE.search(raw)
        if fin_m:
            cur["status"] = fin_m.group(1)
            cur["finishedAt"] = _iso_to_ms(fin_m.group(2))
            if cur["startedAt"] is not None and cur["finishedAt"] is not None:
                cur["durationMs"] = max(0, cur["finishedAt"] - cur["startedAt"])
            _close(idx)
            continue

        # A fatal/auth error before any finish marker => the run failed.
        if _FATAL_RE.search(raw) or _FATAL_GENERIC_RE.search(raw):
            cur["status"] = "failed"
            _close(idx)
            continue

    _close(len(lines))
    return runs


def _crontab_runs(lines: list[str]) -> dict:
    """Build the runs response for a crontab job from its (scrubbed) log lines."""
    segmented = _segment_crontab_runs(lines)
    if not segmented:
        return {"runs": [], "parsed": False, "source": "crontab"}
    # Most-recent first, capped.
    segmented.reverse()
    return {"runs": segmented[:_MAX_RUNS], "parsed": True, "source": "crontab"}


def _parse_journal_runs(text: str) -> list[dict]:
    """Pair Starting/Finished journald events into runs. Pure / deterministic.

    Each line is a JSON object. ``Starting <unit>`` opens a run (start =
    __REALTIME_TIMESTAMP usec->ms); ``Finished <unit>`` closes it (finish ts +
    JOB_RESULT "done"->succeeded else failed). Returns oldest-first; caller reverses.
    MESSAGE text is scrubbed before any use. Returns [] on no parseable events.
    """
    runs: list[dict] = []
    cur: dict | None = None

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        msg = entry.get("MESSAGE")
        if not isinstance(msg, str) or _looks_secret(msg):
            continue
        ts = _usec_to_ms(_as_int(entry.get("__REALTIME_TIMESTAMP")))

        if msg.startswith("Starting "):
            cur = {
                "startedAt": ts,
                "finishedAt": None,
                "durationMs": None,
                "status": "running",
                "trigger": None,
                "lineStart": 0,
                "lineEnd": 0,
            }
        elif msg.startswith("Finished "):
            result = entry.get("JOB_RESULT")
            status = "succeeded" if result == "done" else "failed"
            if cur is not None:
                cur["finishedAt"] = ts
                cur["status"] = status
                if cur["startedAt"] is not None and ts is not None:
                    cur["durationMs"] = max(0, ts - cur["startedAt"])
                runs.append(cur)
                cur = None
            elif ts is not None:
                runs.append({
                    "startedAt": None, "finishedAt": ts, "durationMs": None,
                    "status": status, "trigger": None, "lineStart": 0, "lineEnd": 0,
                })

    if cur is not None:
        # Orphan start with no Finished event => running/unknown by recency.
        now_ms = int(time.time() * 1000)
        if cur["startedAt"] is None or (now_ms - cur["startedAt"]) > _RUNNING_RECENCY_MS:
            cur["status"] = "unknown"
        runs.append(cur)
    return runs


def _as_int(value) -> int | None:
    """journald __REALTIME_TIMESTAMP is a usec string; coerce to int, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# systemctl show with --timestamp=unix renders timestamps as ``@<epoch-seconds>``.
_SHOW_TS_RE = re.compile(r"@(\d+)")


def _parse_show_run(text: str) -> dict | None:
    """Parse `systemctl show -p ExecMain*` into a single most-recent run, or None."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()

    def _ts(key: str) -> int | None:
        val = fields.get(key, "")
        m = _SHOW_TS_RE.search(val)
        return int(m.group(1)) * 1000 if m else None

    started = _ts("ExecMainStartTimestamp")
    finished = _ts("ExecMainExitTimestamp")
    if started is None and finished is None:
        return None

    result = fields.get("Result", "")
    exit_status = fields.get("ExecMainStatus", "")
    if finished is None:
        status = "running"
    elif result == "success" or exit_status == "0":
        status = "succeeded"
    else:
        status = "failed"
    duration = None
    if started is not None and finished is not None:
        duration = max(0, finished - started)
    return {
        "startedAt": started,
        "finishedAt": finished,
        "durationMs": duration,
        "status": status,
        "trigger": None,
        "lineStart": 0,
        "lineEnd": 0,
    }


async def _systemd_runs(unit: str, scope: str) -> dict:
    """Build the runs response for a systemd job (journald + systemctl-show)."""
    if scope == "user":
        jrc, jout = await _run_journalctl_user(unit)
    else:
        jrc, jout = await _run_journalctl_system(unit)

    runs: list[dict] = []
    if jrc == 0 and jout.strip():
        runs = _parse_journal_runs(jout)

    if not runs:
        # Fall back to the single most-recent invocation from systemctl show.
        if scope == "user":
            src, sout = await _run_systemctl_show_user(unit)
        else:
            src, sout = await _run_systemctl_show_system(unit)
        if src == 0 and sout.strip():
            one = _parse_show_run(sout)
            if one is not None:
                runs = [one]

    if not runs:
        return {"runs": [], "parsed": False, "source": "systemd"}
    runs.reverse()  # most-recent first
    return {"runs": runs[:_MAX_RUNS], "parsed": True, "source": "systemd"}


async def _collect_jobs() -> list[dict]:
    """Gather all OS-cron + systemd jobs (READ-only). Each source degrades to []."""
    jobs: list[dict] = []

    (rc, out), (rc_u, out_u), (rc_s, out_s) = await asyncio.gather(
        _run_crontab(), _run_systemctl_user(), _run_systemctl_system()
    )
    if rc == 0 and out.strip():
        jobs.extend(_parse_crontab(out))
    if rc_u == 0 and out_u.strip():
        jobs.extend(_parse_systemd_timers(out_u, "user"))
    if rc_s == 0 and out_s.strip():
        jobs.extend(_parse_systemd_timers(out_s, "system"))

    return jobs


async def list_oscron(request: web.Request) -> web.Response:
    jobs = await _collect_jobs()
    return web.json_response({"jobs": jobs})


def _read_all_lines(path: Path) -> list[str]:
    """Read all lines (newline-stripped) for run segmentation.

    These lines are NEVER returned to the client — the segmenter derives only
    status/timestamps/line-offsets from them, so a secret-bearing line (e.g. an
    AUTH-error path containing ``.midway``) must still be visible to the FATAL
    matcher. The log-slice endpoint scrubs separately when it returns actual text.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        return [ln.rstrip("\n") for ln in fh]


async def get_runs(request: web.Request) -> web.Response:
    """Return a job's execution history, derived honestly from real evidence.

    Stateless (re-parse sources to resolve id -> job, like get_log). Shape:
    ``{runs:[...], parsed:bool, source:"crontab"|"systemd"}``. Any
    missing/unreadable/unparseable path degrades to ``{runs:[], parsed:false}`` —
    NEVER a 500, NEVER a fabricated run/status/time.
    """
    job_id = request.match_info["id"]
    jobs = await _collect_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)

    if job is None:
        return web.json_response({"runs": [], "parsed": False, "source": "crontab"})

    source = job.get("source")
    if source == "systemd":
        try:
            result = await _systemd_runs(job.get("unit") or "", job.get("scope") or "system")
        except (OSError, ValueError):
            result = {"runs": [], "parsed": False, "source": "systemd"}
        return web.json_response(result)

    # crontab: segment the (scrubbed) redirect log into runs.
    log_path = job.get("logPath")
    if not log_path:
        return web.json_response({"runs": [], "parsed": False, "source": "crontab"})
    path = Path(log_path).expanduser()
    try:
        loop = asyncio.get_running_loop()
        lines = await loop.run_in_executor(None, _read_all_lines, path)
    except (OSError, UnicodeDecodeError):
        return web.json_response({"runs": [], "parsed": False, "source": "crontab"})

    return web.json_response(_crontab_runs(lines))


def _read_tail(path: Path, tail: int) -> tuple[list[str], bool]:
    """Read up to the last ``tail`` lines of a file. Returns (lines, truncated).

    Secret-bearing lines are filtered out before returning.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()
    truncated = len(all_lines) > tail
    selected = all_lines[-tail:]
    cleaned = [ln.rstrip("\n") for ln in selected if not _looks_secret(ln)]
    return cleaned, truncated


def _read_line_range(path: Path, line_start: int, line_end: int) -> tuple[list[str], bool]:
    """Read a 1-based inclusive line range, clamped and capped at _MAX_TAIL.

    Returns (lines, truncated) where ``truncated`` is True iff the requested range
    exceeded _MAX_TAIL and was capped. Secret-bearing lines are scrubbed out.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()
    total = len(all_lines)
    lo = max(1, line_start)
    hi = min(total, line_end)
    if hi < lo:
        return [], False
    truncated = (hi - lo + 1) > _MAX_TAIL
    if truncated:
        hi = lo + _MAX_TAIL - 1
    selected = all_lines[lo - 1:hi]
    cleaned = [ln.rstrip("\n") for ln in selected if not _looks_secret(ln)]
    return cleaned, truncated


async def get_log(request: web.Request) -> web.Response:
    """Return the tail of a job's redirect log, by recomputing id -> logPath.

    Stateless: we re-parse the crontab/systemd sources to resolve the id (the
    server holds no in-memory job registry). A missing/unreadable file or a job
    with no logPath returns an explicit ``error: "not available"`` with empty
    lines — never a fabricated outcome.
    """
    job_id = request.match_info["id"]
    try:
        tail = int(request.query.get("tail", _DEFAULT_TAIL))
    except (TypeError, ValueError):
        tail = _DEFAULT_TAIL
    tail = max(1, min(tail, _MAX_TAIL))

    line_start, line_end = _parse_line_range(request.query)

    jobs = await _collect_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)

    # systemd jobs have no redirect file; a per-run journald slice isn't reliably
    # addressable here, so we return an explicit "not available" rather than a fake.
    if job is not None and job.get("source") == "systemd":
        return web.json_response({"lines": [], "path": None, "error": "not available"})

    log_path = job.get("logPath") if job else None
    if not log_path:
        return web.json_response({"lines": [], "path": log_path, "error": "not available"})

    path = Path(log_path).expanduser()
    try:
        loop = asyncio.get_running_loop()
        if line_start is not None and line_end is not None:
            # Per-run slice: exactly that run's line range (clamped + capped).
            lines, truncated = await loop.run_in_executor(
                None, _read_line_range, path, line_start, line_end)
        else:
            lines, truncated = await loop.run_in_executor(None, _read_tail, path, tail)
    except (OSError, UnicodeDecodeError):
        return web.json_response({"lines": [], "path": log_path, "error": "not available"})

    return web.json_response({"lines": lines, "path": log_path, "truncated": truncated})


def _parse_line_range(query) -> tuple[int | None, int | None]:
    """Parse optional ?lineStart=&lineEnd= ints; both must be present & valid."""
    raw_start = query.get("lineStart")
    raw_end = query.get("lineEnd")
    if raw_start is None or raw_end is None:
        return None, None
    try:
        return int(raw_start), int(raw_end)
    except (TypeError, ValueError):
        return None, None
