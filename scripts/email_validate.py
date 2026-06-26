#!/usr/bin/env python3
"""Standalone, re-runnable email validation PRE-FLIGHT (D-055 clause 4).

Run it RIGHT BEFORE enabling the always-on email workers:

    python3 scripts/email_validate.py

It does NOT start the aiohttp server and does NOT start any periodic worker. In
ONE pass it exercises and PROVES the core email functions end-to-end against the
LIVE Microsoft 365 OWA path (the backend manager-outlook-mcp is forced onto the
OWA auth backend by ``email._graph_mcp_params``), asserting the EFFECT at the
destination -- never a tool's success string -- and printing a per-step PASS/FAIL
line plus a final summary. It exits 0 ONLY on full success and non-zero on ANY
failure.

Steps:
  (a) SCAN(+cap+FIFO): one ``_run_guarded_scan`` against a THROWAWAY sidecar,
      confirming the cycle completes AND honors the active-to-do cap -- a store
      pre-seeded at the cap admits 0 new, pre-seeded at cap-8 admits <= 8, after
      the cycle actionable <= cap, and the admitted ones are the OLDEST eligible.
  (b) READ:  ``get_emails`` returns a real inbox list.
  (c) GET:   ``get_email`` on a real id returns a body.
  (d) MARK-READ ROUND-TRIP: flip is_read then RESTORE on a safe message; net
      no-op, re-read at each leg to confirm the destination effect.
  (e) SAVE-DRAFT: create a throwaway draft via the aws-outlook-mcp write path,
      confirm it LANDS in Drafts, then clean it up.
  (f) SOFT-DELETE: the (e) cleanup IS the proof -- ``delete_email(permanent=false)``
      on the throwaway draft leaves Drafts and lands in Deleted Items.

It is IDEMPOTENT: every artifact it creates (a throwaway draft, a flipped
read-state) is cleaned up / restored by the end of the run. It uses ONLY read
tools (via ``email.call_read_tool``) and the gated write seams
(``email._call_graph_write_tool_oneshot`` for mark_email_read / delete_email and
``email._call_owa_write_tool`` for email_draft). It NEVER sends / replies /
forwards and NEVER destructively touches real user mail (no leo11, no real inbox
message) -- only the throwaway artifacts it creates or reversible flips it
restores.

FAIL LOUD: on any step failure it records the FAIL and the run exits non-zero. If
it detects a GRASP 429 / quota_exceeded (via ``email._is_graph_quota_error``) it
STOPS EARLY with a clear message that the backend is NOT OWA, surfaces the RAW
status/body un-truncated, and exits non-zero -- the whole reason this job runs
before flipping the always-on workers on.
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

# Allow `python3 scripts/email_validate.py` to import the server package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.routes import email  # noqa: E402

# Single source of truth for the active-to-do cap: reuse the backend constant
# when it exists (D-055 clause 1 / D-035 lockstep) so this pre-flight can never
# drift from the cap the scanner actually enforces. Fall back to the spec value
# of 50 only when T1's constant has not yet landed.
ACTIVE_TODO_CAP = getattr(email, "EMAIL_ACTIVE_TODO_CAP", 50)

# A safe self-address used for the throwaway draft (never sent).
SELF_ADDRESS = getattr(email, "_MY_EMAIL", "user@example.com")

# Subject prefix for the throwaway draft so it is unmistakably this job's artifact.
_THROWAWAY_SUBJECT_PREFIX = "[email_validate throwaway]"


class QuotaStop(Exception):
    """Raised to STOP the run early when a GRASP 429/quota_exceeded is seen.

    Carries the RAW (un-truncated) body so the operator sees the status code +
    quota code that proves the backend is NOT on OWA.
    """

    def __init__(self, raw: str):
        super().__init__(raw)
        self.raw = raw


class StepResult:
    """One validation step's outcome: name + pass/fail + a human-readable detail."""

    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = bool(passed)
        self.detail = str(detail)

    def render(self) -> str:
        tag = "PASS" if self.passed else "FAIL"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{tag}] {self.name}{suffix}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StepResult(name={self.name!r}, passed={self.passed!r})"


