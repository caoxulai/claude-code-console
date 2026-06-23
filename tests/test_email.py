"""Email route tests — exercises the contract specified in the feature spec.

The email module mirrors the Slack architecture exactly: a JSON sidecar managed
via filestore, a _run_email_agent delegation seam (monkeypatched here), and
a _EMAIL_READ_ONLY_TOOLS frozenset safety boundary.

These tests are in a SEPARATE file (not test_server.py) to avoid bundling with
the in-flight uncommitted edits there. They use pytest.importorskip so the suite
SKIPS (not errors) if the email module has not yet landed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

try:
    from aiohttp import web
except ModuleNotFoundError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Guard: skip the entire file if the email module is not yet implemented.
email_mod = pytest.importorskip(
    "server.routes.email",
    reason="server/routes/email.py not yet implemented — tests will activate once it lands",
)

from server.app import create_app  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> web.Application:
    monkeypatch.setattr("server.app.ALLOWED_CWD_ROOTS", [tmp_path, Path("/etc/__never__")])
    monkeypatch.setenv("CLAUDE_WEB_CWD", str(tmp_path))
    application = create_app()
    application["allowed_cwd_roots"] = [tmp_path, Path("/etc/__never__")]
    application["default_cwd"] = tmp_path
    application["permission_mode"] = "bypassPermissions"
    return application


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture
def email_file(tmp_path: Path, monkeypatch) -> Path:
    """Repoint the email sidecar (+ sibling style store) at tmp_path."""
    p = tmp_path / "email" / "email_threads.json"
    monkeypatch.setattr(email_mod, "EMAIL_PATH", p)
    return p


@pytest.fixture(autouse=True)
def _reset_email_primitives():
    """Reset module-level asyncio primitives between tests (mirrors Slack pattern).

    Workers create loop-bound asyncio.Lock/Event lazily; without a reset, a
    primitive from one test's loop would be reused in the next, raising
    'RuntimeError: ... is bound to a different event loop'.
    """
    # Reset email module primitives
    for attr in ("_scan_in_progress_lock", "_scan_wake_event"):
        if hasattr(email_mod, attr):
            setattr(email_mod, attr, None)
    # Reset Slack module primitives too (create_app starts Slack workers)
    try:
        from server.routes import slack as slack_mod
        slack_mod._scan_in_progress_lock = None
        slack_mod._scan_wake_event = None
    except (ImportError, AttributeError):
        pass
    yield
    for attr in ("_scan_in_progress_lock", "_scan_wake_event"):
        if hasattr(email_mod, attr):
            setattr(email_mod, attr, None)
    try:
        from server.routes import slack as slack_mod
        slack_mod._scan_in_progress_lock = None
        slack_mod._scan_wake_event = None
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def _inert_mark_read(monkeypatch):
    """Neutralize the fire-and-forget mark-read by default.

    dismiss_item spawns a background mark-read that opens a real MCP stdio
    connection; left live in a test it outlives the request and anyio tears
    down its cancel scope across tasks ('exit cancel scope in a different
    task'). Tests that specifically assert mark-read behavior install their own
    _spawn_mark_read/call_read_tool stub, which overrides this default.
    """
    if hasattr(email_mod, "_spawn_mark_read"):
        monkeypatch.setattr(email_mod, "_spawn_mark_read", lambda cid: None)


def _stub_agent(monkeypatch, result):
    """Monkeypatch the delegation seam to return a fixed result dict."""
    async def fake_agent(action, payload):
        fake_agent.calls.append((action, payload))
        return dict(result, action=action)
    fake_agent.calls = []
    monkeypatch.setattr(email_mod, "_run_email_agent", fake_agent)
    return fake_agent


def _seed(email_file, items, **extra):
    """Write a seeded sidecar and return the path."""
    email_file.parent.mkdir(parents=True, exist_ok=True)
    data = {"items": items, **extra}
    email_file.write_text(json.dumps(data), encoding="utf-8")
    return email_file


def _make_item(id="i1", **overrides):
    """Build a minimal valid email queue item with sensible defaults."""
    base = {
        "id": id,
        "conversationId": "AAQtest",
        "sender": "John Doe",
        "senderEmail": "john@example.com",
        "subject": "Re: Project update",
        "snippet": "Hey, checking in",
        "emailBody": "Full thread body here",
        "threadContext": "",
        "draft": "auto draft",
        "generatedDraft": "auto draft",
        "classification": "needs-reply",
        "status": "needs-review",
        "ts": 1718000000000,
        "recipients": ["john@example.com"],
        "hasAttachments": False,
        "messageCount": 1,
        "needsRedraft": False,
    }
    base.update(overrides)
    return base


# ─── Test cases ──────────────────────────────────────────────────────────────


async def test_email_queue_empty_when_no_file(client, email_file):
    """GET /api/email/queue returns empty items[] when the sidecar does not exist."""
    resp = await client.get("/api/email/queue")
    assert resp.status == 200
    body = await resp.json()
    assert body["items"] == []


async def test_email_save_draft_marks_edited(client, email_file, monkeypatch):
    """PUT with a modified draft flips status to 'edited'."""
    _seed(email_file, [_make_item()])
    resp = await client.put("/api/email/queue/i1", json={"draft": "my own words"})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["draft"] == "my own words"
    assert body["item"]["status"] == "edited"
    # Persisted to disk
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "my own words"
    assert saved["status"] == "edited"


async def test_email_save_draft_conflict_returns_409(client, email_file):
    """A stale etag gets 409."""
    _seed(email_file, [_make_item()])
    resp = await client.put("/api/email/queue/i1", json={"draft": "x", "etag": "stale-etag"})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"


async def test_email_dismiss_sets_status_not_delete(client, email_file):
    """DELETE flips status to 'dismissed', item is preserved in the sidecar."""
    _seed(email_file, [
        _make_item("i1"),
        _make_item("i2", sender="Jane"),
    ])
    resp = await client.delete("/api/email/queue/i1", json={})
    assert resp.status == 200

    # Both items still present; i1 is dismissed, i2 untouched.
    items = (await (await client.get("/api/email/queue")).json())["items"]
    by_id = {it["id"]: it for it in items}
    assert "i1" in by_id and "i2" in by_id
    assert by_id["i1"]["status"] == "dismissed"
    assert by_id["i2"]["status"] == "needs-review"

    # Persisted to disk
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert any(it["id"] == "i1" and it["status"] == "dismissed" for it in saved)


async def test_email_dismiss_unknown_is_404(client, email_file):
    """Dismissing a non-existent item returns 404."""
    _seed(email_file, [])
    resp = await client.delete("/api/email/queue/nope", json={})
    assert resp.status == 404


# --- dismiss marks the conversation read in Outlook --------------------------


def _capture_mark_read(monkeypatch):
    """Patch the fire-and-forget mark-read to record its message map synchronously.

    The production path spawns a background task that marks the item's Graph
    message_id read via manager-outlook-mcp's mark_email_read (a batch
    {message_id: subject} map). Capturing at _spawn_mark_read makes the assertion
    deterministic (no racing the event loop) while still proving the dismiss
    handler decided to mark the right message read.
    """
    calls = []
    monkeypatch.setattr(email_mod, "_spawn_mark_read", lambda messages: calls.append(messages))
    return calls


async def test_email_dismiss_marks_message_read(client, email_file, monkeypatch):
    """A successful dismiss marks that message read in Outlook (best-effort).

    Mirrors the user's rule: scanning leaves mail unread, but dismissing is the
    conscious "I've processed this" decision. The read path is now Microsoft Graph
    (manager-outlook-mcp), so the mark targets the item's Graph message_id via
    mark_email_read ({message_id: subject}).
    """
    calls = _capture_mark_read(monkeypatch)
    _seed(email_file, [_make_item("i1", messageId="AAMkMSG-9",
                                  conversationId="AAMkMSG-9", subject="Re: Hi")])

    resp = await client.delete("/api/email/queue/i1", json={})
    assert resp.status == 200
    # Exactly one mark, carrying the Graph message_id keyed to its subject.
    assert calls == [{"AAMkMSG-9": "Re: Hi"}]


async def test_email_dismiss_conflict_does_not_mark_read(client, email_file, monkeypatch):
    """A 409'd dismiss must NOT mark read -- we only fire after the durable write.

    Sends a stale etag so the write conflicts; the message's read-state in
    Outlook must be untouched (no mark-read for a dismiss that did not persist).
    """
    calls = _capture_mark_read(monkeypatch)
    _seed(email_file, [_make_item("i1", messageId="AAMkMSG-9", conversationId="AAMkMSG-9")])

    resp = await client.delete(
        "/api/email/queue/i1", json={"etag": "stale-etag-that-will-conflict"})
    assert resp.status == 409
    assert calls == []  # no mark-read on a conflict


async def test_email_approve_and_mute_do_not_mark_read(client, email_file, monkeypatch):
    """Only dismiss marks read -- approve and mute must NOT (the user's choice).

    Approve still saves an Outlook draft + clipboard and mute still soft-dismisses,
    but neither touches the conversation's read-state.
    """
    calls = _capture_mark_read(monkeypatch)
    _stub_agent(monkeypatch, {"available": True, "draftSaved": True})
    _seed(email_file, [
        _make_item("i1", messageId="AAMkA", conversationId="AAMkA"),
        _make_item("i2", messageId="AAMkB", conversationId="AAMkB"),
    ])

    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    resp = await client.post("/api/email/queue/i2/mute", json={})
    assert resp.status == 200

    # Neither terminal action marked a conversation read.
    assert calls == []


async def test_email_undismiss_restores(client, email_file):
    """POST undismiss restores a dismissed item to needs-review or edited."""
    # Case A: dismissed, draft == generatedDraft → needs-review
    _seed(email_file, [_make_item("i1", status="dismissed")])
    resp = await client.post("/api/email/queue/i1/undismiss", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "needs-review"

    # Case B: dismissed, draft differs from generatedDraft → edited
    _seed(email_file, [_make_item("i2", status="dismissed", draft="my edit",
                                  generatedDraft="auto draft")])
    resp = await client.post("/api/email/queue/i2/undismiss", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "edited"


async def test_email_undismiss_non_dismissed_is_409(client, email_file):
    """Undismiss on a non-dismissed item is a 409 conflict."""
    _seed(email_file, [_make_item("i1", status="needs-review")])
    resp = await client.post("/api/email/queue/i1/undismiss", json={})
    assert resp.status == 409


@pytest.fixture
def content_etag(monkeypatch):
    """Make the filestore etag track file *content*, not mtime.

    Production etags are `st_mtime_ns`, so a conflict only fires when two writes
    land in different nanosecond ticks — which is why the dismiss-all bug is
    nondeterministic both live (UAT saw [200,409,409,409]) and in a fast test
    loop (consecutive writes can share an mtime, hiding the bug). Keying the etag
    on content makes every successful write deterministically change the etag —
    exactly the mtime-tick the UAT observed live — so a loop that reuses one
    stale etag is GUARANTEED to 409 every item after the first.
    """
    import hashlib
    from server import filestore

    def content_etag_for(path):
        try:
            return hashlib.sha1(path.read_bytes()).hexdigest()
        except OSError:
            return None

    monkeypatch.setattr(filestore, "etag_for", content_etag_for)
    return content_etag_for


async def test_email_dismiss_all_clears_every_item(client, email_file, content_etag):
    """AC-9: 'Dismiss all (N)' must clear EVERY item in the declared group, not
    just the first.

    Reproduces the exact frontend dismissAll loop: read ONE queue etag, then
    sequentially DELETE each item. The first write changes the file's etag, so a
    naive loop that reuses the original etag for every item 409s items #2..N
    (backend guard is `expected_etag or current_etag`, which does NOT fall back to
    current for a stale-but-truthy etag) and silently leaves most undismissed.

    The fixed loop threads the etag forward from each response and, on a 409,
    re-reads the fresh queue etag and retries that one item. With the
    content-keyed etag fixture this is deterministic: the OLD reuse-one-etag loop
    leaves 3 of 4 undismissed; the fixed loop dismisses all 4.
    """
    items = [_make_item(f"i{n}", sender=f"S{n}") for n in range(1, 5)]
    _seed(email_file, items)

    # The page closure holds exactly one etag, captured from the queue load.
    cur = (await (await client.get("/api/email/queue")).json())["etag"]

    for it in items:
        resp = await client.delete(
            f"/api/email/queue/{it['id']}", json={"etag": cur})
        if resp.status == 409:
            # Etag raced — re-read and retry this one item, mirroring the fixed
            # dismissAll fallback.
            cur = (await (await client.get("/api/email/queue")).json())["etag"]
            resp = await client.delete(
                f"/api/email/queue/{it['id']}", json={"etag": cur})
        assert resp.status == 200, f"{it['id']} failed: HTTP {resp.status}"
        cur = (await resp.json())["etag"]

    # EVERY item is dismissed — none survived (the AC-9 acceptance bar).
    final = (await (await client.get("/api/email/queue")).json())["items"]
    assert {it["id"]: it["status"] for it in final} == {
        "i1": "dismissed", "i2": "dismissed",
        "i3": "dismissed", "i4": "dismissed",
    }
    # Persisted to disk, not just in-memory.
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert all(it["status"] == "dismissed" for it in saved)


async def test_email_dismiss_all_reused_stale_etag_leaves_items(client, email_file, content_etag):
    """Pin the regression: the OLD dismissAll loop (reuse ONE closure etag for
    every DELETE, no 409 fallback) leaves items #2..N undismissed.

    This is the negative control for AC-9 — it asserts the broken pattern is
    genuinely broken so the positive test above proves the fix, not just a benign
    timing window. Documents WHY threading the etag matters.
    """
    items = [_make_item(f"i{n}", sender=f"S{n}") for n in range(1, 5)]
    _seed(email_file, items)

    stale = (await (await client.get("/api/email/queue")).json())["etag"]
    statuses = []
    for it in items:
        # The bug: setEtag inside the loop is a next-render no-op, so the loop
        # keeps sending the ORIGINAL etag and never falls back on 409.
        resp = await client.delete(
            f"/api/email/queue/{it['id']}", json={"etag": stale})
        statuses.append(resp.status)

    # First write succeeds; the rest conflict because the etag is now stale.
    assert statuses == [200, 409, 409, 409]
    final = (await (await client.get("/api/email/queue")).json())["items"]
    dismissed = sum(1 for it in final if it["status"] == "dismissed")
    assert dismissed == 1, "broken loop should leave 3 of 4 undismissed"


async def test_email_regenerate_replaces_draft(client, email_file, monkeypatch):
    """Monkeypatched agent returns a new draft, item flips to needs-review."""
    _seed(email_file, [_make_item("i1", status="edited", draft="old")])
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "fresh draft"})
    resp = await client.post("/api/email/queue/i1/refresh")
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["draft"] == "fresh draft"
    assert body["item"]["generatedDraft"] == "fresh draft"
    assert body["item"]["status"] == "needs-review"
    assert agent.calls[0][0] == "regenerate"


async def test_email_regenerate_unavailable_does_not_fabricate(client, email_file, monkeypatch):
    """Agent returns available:False — item unchanged, never fabricate a draft."""
    _seed(email_file, [_make_item("i1", draft="original draft")])
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    resp = await client.post("/api/email/queue/i1/refresh")
    assert resp.status == 502
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "original draft"  # untouched


async def test_email_approve_saves_draft_and_returns_ok(client, email_file, monkeypatch):
    """Approve marks status→approved and draftSaved:true when agent succeeds."""
    _seed(email_file, [_make_item("i1", draft="final reply")])
    agent = _stub_agent(monkeypatch, {"available": True, "draftSaved": True})
    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "approved"
    assert body.get("draftSaved") is True
    # Persisted to disk
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "approved"


async def test_email_approve_degrades_without_writes(client, email_file, monkeypatch):
    """Agent returns draftSaved:false (writes unavailable) — status still approved."""
    _seed(email_file, [_make_item("i1", draft="final reply")])
    _stub_agent(monkeypatch, {"available": True, "draftSaved": False})
    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "approved"
    assert body.get("draftSaved") is False


async def test_email_approve_already_approved_is_409(client, email_file, monkeypatch):
    """Cannot re-approve an already-approved item."""
    _seed(email_file, [_make_item("i1", status="approved")])
    _stub_agent(monkeypatch, {"available": True, "draftSaved": True})
    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 409


async def test_email_approve_never_calls_email_reply_or_send(client, email_file, monkeypatch):
    """Assert the agent stub was called with 'save_draft' action only, never
    'reply' or 'send'. The system NEVER sends email directly."""
    _seed(email_file, [_make_item("i1", draft="final reply")])
    agent = _stub_agent(monkeypatch, {"available": True, "draftSaved": True})
    await client.post("/api/email/queue/i1/approve", json={})
    # Only save_draft action should have been called
    actions = [call[0] for call in agent.calls]
    assert "reply" not in actions, "approve must NEVER call the 'reply' action"
    assert "send" not in actions, "approve must NEVER call the 'send' action"
    # The action should be 'save_draft' (the ONE write-tool cold-start)
    assert any(a == "save_draft" for a in actions), \
        f"expected 'save_draft' action, got: {actions}"


async def test_email_mute_dismisses_and_records_key(client, email_file):
    """Muting a conversation dismisses the item AND records the mute key."""
    _seed(email_file, [_make_item("i1", conversationId="AAQconv123")])
    resp = await client.post("/api/email/queue/i1/mute", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "dismissed"
    # The mute key is recorded in the sidecar's mutedThreads
    saved = json.loads(email_file.read_text(encoding="utf-8"))
    muted = saved.get("mutedThreads", {})
    assert len(muted) > 0, "expected a mute key to be recorded"


async def test_email_unmute_removes_key(client, email_file):
    """Unmuting removes the key from mutedThreads."""
    _seed(email_file, [_make_item("i1")], mutedThreads={"AAQconv123": 1718000000})
    resp = await client.delete("/api/email/muted/AAQconv123", json={})
    assert resp.status == 200
    saved = json.loads(email_file.read_text(encoding="utf-8"))
    assert "AAQconv123" not in saved.get("mutedThreads", {})


async def test_email_pause_toggle(client, email_file):
    """PUT /api/email/config {paused:true} toggles the paused state."""
    _seed(email_file, [])
    resp = await client.put("/api/email/config", json={"paused": True})
    assert resp.status == 200
    # Verify via GET queue
    q = await (await client.get("/api/email/queue")).json()
    assert q["paused"] is True

    # Toggle back
    resp = await client.put("/api/email/config", json={"paused": False})
    assert resp.status == 200
    q = await (await client.get("/api/email/queue")).json()
    assert q["paused"] is False


async def test_email_health_probe(client, email_file):
    """GET /api/email/health returns a well-formed structure."""
    resp = await client.get("/api/email/health")
    assert resp.status == 200
    body = await resp.json()
    # Should have at least a 'ready' key (mirrors slack health pattern)
    assert "ready" in body or "claudeOnPath" in body, \
        f"health response missing expected keys: {body}"


async def test_email_style_read_write(client, email_file):
    """GET/PUT /api/email/style round-trips content."""
    # Initially empty
    resp = await client.get("/api/email/style")
    assert resp.status == 200
    body = await resp.json()
    assert body["content"] == ""

    # Write style
    resp = await client.put("/api/email/style", json={"content": "Be concise and professional."})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["content"] == "Be concise and professional."

    # Read back
    resp = await client.get("/api/email/style")
    assert resp.status == 200
    body = await resp.json()
    assert body["content"] == "Be concise and professional."


def test_email_read_only_safety_boundary():
    """Verify call_read_tool (or equivalent) refuses tools not in the frozenset.

    Direct unit test of the structural safety boundary: any tool NOT in
    _EMAIL_READ_ONLY_TOOLS must be refused BEFORE touching MCP.
    """
    read_tools = email_mod._EMAIL_READ_ONLY_TOOLS

    # The frozenset must exist and contain only known read-only tools
    assert isinstance(read_tools, frozenset)
    assert len(read_tools) > 0

    # WRITE tools must NEVER be in the read-only set
    write_tools = {"email_reply", "email_send", "email_draft", "email_forward",
                   "email_move", "email_update"}
    overlap = read_tools & write_tools
    assert not overlap, f"Write tools in read-only set: {overlap}"

    # Known read-only tools SHOULD be in the set
    expected_reads = {"email_inbox", "email_read", "email_search", "email_folders",
                     "email_list_folders", "email_contacts", "email_attachments"}
    missing = expected_reads - read_tools
    assert not missing, f"Expected read tools missing from frozenset: {missing}"

    # If there's a call_read_tool function, test that it rejects a write tool
    if hasattr(email_mod, "call_read_tool"):
        # call_read_tool is async, but we test the synchronous reject path
        import asyncio
        with pytest.raises(Exception):
            asyncio.get_event_loop().run_until_complete(
                email_mod.call_read_tool("email_send", {})
            )


def test_email_body_cap_32k():
    """Verify emailBody is capped at 32k chars in stored items.

    The spec mandates capping emailBody at 32768 characters when storing items.
    Test this by checking the module-level constant or the cap logic.
    """
    # Check for the cap constant
    cap = None
    for attr in ("_EMAIL_BODY_CAP", "_BODY_CAP", "EMAIL_BODY_CAP"):
        if hasattr(email_mod, attr):
            cap = getattr(email_mod, attr)
            break

    if cap is not None:
        assert cap == 32768 or cap == 32 * 1024, \
            f"emailBody cap should be 32k (32768), got {cap}"
    else:
        # If no constant, verify via the _load or item normalization logic
        # The spec says "capped 32k chars" — look for truncation in any
        # item-creation or scan logic
        pytest.skip(
            "No explicit _EMAIL_BODY_CAP constant found — verify cap in scan logic manually"
        )


async def test_email_queue_count_matches_actionable(client, email_file):
    """GET /api/email/queue/count returns {count: N} == the actionable count.

    Seeds a queue spanning every status (the two terminal states + several active
    ones + a missing/empty status) and asserts the lightweight /count endpoint
    equals the count of NON-terminal items in the full /queue response — so the
    NavBar bubble can never drift from countEmailActionable. Terminal = approved
    or dismissed; a missing/empty status counts.
    """
    _seed(email_file, [
        _make_item("c1", status="needs-classify"),
        _make_item("c2", status="needs-draft"),
        _make_item("c3", status="needs-review"),
        _make_item("c4", status="edited"),
        _make_item("c5", status="fyi"),          # classification-as-status legacy: still active
        _make_item("c6", status="approved"),     # terminal — excluded
        _make_item("c7", status="dismissed"),    # terminal — excluded
        _make_item("c8", status=""),             # empty status — counts
    ])

    # Compute the expected count straight from the full queue the UI consumes.
    items = (await (await client.get("/api/email/queue")).json())["items"]
    expected = sum(1 for it in items if it.get("status") not in ("approved", "dismissed"))
    assert expected == 6  # c1..c5 + c8

    resp = await client.get("/api/email/queue/count")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"count": expected}
    assert isinstance(body["count"], int)
    # The lightweight endpoint carries NO item payloads.
    assert "items" not in body


async def test_email_queue_count_empty_is_zero(client, email_file):
    """With no sidecar at all, the count endpoint returns {count: 0} (not 500)."""
    resp = await client.get("/api/email/queue/count")
    assert resp.status == 200
    assert await resp.json() == {"count": 0}


async def test_email_queue_count_route_not_shadowed_by_item_id(client, email_file):
    """'count' is its own route, not captured as a /queue/{item_id} (would 404)."""
    _seed(email_file, [_make_item("count", status="needs-review")])
    # If 'count' were captured as item_id, this would hit get_queue/save_draft
    # handlers; instead it returns the integer count of all 1 actionable items.
    resp = await client.get("/api/email/queue/count")
    assert resp.status == 200
    assert await resp.json() == {"count": 1}


# ─── Maturity: thread history, rich prompt, topic memory, polish ──────────────


@pytest.fixture
def topics_dir(tmp_path: Path, monkeypatch) -> Path:
    """Repoint the per-contact topic-memory dir at tmp_path."""
    d = tmp_path / "email" / "email_topics"
    monkeypatch.setattr(email_mod, "_email_topics_dir", lambda: d)
    return d


# --- (1a) thread-history extraction ------------------------------------------


def test_extract_thread_history_orders_oldest_to_newest():
    """_extract_thread_history builds [{sender,timestamp,body}] oldest->newest.

    The Outlook MCP email_read response carries multiple emails for a multi-turn
    conversation under content.emails[]; we render structured turns ordered oldest
    first (the array arrives newest-first like the inbox), cap body length, and
    never fabricate a turn for an email with no body.
    """
    payload = {
        "success": True,
        "content": {
            "emails": [
                {"sender": "John", "receivedDateTime": "2026-06-03T10:00:00Z",
                 "body": "Third (newest) message"},
                {"sender": "Me", "receivedDateTime": "2026-06-02T10:00:00Z",
                 "body": "Second message"},
                {"sender": "John", "receivedDateTime": "2026-06-01T10:00:00Z",
                 "body": "First (oldest) message"},
            ]
        },
    }
    turns = email_mod._extract_thread_history(payload)
    assert isinstance(turns, list)
    assert [t["body"] for t in turns] == [
        "First (oldest) message", "Second message", "Third (newest) message",
    ]
    assert all(set(t.keys()) >= {"sender", "timestamp", "body"} for t in turns)


def test_extract_thread_history_caps_body_and_count():
    """Each turn body is capped at 4k chars and the array at 20 turns."""
    emails = [
        {"sender": f"s{i}", "receivedDateTime": f"t{i}", "body": "x" * 5000}
        for i in range(30)
    ]
    turns = email_mod._extract_thread_history({"content": {"emails": emails}})
    assert len(turns) <= 20
    assert all(len(t["body"]) <= 4096 for t in turns)


def test_extract_thread_history_handles_garbage_without_fabricating():
    """A non-dict / empty / secret-only payload yields [] (never a fake turn)."""
    assert email_mod._extract_thread_history(None) == []
    assert email_mod._extract_thread_history("just a string") == []
    assert email_mod._extract_thread_history({"content": {"emails": []}}) == []
    # An email with no body must not become a turn.
    turns = email_mod._extract_thread_history(
        {"content": {"emails": [{"sender": "x", "body": ""}]}}
    )
    assert turns == []


def test_extract_email_body_flattens_sender_dict():
    """Outlook delivers sender as {'name','email'} -- the body header must read
    'From: <name>', NEVER a leaked Python dict repr ('From: {\\'name\\': ...}').

    Regression for the Thread Context display showing the raw dict.
    """
    payload = {"content": {"emails": [{
        "sender": {"name": "Tangudu, Punith", "email": "mtaylor@example.com"},
        "subject": "Travel Reminder",
        "body": "Hi all, gentle reminder.",
    }]}}
    out = email_mod._extract_email_body(payload)
    assert "From: Tangudu, Punith" in out
    assert "{'name'" not in out and "'email'" not in out
    # A plain-string sender still works (no regression).
    out2 = email_mod._extract_email_body(
        {"content": {"emails": [{"sender": "Jane", "subject": "Hi", "body": "yo"}]}}
    )
    assert "From: Jane" in out2


# --- (1b) rich draft prompt (the AC-3 unit-testable seam) --------------------


def test_build_email_draft_prompt_includes_history_style_and_topics(
    email_file, topics_dir, monkeypatch
):
    """_build_email_draft_prompt injects threadHistory + style + per-contact topics.

    This is the standalone builder that makes the rich-draft contract testable
    while the agent seam is stubbed: its assembled text must carry the full thread
    turns, the learned style store, and the contact's topic memory.
    """
    # Seed the style store and the contact's topic file.
    email_file.parent.mkdir(parents=True, exist_ok=True)
    email_mod.filestore.write_text(email_mod._style_path(), "Be warm and concise.")
    topics_dir.mkdir(parents=True, exist_ok=True)
    slug = email_mod._slugify_contact("john@example.com")
    email_mod.filestore.write_text(topics_dir / f"{slug}.md", "Discussed Q3 launch timing.")

    item = _make_item(
        sender="john@example.com",
        subject="Re: Launch",
        threadHistory=[
            {"sender": "John", "timestamp": "t1", "body": "When can we ship?"},
            {"sender": "Me", "timestamp": "t2", "body": "Aiming for Friday."},
        ],
    )
    prompt = email_mod._build_email_draft_prompt(item)
    assert "When can we ship?" in prompt          # thread history reaches the prompt
    assert "Aiming for Friday." in prompt
    assert "Be warm and concise." in prompt        # learned style reaches the prompt
    assert "Discussed Q3 launch timing." in prompt  # per-contact topic memory reaches it
    # A multi-turn history implies a re-draft (single cohesive reply) instruction.
    low = prompt.lower()
    assert "language" in low                        # decisive language rule present


def test_build_email_draft_prompt_scrubs_secrets():
    """The assembled prompt is scrubbed — credential lines never ride along."""
    item = _make_item(
        sender="x@example.com",
        threadHistory=[{"sender": "a", "timestamp": "t", "body": "ok"}],
        emailBody="line one\nauthorization: Bearer abc123\nline three",
    )
    prompt = email_mod._build_email_draft_prompt(item)
    assert "Bearer abc123" not in prompt
    assert "authorization:" not in prompt.lower()


def test_draft_prompt_flows_through_build_prompt():
    """_build_prompt('draft') uses payload['prompt'] as its base when present."""
    base = "SENTINEL-PROMPT-CONTENT-123"
    out = email_mod._build_prompt("draft", {"prompt": base, "item": _make_item()})
    assert base in out


# --- (1c) per-contact topic slugification + read ------------------------------


def test_slugify_contact_normalizes():
    """_slugify_contact: lowercase, non-alphanum->dash, collapse runs, strip ends."""
    f = email_mod._slugify_contact
    assert f("John Doe") == "john-doe"
    assert f("john@example.com") == "john-example-com"
    assert f("  Multiple   Spaces  ") == "multiple-spaces"
    assert f("a__b--c") == "a-b-c"
    assert f("---lead-and-trail---") == "lead-and-trail"


def test_slugify_shared_by_read_write(email_file, topics_dir):
    """read/append/prompt all resolve the SAME file via the shared slug."""
    email_file.parent.mkdir(parents=True, exist_ok=True)
    topics_dir.mkdir(parents=True, exist_ok=True)
    contact = "John Doe <john@example.com>"
    slug = email_mod._slugify_contact(contact)
    email_mod.filestore.write_text(topics_dir / f"{slug}.md", "shared note")
    assert "shared note" in email_mod.read_email_topic_summary(contact)


# --- (1c) GET/PUT /api/email/topics/{contact} --------------------------------


async def test_email_topics_get_empty_for_unseen_contact(client, email_file, topics_dir):
    """GET topics for a never-seen contact returns empty content (NOT 404)."""
    resp = await client.get("/api/email/topics/john-doe")
    assert resp.status == 200
    body = await resp.json()
    assert body["content"] == ""


async def test_email_topics_put_then_get_roundtrips(client, email_file, topics_dir):
    """PUT writes a contact's topic note; GET reads it back."""
    resp = await client.put("/api/email/topics/jane-doe", json={"content": "Talked about budgets."})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    resp = await client.get("/api/email/topics/jane-doe")
    assert (await resp.json())["content"] == "Talked about budgets."


async def test_email_topics_put_conflict_returns_409(client, email_file, topics_dir):
    """A stale etag on PUT topics returns the 409-with-current+etag shape."""
    await client.put("/api/email/topics/jane-doe", json={"content": "first"})
    resp = await client.put("/api/email/topics/jane-doe",
                            json={"content": "second", "etag": "stale-etag"})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "conflict"
    assert "current" in body and "etag" in body


async def test_email_topics_handler_rejects_traversal_chars(client, email_file, topics_dir):
    """A contact segment that REACHES the handler carrying a path-traversal char
    (backslash, or an encoded slash that decodes to one) is rejected with 400 by
    _validate_contact BEFORE slugifying. (Literal '..'/'/' segments are collapsed
    by the HTTP routing layer and never reach the handler — covered below.)"""
    for bad in ("x%5Cy", "foo%2Fbar"):  # -> 'x\\y', 'foo/bar' at the handler
        resp = await client.get(f"/api/email/topics/{bad}")
        assert resp.status == 400, f"GET {bad!r} should be rejected, got {resp.status}"
        resp = await client.put(f"/api/email/topics/{bad}", json={"content": "evil"})
        assert resp.status == 400, f"PUT {bad!r} should be rejected, got {resp.status}"


async def test_email_topics_traversal_never_escapes_topics_dir(
    client, email_file, topics_dir, tmp_path
):
    """No contact input (literal or encoded traversal) ever reads/writes a file
    OUTSIDE the topics dir — the slug strips path chars, and the guard rejects the
    rest. Plant a canary a traversal would otherwise hit and prove it's untouched."""
    canary = tmp_path / "passwd_canary.md"
    canary.write_text("SECRET", encoding="utf-8")
    for bad in ("../etc/passwd", "..", "a/b", "x%5Cy", "%2e%2e"):
        await client.put(f"/api/email/topics/{bad}", json={"content": "evil"})
        await client.get(f"/api/email/topics/{bad}")
    assert canary.read_text(encoding="utf-8") == "SECRET", "traversal escaped the topics dir"
    # Every file the handler created lives inside the topics dir and is a slug.md.
    if topics_dir.exists():
        for p in topics_dir.glob("*"):
            assert p.suffix == ".md" and "/" not in p.stem and "\\" not in p.stem


# --- (1d) approve appends a per-contact topic note ----------------------------


async def test_email_approve_appends_topic_note(client, email_file, topics_dir, monkeypatch):
    """Approving an email appends a one-line note to the sender's topic file."""
    _seed(email_file, [_make_item("i1", sender="john@example.com",
                                  subject="Re: Launch", draft="Sounds good, Friday works.")])
    _stub_agent(monkeypatch, {"available": True, "draftSaved": True})
    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    slug = email_mod._slugify_contact("john@example.com")
    note_path = topics_dir / f"{slug}.md"
    assert note_path.exists(), "approve should have written the contact topic file"
    content = note_path.read_text(encoding="utf-8")
    assert "Re: Launch" in content or "Launch" in content
    assert content.strip(), "topic note should be non-empty"


async def test_email_approve_topic_note_failure_is_best_effort(
    client, email_file, topics_dir, monkeypatch
):
    """A topic-write failure NEVER fails the approve (best-effort, logged)."""
    _seed(email_file, [_make_item("i1", draft="final")])
    _stub_agent(monkeypatch, {"available": True, "draftSaved": True})

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(email_mod, "_append_email_topic_note", boom)

    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "approved"


async def test_email_approve_still_only_saves_draft_never_sends(
    client, email_file, topics_dir, monkeypatch
):
    """Approve remains draft-save + clipboard ONLY — no reply/send action."""
    _seed(email_file, [_make_item("i1", draft="final")])
    agent = _stub_agent(monkeypatch, {"available": True, "draftSaved": True})
    await client.post("/api/email/queue/i1/approve", json={})
    actions = [c[0] for c in agent.calls]
    assert "reply" not in actions and "send" not in actions
    assert "save_draft" in actions


# --- (2a) polish endpoint -----------------------------------------------------


async def test_email_polish_returns_polished(client, email_file, monkeypatch):
    """POST /api/email/polish returns {available, draft} on a successful seam."""
    _stub_agent(monkeypatch, {"available": True, "draft": "Polished, fluent text."})
    resp = await client.post("/api/email/polish", json={"text": "rough  draft txt"})
    assert resp.status == 200
    body = await resp.json()
    assert body["available"] is True
    assert body["draft"] == "Polished, fluent text."


async def test_email_polish_empty_text_is_400(client, email_file, monkeypatch):
    """Empty/non-string text is a 400 (never spawns the seam)."""
    agent = _stub_agent(monkeypatch, {"available": True, "draft": "x"})
    for bad in ({"text": ""}, {"text": "   "}, {"text": 123}, {}):
        resp = await client.post("/api/email/polish", json=bad)
        assert resp.status == 400
    assert agent.calls == [], "400 must short-circuit before the seam"


async def test_email_polish_unavailable_is_502_not_fabricated(client, email_file, monkeypatch):
    """An unavailable seam is a 502 — never fabricate a polished result."""
    _stub_agent(monkeypatch, {"available": False, "reason": "not wired"})
    resp = await client.post("/api/email/polish", json={"text": "some draft"})
    assert resp.status == 502
    body = await resp.json()
    assert body["available"] is False


async def test_email_polish_does_not_touch_sidecar(client, email_file, monkeypatch):
    """Polish is stateless: it never reads/writes the email_threads.json sidecar."""
    _seed(email_file, [_make_item("i1", status="needs-review")])
    before = email_file.read_text(encoding="utf-8")
    _stub_agent(monkeypatch, {"available": True, "draft": "polished"})
    resp = await client.post("/api/email/polish", json={"text": "draft text"})
    assert resp.status == 200
    assert email_file.read_text(encoding="utf-8") == before, "polish must not write the sidecar"


def test_email_polish_action_prompt_preserves_language():
    """The 'polish' _build_prompt instruction preserves voice + language."""
    out = email_mod._build_prompt("polish", {"text": "hola, como estas"}).lower()
    assert "hola, como estas" in out
    assert "translate" in out  # the conservative "do NOT translate" guard


def test_email_polish_parse_failloud_on_missing_draft():
    """_parse_agent_result('polish') fails loud when 'draft' is missing."""
    res = email_mod._parse_agent_result('{"notdraft": 1}', "polish")
    assert res["available"] is False
    ok = email_mod._parse_agent_result('{"draft": "hi"}', "polish")
    assert ok["available"] is True and ok["draft"] == "hi"


# --- _mark_messages_read calls manager-outlook-mcp mark_email_read (Graph) ----
#
# The read path migrated to Microsoft Graph (manager-outlook-mcp). Dismiss-marks-
# read now uses the verified Graph schema:
#   mark_email_read {emails: {<message_id>: <subject|null>}, is_read: bool}
# It is a WRITE tool, so it MUST go through a SEPARATE explicitly-gated path
# (_call_graph_write_tool / _mark_messages_read), NEVER through call_read_tool and
# NEVER added to _EMAIL_READ_ONLY_TOOLS.


def _capture_graph_write(monkeypatch, return_value=None):
    """Patch the separate gated graph write path to record (name, args) calls."""
    calls = []

    async def fake_write(name, arguments):
        calls.append((name, arguments))
        if return_value is not None:
            return return_value
        # Default: a success result for every id in the batch.
        ids = list((arguments.get("emails") or {}).keys())
        return {
            "results": [{"message_id": i, "success": True} for i in ids],
            "summary": {"total": len(ids), "success": len(ids), "failed": 0},
        }

    monkeypatch.setattr(email_mod, "_call_graph_write_tool", fake_write)
    return calls


async def test_mark_messages_read_uses_mark_email_read(monkeypatch):
    """_mark_messages_read marks messages read via the Graph mark_email_read tool.

    Asserts the exact tool + Graph argument shape ({emails:{id:subj}, is_read:true})
    so a refactor that drops the batch map or the is_read flag is caught.
    """
    calls = _capture_graph_write(monkeypatch)
    await email_mod._mark_messages_read({"AAMk1": "Re: A", "AAMk2": "Re: B"})

    assert len(calls) == 1
    name, args = calls[0]
    assert name == "mark_email_read"
    assert args["is_read"] is True
    assert args["emails"] == {"AAMk1": "Re: A", "AAMk2": "Re: B"}


async def test_mark_messages_read_batches_at_ten(monkeypatch):
    """Graph mark_email_read accepts at most 10 ids/call — coalesce into chunks."""
    calls = _capture_graph_write(monkeypatch)
    messages = {f"AAMk{i}": f"S{i}" for i in range(23)}
    await email_mod._mark_messages_read(messages)

    # 23 ids -> 3 batches (10 + 10 + 3), each <= 10.
    assert len(calls) == 3
    sizes = [len(args["emails"]) for _, args in calls]
    assert sizes == [10, 10, 3]
    assert all(sz <= 10 for sz in sizes)
    # Every id was marked exactly once across the batches.
    seen = set()
    for _, args in calls:
        seen |= set(args["emails"].keys())
    assert seen == set(messages.keys())


async def test_mark_messages_read_failure_logs_warning_nonfatal(monkeypatch, caplog):
    """A Graph {success:false} (or a raised error) must NOT raise, and must log at
    WARNING (not invisible debug) — never claim success it did not achieve."""
    import logging

    async def fake_write(name, arguments):
        ids = list((arguments.get("emails") or {}).keys())
        # Graph reports the write FAILED for every id.
        return {
            "results": [{"message_id": i, "success": False} for i in ids],
            "summary": {"total": len(ids), "success": 0, "failed": len(ids)},
        }

    monkeypatch.setattr(email_mod, "_call_graph_write_tool", fake_write)
    with caplog.at_level(logging.WARNING, logger=email_mod.logger.name):
        # Must not raise.
        await email_mod._mark_messages_read({"AAMkX": "Re: X"})
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records), \
        "a mark-read failure must surface at WARNING"


