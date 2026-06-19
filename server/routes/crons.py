"""CRUD /api/crons — manage scheduled_tasks.json + reconcile run history.

Source-of-truth note: the Claude Code harness scheduler (the CronCreate tool)
persists durable tasks to ``.claude/scheduled_tasks.json`` resolved **relative to
the session's working directory**, NOT to ``~/.claude/``. Empirically confirmed:
a harness-created durable job landed in ``<cwd>/.claude/scheduled_tasks.json``
while ``~/.claude/scheduled_tasks.json`` stayed empty. The claude-web server is
launched from the same working directory as the session, so we resolve the same
cwd-relative path here — that way the Cron Jobs tab is a faithful view of exactly
the tasks the scheduler fires, instead of a second, divergent list.

If the server is ever started from a different directory than the scheduler, set
``CLAUDE_WEB_TASKS_PATH`` to the scheduler's file to keep them aligned.

What the harness records when a durable job fires (verified on disk 2026-06-15):
  * FIRE TIMESTAMP — yes. The harness writes a numeric ``lastFiredAt`` (epoch
    millis) onto the task in scheduled_tasks.json when it fires. E.g. the live
    pipeline-health job (id 3af99bc9) showed ``lastFiredAt=1781482020160`` ==
    2026-06-15T00:07:00.16Z. (An earlier comment here claimed "real tasks have no
    lastFiredAt" — that was wrong and is corrected.)
  * EXECUTION OUTPUT — NOT in any cron-specific file. cron_runs.json is written
    ONLY by claude-web's own POST /api/crons/{id}/runs; the harness never touches
    it. The harness instead fires a durable job by ENQUEUEING the job prompt into
    the CREATING session's transcript at
    ``~/.claude/projects/<cwd-slug>/<createdBySessionId>.jsonl`` as a JSONL line
    ``{"type":"queue-operation","operation":"enqueue","timestamp":"<ISO>", ...}``.
    The enqueue ``timestamp`` matches ``lastFiredAt`` to the millisecond (verified:
    2026-06-15T00:07:00.161Z == 1781482020160). The assistant's reply (the actual
    VERDICT/result text) appears on the assistant-message lines that follow, up to
    the next enqueue or the next real user prompt.

Reconciliation choice: we reconcile SERVER-SIDE in GET /api/crons/{id}/runs (the
spec-preferred single source of truth — easier to test than client-side merging).
On read we (1) load the console-POSTed runs from cron_runs.json, (2) read the
harness task's ``lastFiredAt`` and, if present, synthesize a ``fired`` run entry
best-effort enriched with harvested output text, (3) de-dupe a fire against any
console run within ~60s, and (4) merge newest-first. We NEVER fabricate a
success/failure outcome: a fire with no harvestable output surfaces as a ``fired``
entry with empty ``result``, which the UI renders as "result not captured". All
harness files (scheduled_tasks.json, the transcript) are treated READ-only.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

from server.routes import read_json_body

from server import filestore

logger = logging.getLogger(__name__)


def _resolve_tasks_path() -> Path:
    """Resolve the scheduled_tasks.json the harness scheduler actually fires from.

    Precedence:
      1. ``CLAUDE_WEB_TASKS_PATH`` env override (explicit alignment escape hatch).
      2. ``<cwd>/.claude/scheduled_tasks.json`` — where the harness writes durable
         tasks (cwd-relative); the server shares the session's cwd.
    """
    env = os.environ.get("CLAUDE_WEB_TASKS_PATH")
    if env:
        return Path(env)
    return Path.cwd() / ".claude" / "scheduled_tasks.json"


# Resolved once at import (the server's cwd is fixed for its lifetime). Kept as a
# module attribute so it stays inspectable and overridable (tests monkeypatch it).
TASKS_PATH = _resolve_tasks_path()


def _resolve_runs_path() -> Path:
    """Resolve the cron run-history sidecar file.

    Derived from the SAME logic as TASKS_PATH so the two files stay aligned:
    ``cron_runs.json`` lives in the same dir as the harness scheduled_tasks.json.
    A ``CLAUDE_WEB_CRON_RUNS_PATH`` env override mirrors ``CLAUDE_WEB_TASKS_PATH``.
    """
    env = os.environ.get("CLAUDE_WEB_CRON_RUNS_PATH")
    if env:
        return Path(env)
    return TASKS_PATH.parent / "cron_runs.json"


# Run-history is a SEPARATE store, never written into scheduled_tasks.json. WHY:
# the Claude Code harness DOES record a fire timestamp (it writes a numeric
# lastFiredAt onto the task when it fires) but does NOT record execution OUTPUT in
# any cron file — cron_runs.json only ever holds runs that flowed through
# claude-web's own POST endpoint. So on read we reconcile the two: console-POSTed
# runs from this sidecar PLUS a synthesized "fired" entry derived from the harness
# lastFiredAt (best-effort enriched from the creating session's transcript). We
# NEVER fabricate a success/failure outcome and NEVER write into the harness files.
RUNS_PATH = _resolve_runs_path()

# Where the harness stores per-session transcripts. Mirrors sessions.py so the
# harvester finds the same files; kept as a module attribute so tests can patch it.
CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"

# Outcomes we accept on a console-POSTed run; anything else is normalized to
# "unknown". "fired" is reserved for harness-fire entries we synthesize on read
# (it is NOT acceptable as a client-supplied outcome — see append_run).
_VALID_OUTCOMES = {"success", "failure", "unknown"}
# A synthesized harness-fire run. Distinct from the POST outcomes above so the UI
# can render it as a neutral "fired" badge (never a fabricated success/failure).
_FIRED_OUTCOME = "fired"
# Console run vs harness fire are "the same fire" if their timestamps fall within
# this window — the harness lastFiredAt and a console POST for the same run won't
# be byte-identical, so we collapse near-coincident entries.
_FIRE_DEDUPE_MS = 60_000
_RESULT_CAP = 4000
# Hard cap on how many harvested fires we return / persist per job. A long-lived
# recurring cron can accrue hundreds of fires in one transcript; we keep only the
# most-recent slice so the sidecar and the response stay bounded.
_RUNS_CAP = 100


def _project_path_to_claude_slug(project_path: str) -> str:
    """Convert a real project path to the Claude session-dir slug.

    Mirrors server.routes.sessions._project_path_to_claude_slug. Replicated here
    (rather than imported) to avoid a cross-route import cycle for a one-liner:
    Claude encodes cwds by replacing "/" with "-" and prepending "-".
    """
    return "-" + project_path.lstrip("/").replace("/", "-")


# Lines/text that may carry credentials or cookie material — never surfaced.
_SECRET_MARKERS = ("~/.midway", ".midway", "cookie", "mwinit", "aws_secret", "authorization:")


def _looks_secret(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


def _assistant_text(obj: dict) -> str:
    """Extract concatenated assistant text-block content from a transcript line."""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def _is_real_user_prompt(obj: dict) -> bool:
    """True for a genuine user turn (a boundary), False for a tool_result line.

    A real user prompt carries string content or a text block; the intermediate
    user lines that interleave assistant tool calls carry only tool_result blocks.
    """
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def _first_nonempty_line(text) -> str:
    """Return the first stripped non-empty line of ``text`` (or "")."""
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _collect_result_after(lines: list[dict], start_idx: int) -> str:
    """Collect the assistant text turns following ``lines[start_idx]`` (an enqueue).

    Walks forward from ``start_idx + 1`` until the NEXT enqueue or the next real
    user prompt (a genuine turn boundary), concatenating assistant text turns and
    scrubbing any credential-bearing line. Capped at ``_RESULT_CAP``. Shared by
    both ``_harvest_fire_result`` (lastFiredAt fallback) and ``_harvest_all_fires``
    so the two harvest paths can never drift.
    """
    collected: list[str] = []
    for obj in lines[start_idx + 1:]:
        t = obj.get("type")
        if t == "queue-operation" and obj.get("operation") == "enqueue":
            break  # next fire — stop
        if t == "user" and _is_real_user_prompt(obj):
            # The fired job's own prompt is echoed as a user line right after the
            # enqueue; skip it. Only a user prompt that arrives AFTER we've captured
            # assistant text is a genuine turn boundary.
            if collected:
                break
            continue
        if t == "assistant":
            text = _assistant_text(obj).strip()
            if text and not _looks_secret(text):
                collected.append(text)
    result = "\n\n".join(collected).strip()
    return result[:_RESULT_CAP]


def _locate_transcript(job: dict) -> Path | None:
    """Resolve a job's creating-session transcript path (READ-only), or None.

    If the job records a cwd → derive the Claude session-dir slug; otherwise fall
    back to scanning the project dirs for ``<sessionId>.jsonl`` (the live harness
    jobs have cwd=None, so the slug is otherwise underivable). Shared by both
    harvest paths so transcript resolution can never drift.
    """
    session_id = job.get("createdBySessionId")
    if not session_id:
        return None
    cwd = job.get("cwd") or job.get("createdInCwd")
    if cwd:
        slug = _project_path_to_claude_slug(str(Path(cwd).resolve()))
        return CLAUDE_PROJECTS_BASE / slug / f"{session_id}.jsonl"
    return _find_session_transcript(session_id)


def _parse_transcript(path: Path) -> list[dict]:
    """Parse a .jsonl transcript ONCE into a list of dict lines (bad lines skipped)."""
    lines: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                lines.append(obj)
    return lines


def _harvest_fire_result(job: dict, last_fired_ms: int) -> str:
    """Best-effort, READ-only harvest of a fired job's result text.

    Opens the creating session's transcript, finds the enqueue line whose timestamp
    is closest to ``last_fired_ms``, then collects the assistant text turns that
    follow up to the next enqueue / real user prompt. Returns "" (the honest
    "result not captured" fallback) on ANY missing-file / parse / no-match error —
    we never fabricate output. Credential-bearing text is scrubbed out.
    """
    if not job.get("createdBySessionId"):
        return ""
    try:
        path = _locate_transcript(job)
        if path is None or not path.is_file():
            return ""

        lines = _parse_transcript(path)

        # Locate the enqueue line closest to the fire timestamp.
        best_idx = None
        best_delta = None
        for i, obj in enumerate(lines):
            if obj.get("type") == "queue-operation" and obj.get("operation") == "enqueue":
                ms = _iso_to_ms(obj.get("timestamp"))
                if ms is None:
                    continue
                delta = abs(ms - last_fired_ms)
                if best_delta is None or delta < best_delta:
                    best_delta, best_idx = delta, i
        if best_idx is None or (best_delta is not None and best_delta > _FIRE_DEDUPE_MS):
            return ""

        return _collect_result_after(lines, best_idx)
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


def _enqueue_is_fire(content, prompt_first_line: str) -> bool:
    """Decide whether an enqueue's ``content`` is a fire of THIS job.

    MATCHER RULE (verified against the live warm-up transcript 6213bbfd-*.jsonl,
    657 fires): an enqueue is a fire of this job when the FIRST non-empty line of
    its ``content`` equals (strip()-compared) the first non-empty line of the job's
    ``prompt`` — for the warm-up that is the stable ``SLACK-WARMUP v1 (claude-web)``
    marker line. We EXCLUDE enqueues whose content (stripped) starts with
    ``<task-notification>``: those are workflow notifications the harness enqueues,
    not cron fires, and would otherwise inflate the count.
    """
    if not isinstance(content, str) or not prompt_first_line:
        return False
    if content.strip().startswith("<task-notification>"):
        return False
    return _first_nonempty_line(content) == prompt_first_line


def _harvest_all_fires(job: dict) -> list[dict]:
    """Enumerate EVERY recoverable fire for a job from its session transcript.

    READ-only. Parses the creating-session transcript once, finds every enqueue
    line that matches this job's prompt (see ``_enqueue_is_fire``), and builds one
    run entry per match enriched with the assistant text turns that follow it
    (``_collect_result_after``, which scrubs secrets and caps the result). Returns
    newest-first, capped at ``_RUNS_CAP``; logs (never silently truncates) when the
    transcript held more matched fires than the cap. Returns ``[]`` on ANY
    missing-session / missing-file / parse error — never raises, never fabricates.
    """
    if not job.get("createdBySessionId"):
        return []
    prompt_first_line = _first_nonempty_line(job.get("prompt"))
    if not prompt_first_line:
        return []
    try:
        path = _locate_transcript(job)
        if path is None or not path.is_file():
            return []
        lines = _parse_transcript(path)

        fires: list[dict] = []
        for i, obj in enumerate(lines):
            if obj.get("type") != "queue-operation" or obj.get("operation") != "enqueue":
                continue
            if not _enqueue_is_fire(obj.get("content"), prompt_first_line):
                continue
            ts = _iso_to_ms(obj.get("timestamp"))
            if ts is None:
                continue
            fires.append({
                "id": f"fired-{ts}",
                "ts": ts,
                "outcome": _FIRED_OUTCOME,
                "result": _collect_result_after(lines, i),
                "source": "harness",
            })
    except (OSError, UnicodeDecodeError, ValueError):
        return []

    fires.sort(key=lambda r: r["ts"], reverse=True)
    if len(fires) > _RUNS_CAP:
        dropped = len(fires) - _RUNS_CAP
        logger.info(
            "cron %s: harvested %d fires from transcript, returning most-recent %d "
            "(dropped %d oldest)",
            job.get("id"), len(fires), _RUNS_CAP, dropped,
        )
        fires = fires[:_RUNS_CAP]
    return fires


def _find_session_transcript(session_id: str) -> Path | None:
    """Scan project dirs for <session_id>.jsonl (READ-only; best-effort)."""
    try:
        if not CLAUDE_PROJECTS_BASE.is_dir():
            return None
        for proj in CLAUDE_PROJECTS_BASE.iterdir():
            candidate = proj / f"{session_id}.jsonl"
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def _iso_to_ms(value) -> int | None:
    """Parse an ISO-8601 transcript timestamp to epoch millis, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        iso = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def register(app: web.Application):
    app.router.add_get("/api/crons", list_crons)
    app.router.add_post("/api/crons", create_cron)
    app.router.add_put("/api/crons/{job_id}", update_cron)
    app.router.add_delete("/api/crons/{job_id}", delete_cron)
    app.router.add_get("/api/crons/{job_id}/runs", list_runs)
    app.router.add_post("/api/crons/{job_id}/runs", append_run)