async def run_steps(steps) -> tuple[list[StepResult], int]:
    """Run ``steps`` in order; return (results, exit_code).

    Each step is a zero-arg coroutine function returning a StepResult (or raising).
    A raised exception becomes a FAIL StepResult; a GRASP quota condition (raised
    or returned) STOPS the run early with the RAW body surfaced, and the run still
    exits non-zero. Exit code is 0 ONLY when every executed step passed AND every
    step ran (an early stop is itself a failure).
    """
    results: list[StepResult] = []
    for step in steps:
        try:
            res = await step()
        except QuotaStop as q:
            results.append(StepResult(
                getattr(step, "__name__", "step"),
                False,
                "GRASP QUOTA / 429 detected — the backend is NOT on OWA. Raw body: "
                + q.raw,
            ))
            return results, 1
        except email.EmailMcpError as e:
            raw = str(e)
            if email._is_graph_quota_error(raw):
                results.append(StepResult(
                    getattr(step, "__name__", "step"),
                    False,
                    "GRASP QUOTA / 429 detected — the backend is NOT on OWA. Raw body: "
                    + raw,
                ))
                return results, 1
            results.append(StepResult(getattr(step, "__name__", "step"), False, raw))
            return results, 1
        except Exception as e:  # noqa: BLE001 -- any step error fails loud
            results.append(StepResult(getattr(step, "__name__", "step"), False, repr(e)))
            return results, 1

        if not isinstance(res, StepResult):
            res = StepResult(getattr(step, "__name__", "step"), bool(res), str(res))
        results.append(res)
        if not res.passed:
            return results, 1

    return results, 0


def assert_scan_cap(*, pre_actionable: int, post_actionable: int,
                    admitted_ts: list, eligible_ts: list) -> StepResult:
    """Pure cap+FIFO predicate for step (a) (offline-checkable).

    PASS only when ALL hold:
      - post-cycle actionable count does NOT exceed the cap;
      - the number admitted == min(headroom, #eligible) where headroom =
        max(0, cap - pre_actionable);
      - the admitted items are the OLDEST eligible unread (FIFO): every admitted
        timestamp is <= every NON-admitted eligible timestamp.
    """
    cap = ACTIVE_TODO_CAP
    headroom = max(0, cap - pre_actionable)
    expected_admit = min(headroom, len(eligible_ts))

    if post_actionable > cap:
        return StepResult(
            "scan-cap", False,
            f"post-cycle actionable {post_actionable} EXCEEDS the cap {cap}",
        )
    if len(admitted_ts) != expected_admit:
        return StepResult(
            "scan-cap", False,
            f"admitted {len(admitted_ts)} but headroom allowed {expected_admit} "
            f"(pre={pre_actionable}, eligible={len(eligible_ts)}, cap={cap})",
        )

    # FIFO: the admitted set must be the oldest eligible. Compare against the
    # eligible items NOT admitted -- every admitted ts must be <= every held ts.
    admitted_sorted = sorted(admitted_ts)
    eligible_sorted = sorted(eligible_ts)
    oldest_expected = eligible_sorted[:expected_admit]
    if admitted_sorted != oldest_expected:
        return StepResult(
            "scan-cap", False,
            "admitted items are NOT the OLDEST eligible (FIFO inversion): "
            f"admitted={admitted_sorted}, expected oldest={oldest_expected}",
        )

    return StepResult(
        "scan-cap", True,
        f"pre={pre_actionable} admitted={len(admitted_ts)} post={post_actionable} "
        f"(cap {cap}, oldest-first)",
    )


async def mark_read_roundtrip(*, message_id: str, get_email=None, write_tool=None) -> StepResult:
    """Flip is_read on ``message_id`` then RESTORE it; net no-op, effect-verified.

    Re-reads at each leg (not a success string): the flip must be OBSERVED in the
    re-read, and the final state must equal the original. ``get_email`` /
    ``write_tool`` default to the live email seams but are injectable for tests.
    """
    if get_email is None:
        async def get_email(mid):  # noqa: ANN001
            return await email.call_read_tool("get_email", {"message_id": mid})
    if write_tool is None:
        async def write_tool(name, args):  # noqa: ANN001
            return await email._call_graph_write_tool_oneshot(name, args)

    def _is_read(payload) -> bool:
        msg = payload.get("email") if isinstance(payload, dict) else None
        if not isinstance(msg, dict):
            msg = payload if isinstance(payload, dict) else {}
        return bool(msg.get("is_read"))

    original = _is_read(await get_email(message_id))
    flipped_target = not original

    # Flip.
    await write_tool("mark_email_read", {
        "emails": {message_id: None}, "is_read": flipped_target,
    })
    after_flip = _is_read(await get_email(message_id))
    if after_flip != flipped_target:
        # Restore best-effort before failing so we never leave a stuck flip.
        try:
            await write_tool("mark_email_read", {
                "emails": {message_id: None}, "is_read": original,
            })
        except Exception:  # noqa: BLE001
            pass
        return StepResult(
            "mark-read-roundtrip", False,
            f"flip not observed: requested is_read={flipped_target} but re-read "
            f"shows is_read={after_flip} (success string is not proof)",
        )

    # Restore.
    await write_tool("mark_email_read", {
        "emails": {message_id: None}, "is_read": original,
    })
    after_restore = _is_read(await get_email(message_id))
    if after_restore != original:
        return StepResult(
            "mark-read-roundtrip", False,
            f"restore failed: original is_read={original} but re-read shows "
            f"is_read={after_restore} (mailbox left in a changed state)",
        )

    return StepResult(
        "mark-read-roundtrip", True,
        f"flip {original}->{flipped_target} then restore -> {original} (net no-op)",
    )