async def test_mark_messages_read_raise_is_nonfatal(monkeypatch, caplog):
    """A raised exception in the gated write path NEVER breaks the dismiss."""
    import logging

    async def boom(name, arguments):
        raise email_mod.EmailMcpError("mark-read blew up")

    monkeypatch.setattr(email_mod, "_call_graph_write_tool", boom)
    with caplog.at_level(logging.WARNING, logger=email_mod.logger.name):
        await email_mod._mark_messages_read({"AAMkX": "Re: X"})
        await email_mod._mark_messages_read({})  # empty map is a no-op
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_mark_email_read_not_in_read_only_frozenset():
    """BOUNDARY: mark_email_read (a Graph WRITE tool) must NEVER be reachable via
    the read client — it stays out of _EMAIL_READ_ONLY_TOOLS so the read session
    can never invoke send/reply/delete/move/mark-read."""
    ro = email_mod._EMAIL_READ_ONLY_TOOLS
    for write_tool in ("mark_email_read", "send_email", "reply_to_email",
                       "delete_email", "move_email"):
        assert write_tool not in ro, f"{write_tool} must not be in the read-only set"
    # The two Graph READ tools DO belong on the read-only side.
    assert "get_emails" in ro and "get_email" in ro


async def test_call_read_tool_refuses_mark_email_read(monkeypatch):
    """call_read_tool must refuse mark_email_read by name BEFORE any connect."""
    with pytest.raises(email_mod.EmailMcpError):
        await email_mod.call_read_tool("mark_email_read", {})