def _load() -> tuple[dict, str | None]:
    data, etag = filestore.read_json(TASKS_PATH)
    if not data:
        data = {"tasks": []}
    if "tasks" not in data:
        data["tasks"] = []
    return data, etag


def _load_runs() -> tuple[dict, str | None]:
    data, etag = filestore.read_json(RUNS_PATH)
    if not data:
        data = {"runs": {}}
    if "runs" not in data:
        data["runs"] = {}
    return data, etag


async def list_crons(request: web.Request) -> web.Response:
    data, etag = _load()
    return web.json_response({"jobs": data["tasks"], "etag": etag})


async def create_cron(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    cron_expr = body.get("cron", "").strip()
    prompt = body.get("prompt", "").strip()
    recurring = body.get("recurring", True)
    description = body.get("description", "").strip()

    if not cron_expr or not prompt:
        raise web.HTTPBadRequest(reason="cron and prompt required")

    data, etag = _load()
    job = {
        "id": secrets.token_hex(4),
        "cron": cron_expr,
        "prompt": prompt,
        "recurring": recurring,
        "createdAt": int(time.time() * 1000),
        "lastFiredAt": None,
    }
    # Purely additive + optional: only persist when non-empty so existing jobs and
    # callers that omit it stay byte-identical (no description:null key).
    if description:
        job["description"] = description
    data["tasks"].append(job)

    try:
        new_etag = filestore.write_json(TASKS_PATH, data, etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_changed", {"id": job["id"]})
    return web.json_response({"job": job, "etag": new_etag}, status=201)


async def update_cron(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")

    data, current_etag = _load()
    job = next((j for j in data["tasks"] if j.get("id") == job_id), None)
    if not job:
        raise web.HTTPNotFound(reason=f"job {job_id} not found")

    if "cron" in body:
        job["cron"] = body["cron"]
    if "prompt" in body:
        job["prompt"] = body["prompt"]
    if "recurring" in body:
        job["recurring"] = body["recurring"]
    if "description" in body:
        job["description"] = body["description"]

    try:
        new_etag = filestore.write_json(TASKS_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_changed", {"id": job_id})
    return web.json_response({"job": job, "etag": new_etag})


async def delete_cron(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    # Read the body whether or not Content-Length is set (chunked bodies report
    # content_length=None) so the optimistic-concurrency etag is always honored.
    # An empty body is fine — it just means no etag was supplied.
    if request.can_read_body:
        body = await read_json_body(request)
    else:
        body = {}
    expected_etag = body.get("etag")

    data, current_etag = _load()
    original_len = len(data["tasks"])
    data["tasks"] = [j for j in data["tasks"] if j.get("id") != job_id]

    if len(data["tasks"]) == original_len:
        raise web.HTTPNotFound(reason=f"job {job_id} not found")

    try:
        new_etag = filestore.write_json(TASKS_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_deleted", {"id": job_id})
    return web.json_response({"deleted": job_id, "etag": new_etag})


def _is_harness_fire(run: dict) -> bool:
    """A run is a harvested/synthesized harness fire (vs a console POST)."""
    return run.get("source") == "harness" or str(run.get("id", "")).startswith("fired-")


def _newest_persisted_harness_ts(runs: list[dict]) -> int | None:
    """Most-recent ts among persisted harness fires, or None if there are none."""
    ts = [int(r["ts"]) for r in runs
          if _is_harness_fire(r) and isinstance(r.get("ts"), (int, float))]
    return max(ts) if ts else None


def _dedupe_by_ts(runs: list[dict]) -> list[dict]:
    """Collapse a harness fire against the ONE near-coincident console POST (same fire).

    A console POST and the harness lastFiredAt / harvested-enqueue ts for ONE fire
    won't be byte-identical, so we treat them as the same fire when they fall within
    _FIRE_DEDUPE_MS — but the collapse is across the harness/console boundary AND
    one-to-one: a given console POST absorbs AT MOST ONE harness fire (its nearest
    neighbour), never a whole burst.

    This one-to-one rule is essential for high-frequency crons: the warm-up cron
    fires 0-3s apart, so many distinct harvested fires (each keyed by a unique
    ``fired-<ts>`` id) sit inside a single 60s window. A blanket "drop any harness
    fire within 60s of a console POST" rule silently dropped ~49 of them. Here, a
    lone console run only ever cancels the single fire it actually reported; the
    rest survive as distinct records.

    We never collapse two harness fires against each other, and two genuine console
    POSTs close in time are DISTINCT runs that both survive. Identical ids are still
    squashed (belt-and-suspenders against a re-harvested fire). Iterating newest-
    first, the console POST (reached ahead of a fire of equal ts via the stable sort)
    wins the slot, so a real outcome beats a synthesized fire.
    """
    kept: list[dict] = []
    seen_ids: set = set()
    # ts of kept console POSTs that have NOT yet absorbed a harness fire.
    free_console_ts: list[int] = []
    for r in runs:
        rid = r.get("id")
        if rid is not None and rid in seen_ids:
            continue
        rt = r.get("ts", 0)
        if _is_harness_fire(r):
            # Collapse into the nearest still-unmatched console POST within the window.
            match_idx = None
            best = _FIRE_DEDUPE_MS
            for idx, ct in enumerate(free_console_ts):
                d = abs(rt - ct)
                if d < best:
                    best, match_idx = d, idx
            if match_idx is not None:
                free_console_ts.pop(match_idx)  # that POST is now spent
                continue
        else:
            free_console_ts.append(rt)
        kept.append(r)
        if rid is not None:
            seen_ids.add(rid)
    return kept


def _backfill_harness_fires(job_id: str, job: dict, sidecar_data: dict,
                            sidecar_etag) -> tuple[list[dict], str | None]:
    """Harvest ALL transcript fires, persist the NEW ones into cron_runs.json.

    IDEMPOTENCE / INCREMENTAL: we only ever insert fires NEWER than the newest
    harness fire already persisted for this job (or ALL of them on the very first
    read, when none are persisted yet). A second GET therefore inserts nothing and
    the sidecar does not grow — the full transcript scan does meaningful work only
    on the first read (or when fresh fires accrue).

    De-dupe at persist time is purely by the unique ``fired-<ts>`` id: distinct fires
    seconds apart keep distinct ids and must each persist (high-frequency crons fire
    seconds apart). We do NOT drop a harvested fire here just because it lands near a
    console POST — that cross-boundary collapse is the read-time job of
    ``_dedupe_by_ts`` (which folds a console POST into at most ONE coincident fire),
    so a single console run can never swallow a whole dense burst of distinct fires.

    Returns ``(harvested_fires, etag)`` where ``etag`` is the post-write etag if a
    persist happened, else the read etag. A ConflictError (a concurrent console POST
    wrote the sidecar between our read and write) is SWALLOWED — we skip the persist
    this round and still serve the reconciled view, so a backfill can never 500 the
    GET (re-read & merge, never blind-overwrite — the shared-store principle).
    """
    harvested = _harvest_all_fires(job)
    if not harvested:
        return [], sidecar_etag

    persisted = sidecar_data["runs"].get(job_id, [])
    newest = _newest_persisted_harness_ts(persisted)
    existing_ids = {r.get("id") for r in persisted}

    to_persist = [
        f for f in harvested
        if (newest is None or f["ts"] > newest)
        and f["id"] not in existing_ids
    ]
    if not to_persist:
        return harvested, sidecar_etag

    # Re-read fresh right before writing so a concurrent console POST is preserved
    # (never blind-overwrite the shared store) — then prepend the new fires.
    fresh_data, fresh_etag = _load_runs()
    bucket = fresh_data["runs"].setdefault(job_id, [])
    fresh_ids = {r.get("id") for r in bucket}
    for f in reversed(to_persist):  # reversed → final order stays newest-first
        # Idempotent by id; distinct dense fires keep distinct ids and must each
        # persist. Cross-boundary console collapse is _dedupe_by_ts's read-time job.
        if f["id"] in fresh_ids:
            continue
        bucket.insert(0, f)
        fresh_ids.add(f["id"])
    bucket.sort(key=lambda r: r.get("ts", 0), reverse=True)
    if len(bucket) > _RUNS_CAP:
        fresh_data["runs"][job_id] = bucket[:_RUNS_CAP]

    try:
        new_etag = filestore.write_json(RUNS_PATH, fresh_data, fresh_etag)
    except filestore.ConflictError:
        # A concurrent write beat us — skip the persist, still serve the view.
        return harvested, sidecar_etag
    # Reflect the persisted state back into the caller's in-memory sidecar view.
    sidecar_data["runs"] = fresh_data["runs"]
    return harvested, new_etag


async def list_runs(request: web.Request) -> web.Response:
    """Reconcile persisted runs with the full harvested harness fire history.

    Server-side reconciliation (the spec's single source of truth). On read we:
      1. load the cron_runs.json sidecar (console POSTs + previously-backfilled
         harness fires);
      2. ONE-TIME BACKFILL: harvest EVERY fire from the creating session's
         transcript and persist the ones newer than what's already stored (see
         ``_backfill_harness_fires`` — idempotent, so repeat reads don't grow it);
      3. merge persisted + freshly-harvested fires, de-dupe within ~60s, sort
         newest-first, cap at _RUNS_CAP;
      4. FALLBACK ONLY when the transcript can't be found / yields zero fires:
         synthesize the single lastFiredAt entry (``_fired_run_for``) so a job whose
         transcript is missing still shows its one known fire instead of regressing
         to empty.
    The response etag is the post-backfill runs-file etag (or the read etag if no
    write happened). All harness files stay READ-only.
    """
    job_id = request.match_info["job_id"]
    data, etag = _load_runs()

    job = _harness_task(job_id)
    harvested: list[dict] = []
    if job is not None:
        harvested, etag = _backfill_harness_fires(job_id, job, data, etag)

    runs = list(data["runs"].get(job_id, []))
    # Merge freshly-harvested fires (they may include entries newer than what got
    # persisted this round, e.g. on a ConflictError-skipped backfill) by id.
    seen_ids = {r.get("id") for r in runs}
    for f in harvested:
        if f["id"] not in seen_ids:
            runs.append(f)
            seen_ids.add(f["id"])

    if not harvested:
        # Transcript missing / zero fires → keep the single-lastFiredAt synthesis as
        # a strict fallback so the job still shows its one known fire.
        fired = _fired_run_for(job_id, job)
        if fired is not None and not any(
            abs(r.get("ts", 0) - fired["ts"]) < _FIRE_DEDUPE_MS for r in runs
        ):
            runs.append(fired)

    runs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    runs = _dedupe_by_ts(runs)[:_RUNS_CAP]
    return web.json_response({"runs": runs, "etag": etag})


def _harness_task(job_id: str) -> dict | None:
    """Look up the harness task by id (READ-only), or None."""
    try:
        tasks_data, _ = _load()
    except OSError:
        return None
    return next((j for j in tasks_data["tasks"] if j.get("id") == job_id), None)


def _fired_run_for(job_id: str, job: dict | None = None):
    """Synthesize the harness "fired" run entry from lastFiredAt, or None.

    Reads the harness task (READ-only) for a truthy numeric lastFiredAt and, if
    present, best-effort harvests the result text. Never fabricates an outcome.
    Used ONLY as the fallback when the transcript yields no enumerable fires.
    """
    if job is None:
        job = _harness_task(job_id)
    if not job:
        return None
    last_fired = job.get("lastFiredAt")
    if not isinstance(last_fired, (int, float)) or last_fired <= 0:
        return None
    last_fired = int(last_fired)
    return {
        "id": f"fired-{last_fired}",
        "ts": last_fired,
        "outcome": _FIRED_OUTCOME,
        "result": _harvest_fire_result(job, last_fired),
        "source": "harness",
    }


async def append_run(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    body = await read_json_body(request)
    expected_etag = body.get("etag")

    outcome = body.get("outcome", "unknown")
    if outcome not in _VALID_OUTCOMES:
        outcome = "unknown"
    result = str(body.get("result", ""))[:_RESULT_CAP]

    run = {
        "id": secrets.token_hex(4),
        "ts": body.get("ts") or int(time.time() * 1000),
        "outcome": outcome,
        "result": result,
    }
    if body.get("sessionId"):
        run["sessionId"] = body["sessionId"]

    data, current_etag = _load_runs()
    # Prepend so the on-disk order is newest-first too.
    data["runs"].setdefault(job_id, []).insert(0, run)

    try:
        new_etag = filestore.write_json(RUNS_PATH, data, expected_etag or current_etag)
    except filestore.ConflictError as e:
        return web.json_response({"error": "conflict", "message": str(e)}, status=409)

    ws = request.app["ws_manager"]
    await ws.broadcast("cron_run_recorded", {"id": job_id})
    return web.json_response({"run": run, "etag": new_etag}, status=201)