async def save_draft_roundtrip(*, subject: str, body: str, to: list,
                               owa_write=None, get_emails=None, write_tool=None) -> StepResult:
    """Create a THROWAWAY draft, confirm it LANDS in Drafts, then soft-delete it.

    Proves the EFFECT at the destination twice: the draft must be FOUND in Drafts
    after creation, and must be GONE from Drafts after the soft delete (which moves
    it to Deleted Items). NEVER sends. Idempotent: leaves no residue. The seams are
    injectable for offline tests.
    """
    if owa_write is None:
        async def owa_write(name, args):  # noqa: ANN001
            return await email._call_owa_write_tool(name, args)
    if get_emails is None:
        async def get_emails(folder, limit):  # noqa: ANN001
            return await email.call_read_tool("get_emails", {"folder": folder, "limit": limit})
    if write_tool is None:
        async def write_tool(name, args):  # noqa: ANN001
            return await email._call_graph_write_tool_oneshot(name, args)

    # CREATE the draft (aws-outlook-mcp write path). Never a send/reply/forward.
    create_result = await owa_write("email_draft", {
        "operation": "create",
        "to": list(to),
        "subject": subject,
        "body": body,
    })

    # The create-returned id is only a MATCHING HINT, never the delete target: the
    # aws-outlook email_draft create returns an EWS-form draftId, but Graph's
    # delete_email needs the GRAPH itemId that the Drafts listing exposes. Those id
    # FORMS diverge, so deleting by the create-returned id 400s (move fails) and
    # leaves residue. We therefore resolve the AUTHORITATIVE delete id from the
    # message we actually find in Drafts, never from create_result.
    create_id = None
    if isinstance(create_result, dict):
        create_id = create_result.get("id")
        content = create_result.get("content")
        if not create_id and isinstance(content, dict):
            create_id = content.get("draftId")

    # CONFIRM it LANDS in Drafts (effect, not the success string).
    drafts_payload = await get_emails("drafts", 50)
    drafts = email._unwrap_list(drafts_payload, "emails", "messages", "items", "value")

    def _match(msg) -> bool:
        if not isinstance(msg, dict):
            return False
        if create_id and str(msg.get("id", "")) == str(create_id):
            return True
        return str(msg.get("subject", "")) == subject

    landed = next((m for m in drafts if _match(m)), None)
    if landed is None:
        return StepResult(
            "save-draft", False,
            "the throwaway draft did NOT land in Drafts after create "
            "(a 'saved' return is not proof)",
        )
    # AUTHORITATIVE delete target: the itemId carried by the draft AS IT LIVES IN
    # Drafts (what Graph delete_email actually accepts), NOT the create-returned id.
    draft_id = str(landed.get("id", ""))
    if not draft_id:
        return StepResult(
            "save-draft", False,
            "draft landed but no message id resolvable to clean it up",
        )

    # CLEAN UP / SOFT-DELETE: move the throwaway draft to Deleted Items. This IS
    # the proof for step (f): delete_email(permanent=false) must leave Drafts.
    await write_tool("delete_email", {
        "emails": {draft_id: subject}, "permanent": False,
    })

    # VERIFY THE REAL DRAFT LEFT DRAFTS (effect at destination, not a never-present
    # wrong-id absence): the draft we confirmed landed must be GONE -- matched the
    # SAME way it landed (by its itemId, with subject as the secondary check), so a
    # residue under EITHER id form or the subject is caught.
    drafts_after_payload = await get_emails("drafts", 50)
    drafts_after = email._unwrap_list(
        drafts_after_payload, "emails", "messages", "items", "value")
    still_there = any(
        isinstance(m, dict)
        and (str(m.get("id", "")) == str(draft_id)
             or str(m.get("subject", "")) == subject)
        for m in drafts_after
    )
    if still_there:
        return StepResult(
            "save-draft", False,
            "soft-delete did NOT remove the throwaway draft from Drafts "
            "(residue left behind)",
        )

    return StepResult(
        "save-draft", True,
        f"throwaway draft {draft_id} landed in Drafts then soft-deleted (no residue)",
    )