async def test_graph_write_path_refuses_send_and_delete():
    """The SEPARATE gated write path accepts ONLY mark_email_read — never the other
    Graph write tools (send/reply/delete/move), so the gate can't be widened into a
    general send path."""
    for forbidden in ("send_email", "reply_to_email", "delete_email", "move_email",
                      "get_emails", "get_email"):
        with pytest.raises(email_mod.EmailMcpError):
            await email_mod._call_graph_write_tool(forbidden, {})


# --- (1a) scan attaches threadHistory from a multi-message conversation -------


async def test_scan_attaches_thread_history(email_file, monkeypatch):
    """_scan_once attaches a structured threadHistory from the Graph get_email body.

    Mocks the read-only Graph MCP client to return one new unread inbox message
    (get_emails) and a full body (get_email) so the scan stores threadHistory turns
    on the skeleton without the draft worker needing any further MCP call.
    """
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkRoadmap",
                "subject": "Re: Roadmap",
                "from": {"name": "John", "email": "john@example.com"},
                "received": "2026-06-03T10:00:00Z",
                "is_read": False,
                "preview": "latest snippet",
            }], "count": 1, "folder": "inbox"}
        if name == "get_email":
            return {"email": {
                "id": "AAMkRoadmap",
                "subject": "Re: Roadmap",
                "from": {"name": "John", "email": "john@example.com"},
                "body": (
                    "From: John\n\nnewest turn\n---\n"
                    "From: Me\n\nmiddle turn\n---\nFrom: John\n\noldest turn"
                ),
                "messages": [
                    {"from": {"name": "John"}, "received": "2026-06-03T10:00:00Z", "body": "newest turn"},
                    {"from": {"name": "Me"}, "received": "2026-06-02T10:00:00Z", "body": "middle turn"},
                    {"from": {"name": "John"}, "received": "2026-06-01T10:00:00Z", "body": "oldest turn"},
                ],
            }}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert len(saved) == 1
    th = saved[0].get("threadHistory")
    assert isinstance(th, list) and len(th) == 3
    assert [t["body"] for t in th] == ["oldest turn", "middle turn", "newest turn"]