# ─── Live step wrappers (used by main(); each FAILS LOUD on a quota condition) ──

# Shared state threaded between live steps within one run.
_LIVE = {"inbox_msg_id": None}


def _raise_if_quota(payload) -> None:
    """If a tool payload smells like a GRASP quota error, STOP the run early."""
    if email._is_graph_quota_error(payload):
        raise QuotaStop(str(payload))


async def _step_scan(app) -> StepResult:
    """(a) One guarded scan against a THROWAWAY sidecar, cap+FIFO honored.

    Runs three real scan cycles against an isolated temp store with stubbed Graph
    reads so we can PROVE the cap+FIFO behavior deterministically without touching
    the real sidecar or sending any mail: pre-seeded at the cap admits 0, and
    pre-seeded at cap-8 admits the 8 OLDEST eligible unread.
    """
    import json
    import tempfile

    cap = ACTIVE_TODO_CAP
    real_path = email.EMAIL_PATH
    tmp = Path(tempfile.mkdtemp(prefix="email_validate_")) / "email_threads.json"

    # 12 eligible unread, returned NEWEST-FIRST (as Graph does). ts day == n, so a
    # larger n is a newer message; MSG-1 is the OLDEST.
    raws = [{
        "id": f"VAL-MSG-{n}",
        "subject": f"validate subject {n}",
        "from": {"name": f"Sender {n}", "email": f"s{n}@example.com"},
        "received": f"2026-06-{n:02d}T10:00:00Z",
        "is_read": False,
        "preview": f"preview {n}",
    } for n in range(12, 0, -1)]

    async def stub_read(name, arguments):
        if name == "get_emails":
            return {"emails": raws, "count": len(raws), "folder": "inbox"}
        if name == "get_email":
            return {"email": {"id": arguments.get("message_id"), "body": "body"}}
        return {}

    def _seed(items):
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"items": items}), encoding="utf-8")

    def _existing(n):
        return {
            "id": f"val-existing-{n}", "messageId": f"VAL-EXIST-{n}",
            "conversationId": f"VAL-EXIST-{n}", "status": "needs-review",
        }

    orig_read = email.call_read_tool
    try:
        email.EMAIL_PATH = tmp
        email.call_read_tool = stub_read
        # Reset the loop-bound scan guard so _run_guarded_scan builds it fresh.
        email._scan_in_progress_lock = None
        email._scan_wake_event = None

        # Pre-seed at cap-8: expect at most 8 admitted, the OLDEST eligible.
        pre = cap - 8
        _seed([_existing(i) for i in range(pre)])
        completed = await email._run_guarded_scan(app)
        if not completed:
            return StepResult("scan-cap", False, "_run_guarded_scan did not run a cycle")

        saved = json.loads(tmp.read_text(encoding="utf-8"))["items"]
        post = email._count_actionable(saved)
        admitted = [
            it for it in saved
            if str(it.get("messageId", "")).startswith("VAL-MSG-")
        ]
        admitted_ts = [it.get("ts") for it in admitted]
        eligible_ts = [email._graph_ts_ms(r) for r in raws]
        below = assert_scan_cap(
            pre_actionable=pre, post_actionable=post,
            admitted_ts=admitted_ts, eligible_ts=eligible_ts,
        )
        if not below.passed:
            return below

        # Pre-seed AT the cap: expect ZERO admitted.
        _seed([_existing(i) for i in range(cap)])
        email._scan_in_progress_lock = None
        email._scan_wake_event = None
        await email._run_guarded_scan(app)
        saved_full = json.loads(tmp.read_text(encoding="utf-8"))["items"]
        post_full = email._count_actionable(saved_full)
        admitted_full = [
            it for it in saved_full
            if str(it.get("messageId", "")).startswith("VAL-MSG-")
        ]
        at_cap = assert_scan_cap(
            pre_actionable=cap, post_actionable=post_full,
            admitted_ts=[it.get("ts") for it in admitted_full], eligible_ts=eligible_ts,
        )
        if not at_cap.passed:
            return at_cap
        if admitted_full:
            return StepResult(
                "scan-cap", False,
                f"a store at the cap admitted {len(admitted_full)} new items (expected 0)",
            )

        return StepResult(
            "scan-cap", True,
            f"guarded scan honored the cap {cap}: cap-8 admitted {len(admitted)} "
            "OLDEST-first, at-cap admitted 0",
        )
    finally:
        email.EMAIL_PATH = real_path
        email.call_read_tool = orig_read
        email._scan_in_progress_lock = None
        email._scan_wake_event = None
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except OSError:
            pass