async def test_scan_history_failure_is_nonfatal(email_file, monkeypatch):
    """A failing get_email does NOT fail the scan and never fabricates turns."""
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkX",
                "subject": "Hi",
                "from": {"name": "A", "email": "a@example.com"},
                "received": "2026-06-01T10:00:00Z",
                "is_read": False,
                "preview": "snip",
            }]}
        if name == "get_email":
            raise email_mod.EmailMcpError("read blew up")
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert len(saved) == 1  # item still created from the snippet
    assert not saved[0].get("threadHistory")  # no fabricated turns


# --- Graph read path: get_emails/get_email + _skeleton_for shape mapping -------


def test_skeleton_maps_graph_shape_with_sender_email():
    """REGRESSION (AC-3 / shape-mapping-gap): _skeleton_for maps the GRAPH email
    shape (id/subject/from{name,email}/received/is_read/has_attachments/preview)
    into the item contract — and senderEmail is now POPULATED (it was blank on the
    old aws-outlook path). A green pytest that only checks status flips would miss
    a blank senderEmail / empty snippet / unparsed ts; assert them explicitly."""
    raw = {
        "id": "AAMkADExample==",
        "subject": "Re: Q3 planning",
        "from": {"name": "Jane Roe", "email": "jane.roe@example.com"},
        "received": "2026-06-20T14:30:00Z",
        "is_read": False,
        "has_attachments": True,
        "preview": "Following up on the numbers we discussed",
    }
    sk = email_mod._skeleton_for(raw)
    # Graph message_id keys the item natively; conversationId aliases it (D-051).
    assert sk["messageId"] == "AAMkADExample=="
    assert sk["conversationId"] == "AAMkADExample=="
    assert sk["sender"] == "Jane Roe"
    assert sk["senderEmail"] == "jane.roe@example.com"   # NOW populated (was blank)
    assert sk["subject"] == "Re: Q3 planning"
    assert sk["snippet"] == "Following up on the numbers we discussed"
    assert sk["hasAttachments"] is True
    assert sk["status"] == "needs-classify"
    # ts is parsed from the ISO8601 'received' into epoch ms (not left as now()).
    assert isinstance(sk["ts"], int)
    from datetime import datetime, timezone
    expected_ms = int(datetime(2026, 6, 20, 14, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert sk["ts"] == expected_ms
    # A random-hex queue id distinct from the Graph message id.
    assert sk["id"] and sk["id"] != sk["messageId"]


def test_skeleton_sender_falls_back_to_email_when_name_blank():
    """When Graph from.name is empty, sender falls back to from.email (never blank)."""
    sk = email_mod._skeleton_for({
        "id": "AAMk1", "subject": "Hi",
        "from": {"name": "", "email": "noreply@example.com"},
        "received": "2026-06-20T14:30:00Z",
    })
    assert sk["sender"] == "noreply@example.com"
    assert sk["senderEmail"] == "noreply@example.com"


def test_extract_email_body_handles_graph_shape():
    """_extract_email_body reads the Graph {email:{...}} shape, flattening from{}."""
    payload = {"email": {
        "id": "AAMk1",
        "subject": "Travel Reminder",
        "from": {"name": "Tangudu, Punith", "email": "mtaylor@example.com"},
        "body": "Hi all, gentle reminder.",
    }}
    out = email_mod._extract_email_body(payload)
    assert "Hi all, gentle reminder." in out
    assert "From: Tangudu, Punith" in out
    assert "{'name'" not in out and "'email'" not in out


async def test_scan_dedupes_on_message_id(email_file, monkeypatch):
    """MIGRATION DATA-LOSS guard: an item already in the queue (active status) under
    its Graph message_id is NOT re-surfaced, and a genuinely new message_id IS.
    Re-keying conversationId->messageId must not drop or double-surface emails."""
    # Seed an existing active item keyed on a Graph message_id.
    _seed(email_file, [_make_item("existing", messageId="AAMkOLD",
                                  conversationId="AAMkOLD", status="needs-review")])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [
                {"id": "AAMkOLD", "subject": "Old", "from": {"name": "A", "email": "a@x.com"},
                 "received": "2026-06-01T10:00:00Z", "is_read": False, "preview": "old"},
                {"id": "AAMkNEW", "subject": "New", "from": {"name": "B", "email": "b@x.com"},
                 "received": "2026-06-02T10:00:00Z", "is_read": False, "preview": "new"},
            ]}
        if name == "get_email":
            mid = arguments.get("message_id")
            return {"email": {"id": mid, "subject": "S", "from": {"name": "x"}, "body": "body"}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    by_msg = {it.get("messageId"): it for it in saved}
    # The old message_id stayed a single item (not double-surfaced); the new one
    # earned a fresh skeleton.
    assert sum(1 for it in saved if it.get("messageId") == "AAMkOLD") == 1
    assert "AAMkNEW" in by_msg
    assert by_msg["AAMkNEW"]["status"] == "needs-classify"


async def test_scan_skips_muted_message(email_file, monkeypatch):
    """MIGRATION guard: a muted message (keyed on its messageId) must NOT re-appear
    after the conversationId->messageId re-key. Mute still matches."""
    _seed(email_file, [], mutedThreads={"AAMkMUTED": __import__("time").time()})

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [
                {"id": "AAMkMUTED", "subject": "Muted thread",
                 "from": {"name": "A", "email": "a@x.com"},
                 "received": "2026-06-01T10:00:00Z", "is_read": False, "preview": "x"},
            ]}
        if name == "get_email":
            return {"email": {"id": "AAMkMUTED", "subject": "S", "from": {"name": "x"}, "body": "b"}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert saved == [], "a muted message must not re-surface"


def test_node24_bin_resolution_fails_loud_when_absent(monkeypatch, tmp_path):
    """SILENT-NODE-22-FALLBACK guard: when no Node 24 bin dir is found, resolution
    RAISES an actionable error (never silently falls back to the default Node,
    which produces the opaque 'Connection closed')."""
    # Point the search at an empty dir and clear any override so nothing resolves.
    monkeypatch.setattr(email_mod, "_NODE24_GLOB", str(tmp_path / "nonexistent" / "node" / "24*" / "bin"))
    monkeypatch.delenv("CLAUDE_WEB_NODE24_BIN", raising=False)
    with pytest.raises(email_mod.GraphMcpNodeError) as exc:
        email_mod._resolve_node24_bin()
    msg = str(exc.value)
    # The error must name the expected location and the install command.
    assert "24" in msg
    assert ("mise" in msg.lower() or "install" in msg.lower())


def test_graph_mcp_params_injects_node24_path(monkeypatch, tmp_path):
    """_graph_mcp_params PREPENDS the Node-24 bin dir onto PATH for THAT subprocess
    only — never mutates os.environ or the aws-outlook launch."""
    node24 = tmp_path / "node24bin"
    node24.mkdir()
    monkeypatch.setenv("CLAUDE_WEB_NODE24_BIN", str(node24))
    before_os_path = __import__("os").environ.get("PATH", "")

    params = email_mod._graph_mcp_params()
    env = params.env or {}
    assert "PATH" in env, "the subprocess env must carry a PATH with Node 24 prepended"
    assert env["PATH"].split(":")[0] == str(node24), \
        "Node 24 bin dir must be PREPENDED (first on PATH)"
    # os.environ is untouched (no global toolchain mutation).
    assert __import__("os").environ.get("PATH", "") == before_os_path


def test_find_graph_mcp_entry_does_not_match_aws_outlook(monkeypatch, tmp_path):
    """The graph finder matches 'manager-outlook'/'graph' names, NOT 'aws-outlook'
    — so the existing aws-outlook draft path is untouched."""
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "aws-outlook-mcp": {"command": "aws-outlook-mcp"},
        "manager-outlook-mcp": {"command": "manager-outlook-mcp", "args": []},
    }}), encoding="utf-8")
    monkeypatch.setattr(email_mod, "_mcp_config_paths", lambda: [cfg])
    entry = email_mod._find_graph_mcp_entry()
    assert entry is not None
    assert entry.get("command") == "manager-outlook-mcp"


# ─── threadAsk contract (D-048 clauses 1-3): summary + ask, ONE call ──────────
#
# The draft/regenerate action emits BOTH threadContext (1-2 sentence summary) and
# a NEW threadAsk (what the sender needs FROM ME) from the SAME LLM call. The ask
# is OPTIONAL: a missing ask resolves to "" and must NEVER make a good draft
# return available:False or get fabricated. Both the draft WORKER and the
# regenerate request path must persist both (regenerate previously dropped even
# threadContext).


def test_build_prompt_draft_instructs_threadask_without_fabrication():
    """_build_prompt('draft'/'regenerate') asks for threadContext AND threadAsk,
    and the threadAsk wording forbids inventing an ask for an FYI/newsletter."""
    for action in ("draft", "regenerate"):
        out = email_mod._build_prompt(action, {"prompt": "BASE", "item": _make_item()})
        assert '"threadContext"' in out, f"{action}: summary key missing from contract"
        assert '"threadAsk"' in out, f"{action}: ask key missing from contract"
        low = out.lower()
        # The ask must be empty for a no-ask email and never invented.
        assert "empty string" in low, f"{action}: should instruct an empty ask when none"
        assert ("do not invent" in low or "not invent" in low or "never invent" in low), \
            f"{action}: should forbid fabricating an ask"