async def _step_read() -> StepResult:
    """(b) get_emails returns a real inbox list; stash an id for step (c)."""
    payload = await email.call_read_tool("get_emails", {
        "folder": "inbox", "limit": 10, "unread_only": False,
    })
    _raise_if_quota(payload)
    msgs = email._unwrap_list(payload, "emails", "messages", "items", "value")
    if not isinstance(msgs, list) or not msgs:
        return StepResult("read-inbox", False,
                          "get_emails returned no inbox messages (cannot validate read)")
    first = next((m for m in msgs if isinstance(m, dict) and m.get("id")), None)
    if first is None:
        return StepResult("read-inbox", False, "no inbox message carried an id")
    _LIVE["inbox_msg_id"] = str(first.get("id"))
    return StepResult("read-inbox", True, f"get_emails returned {len(msgs)} inbox message(s)")


async def _step_get() -> StepResult:
    """(c) get_email on the id from (b) returns a body."""
    mid = _LIVE.get("inbox_msg_id")
    if not mid:
        return StepResult("get-email", False, "no inbox message id available from the read step")
    payload = await email.call_read_tool("get_email", {"message_id": mid})
    _raise_if_quota(payload)
    body = email._extract_email_body(payload)
    if not body:
        return StepResult("get-email", False, f"get_email({mid}) returned no body")
    return StepResult("get-email", True, f"get_email returned a {len(body)}-char body")


async def _step_mark_read() -> StepResult:
    """(d) mark-read flip+restore on the safe inbox message from (b)."""
    mid = _LIVE.get("inbox_msg_id")
    if not mid:
        return StepResult("mark-read-roundtrip", False, "no safe message id available")
    return await mark_read_roundtrip(message_id=mid)


async def _step_save_draft() -> StepResult:
    """(e)+(f) save a throwaway draft, confirm it lands, then soft-delete it."""
    subject = f"{_THROWAWAY_SUBJECT_PREFIX} {secrets.token_hex(4)}"
    return await save_draft_roundtrip(
        subject=subject,
        body="Throwaway draft created by scripts/email_validate.py — safe to delete.",
        to=[SELF_ADDRESS],
    )


async def main() -> int:
    """Run all steps, print PASS/FAIL + a summary, return the process exit code."""
    print("email_validate: pre-flight for the always-on email workers (live OWA path).")
    print(f"email_validate: active-to-do cap = {ACTIVE_TODO_CAP}")

    class _NoopWS:
        async def broadcast(self, *a, **k):
            pass

    app = {"ws_manager": _NoopWS()}

    steps = [
        lambda: _step_scan(app),
        _step_read,
        _step_get,
        _step_mark_read,
        _step_save_draft,
    ]
    # run_steps keys the step name off __name__; give the lambda a name.
    steps[0].__name__ = "_step_scan"

    results, code = await run_steps(steps)

    print("\n=== email_validate results ===")
    for r in results:
        print(r.render())
    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} step(s) passed; "
          f"{'ALL GREEN' if code == 0 else 'FAILED'} (exit {code}).")
    if code != 0:
        print("email_validate: do NOT enable CLAUDE_WEB_EMAIL_WORKERS until this passes.")
    return code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    finally:
        # Never leave a persistent MCP subprocess alive after the pre-flight.
        try:
            asyncio.run(email._disconnect_graph_mcp())
        except Exception:  # noqa: BLE001
            pass
        try:
            asyncio.run(email._disconnect_owa_write())
        except Exception:  # noqa: BLE001
            pass
        try:
            asyncio.run(email._disconnect_mcp())
        except Exception:  # noqa: BLE001
            pass