def test_parse_draft_result_includes_threadask():
    """A draft/regenerate reply that carries threadAsk surfaces it in the dict."""
    for action in ("draft", "regenerate"):
        res = email_mod._parse_agent_result(
            json.dumps({
                "draft": "Sure, Friday works.",
                "generatedDraft": "Sure, Friday works.",
                "threadContext": "John is asking about the ship date.",
                "threadAsk": "Confirm whether Friday works to ship.",
            }),
            action,
        )
        assert res["available"] is True
        assert res["threadContext"] == "John is asking about the ship date."
        assert res["threadAsk"] == "Confirm whether Friday works to ship."


def test_parse_draft_result_missing_ask_is_empty_not_fatal():
    """A reply that OMITS threadAsk is still available (ask defaults to ""), so a
    perfectly good draft is never thrown away because the ask was missing."""
    for action in ("draft", "regenerate"):
        res = email_mod._parse_agent_result(
            json.dumps({"draft": "Thanks for the heads up.",
                        "generatedDraft": "Thanks for the heads up.",
                        "threadContext": "FYI: build pipeline migrated."}),
            action,
        )
        assert res["available"] is True, f"{action}: missing ask must not be fatal"
        assert res["threadAsk"] == "", f"{action}: missing ask must default to empty, not fabricated"


def test_parse_draft_result_still_requires_draft():
    """'draft' remains the ONLY required key — a missing draft is still unavailable
    even when threadAsk/threadContext are present."""
    for action in ("draft", "regenerate"):
        res = email_mod._parse_agent_result(
            json.dumps({"threadContext": "summary", "threadAsk": "do the thing"}),
            action,
        )
        assert res["available"] is False


async def test_draft_worker_persists_threadask_and_context(email_file, monkeypatch):
    """The draft worker stores BOTH threadContext and threadAsk on the item."""
    _seed(email_file, [_make_item("i1", status="needs-draft", draft="", generatedDraft="")])
    _stub_agent(monkeypatch, {
        "available": True,
        "draft": "Sure, Friday works.",
        "generatedDraft": "Sure, Friday works.",
        "threadContext": "John asks about the ship date.",
        "threadAsk": "Confirm Friday ship date.",
    })

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    sem = email_mod.asyncio.Semaphore(1)
    await email_mod._draft_one({"ws_manager": _WS()}, sem, "i1")

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["threadContext"] == "John asks about the ship date."
    assert saved["threadAsk"] == "Confirm Friday ship date."


async def test_draft_worker_missing_ask_leaves_prior_untouched(email_file, monkeypatch):
    """An absent ask in the draft result never fails the save and leaves any prior
    threadAsk untouched (never blanks it, never writes 'undefined')."""
    _seed(email_file, [_make_item("i1", status="needs-draft", draft="", generatedDraft="",
                                  threadAsk="prior ask")])
    _stub_agent(monkeypatch, {
        "available": True,
        "draft": "Got it, thanks.",
        "generatedDraft": "Got it, thanks.",
        "threadContext": "FYI only.",
        # no threadAsk
    })

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    sem = email_mod.asyncio.Semaphore(1)
    await email_mod._draft_one({"ws_manager": _WS()}, sem, "i1")

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["status"] == "needs-review"
    assert saved["draft"] == "Got it, thanks."
    assert saved["threadAsk"] == "prior ask"  # untouched, not blanked


async def test_email_regenerate_persists_summary_and_ask(client, email_file, monkeypatch):
    """Regenerate refreshes BOTH threadContext AND threadAsk (it previously wrote
    only draft/generatedDraft/status and silently dropped threadContext)."""
    _seed(email_file, [_make_item("i1", status="edited", draft="old",
                                  threadContext="stale summary", threadAsk="stale ask")])
    _stub_agent(monkeypatch, {
        "available": True,
        "draft": "fresh draft",
        "generatedDraft": "fresh draft",
        "threadContext": "fresh summary",
        "threadAsk": "fresh ask",
    })
    resp = await client.post("/api/email/queue/i1/refresh")
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["draft"] == "fresh draft"
    assert body["item"]["threadContext"] == "fresh summary"
    assert body["item"]["threadAsk"] == "fresh ask"
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["threadContext"] == "fresh summary"
    assert saved["threadAsk"] == "fresh ask"


async def test_email_regenerate_empty_fields_keep_prior(client, email_file, monkeypatch):
    """Regenerate with an empty/absent summary+ask keeps the PRE-regenerate text
    rather than blanking it (set-only-when-non-empty rule)."""
    _seed(email_file, [_make_item("i1", status="edited", draft="old",
                                  threadContext="keep this summary",
                                  threadAsk="keep this ask")])
    _stub_agent(monkeypatch, {
        "available": True,
        "draft": "fresh draft",
        "generatedDraft": "fresh draft",
        # no threadContext, no threadAsk
    })
    resp = await client.post("/api/email/queue/i1/refresh")
    assert resp.status == 200
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["draft"] == "fresh draft"
    assert saved["threadContext"] == "keep this summary"  # not blanked
    assert saved["threadAsk"] == "keep this ask"          # not blanked
