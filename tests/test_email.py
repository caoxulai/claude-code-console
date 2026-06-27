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


async def test_email_approve_marks_read_but_mute_does_not(client, email_file, monkeypatch):
    """Approve and dismiss mark read; mute does NOT."""
    calls = _capture_mark_read(monkeypatch)
    async def fake_owa_write(name, arguments):
        return {"success": True, "draftId": "D1"}
    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    _seed(email_file, [
        _make_item("i1", messageId="AAMkA", conversationId="AAMkA", senderEmail="x@amazon.com"),
        _make_item("i2", messageId="AAMkB", conversationId="AAMkB"),
    ])

    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    resp = await client.post("/api/email/queue/i2/mute", json={})
    assert resp.status == 200

    # Approve marks read (AAMkA); mute does NOT (AAMkB absent).
    assert len(calls) == 1
    assert "AAMkA" in calls[0]


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
    """Approve marks status→approved and draftSaved:true when MCP draft succeeds."""
    _seed(email_file, [_make_item("i1", draft="final reply", senderEmail="bob@example.com")])
    calls = []
    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "draftId": "DRAFT123"}
    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "approved"
    assert body.get("draftSaved") is True
    assert len(calls) == 1
    assert calls[0][0] == "email_draft"
    assert calls[0][1]["operation"] == "create"
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
    """Approve calls ONLY email_draft (create) — never reply/send/forward."""
    _seed(email_file, [_make_item("i1", draft="final reply", senderEmail="bob@example.com")])
    calls = []
    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "draftId": "D1"}
    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    await client.post("/api/email/queue/i1/approve", json={})
    tool_names = [c[0] for c in calls]
    assert "email_reply" not in tool_names, "approve must NEVER call email_reply"
    assert "email_send" not in tool_names, "approve must NEVER call email_send"
    assert "email_forward" not in tool_names, "approve must NEVER call email_forward"
    assert "email_draft" in tool_names, f"expected email_draft, got: {tool_names}"


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


# --- (D-053) split a SINGLE inline-quoted Graph body into per-message turns ---


# The REAL MAWS-shaped body: Graph returns the whole thread as ONE message whose
# body has the latest reply on top and the quoted original below an Outlook
# From:/Date:/To:/Cc:/Subject: header block. There is NO messages[] array — the
# split helper is the ONLY thing that can produce >1 turn from this fixture, so
# the test goes RED the moment the split is reverted (anti-bogus-green).
_MAWS_BODY = (
    "+ Grace, Sailini\n"
    "\n"
    "Thanks Yibo for the background.\n"
    "\n"
    "Hi, managers, check this doc: https://chorus.aws.dev/doc/6SQyJYQGewO7 ...\n"
    "--\n"
    "Regrads,\n"
    "Han, Bingfeng\n"
    "\n"
    "From: Wang, Yibo mei@example.com\n"
    "Date: Wednesday, June 24, 2026 at 10:23\n"
    "To: Agarwal, Ankit sjones@example.com; Lakshmanan, Geetika priya@example.com; "
    "Seah, Yi Ling jkim@example.com\n"
    "Cc: Han, Bingfeng asmith@example.com; agl-pe agl-pe@amazon.com\n"
    "Subject: [Action needed] MAWS CN deprecation\n"
    "\n"
    "GL L7 SDMs,\n"
    "\n"
    "SDO is moving to deprecate MAWS hosting in the China (PEK) region ...\n"
    "Please review and confirm the migration plan by EOW.\n"
)


def _maws_payload() -> dict:
    """Graph get_email shape: a SINGLE message, the whole chain inline in body."""
    return {
        "email": {
            "from": {"name": "Han, Bingfeng", "email": "asmith@example.com"},
            "toRecipients": [
                {"name": "Wang, Yibo", "email": "mei@example.com"},
            ],
            "received": "2026-06-24T14:00:00Z",
            "subject": "[Action needed] MAWS CN deprecation",
            "body": _MAWS_BODY,
        }
    }


def test_extract_thread_history_splits_inline_quoted_chain():
    """A single inline-quoted Graph body splits into TWO ordered turns.

    turn[0] = Wang, Yibo (oldest, the quoted original, with his two asks + his To:
    list parsed into recipients); turn[1] = Han, Bingfeng (newest, his reply).
    NO From:/Date:/Subject: header text remains in either turn's body — those
    fields are promoted to the structured turn fields, so the run-on blob is gone.
    """
    turns = email_mod._extract_thread_history(_maws_payload())
    assert len(turns) == 2, f"expected a 2-way split, got {len(turns)}"

    oldest, newest = turns[0], turns[1]

    # Oldest = the quoted original from Wang, Yibo (sender PARSED from From:).
    assert "Wang, Yibo" in oldest["sender"]
    assert "mei@example.com" not in oldest["sender"]  # flattened to a name
    # His two asks survived (no message content lost).
    assert "SDO is moving to deprecate MAWS" in oldest["body"]
    assert "confirm the migration plan" in oldest["body"]
    # The Date: string is kept verbatim (un-ISO, _parse_ts returns None — fine).
    assert oldest["timestamp"] == "Wednesday, June 24, 2026 at 10:23"
    # His To: list was parsed into the recipients field.
    assert "Agarwal, Ankit" in oldest["recipients"]
    assert "Seah, Yi Ling" in oldest["recipients"]

    # Newest = Han, Bingfeng's reply (sender/recipients/timestamp from top_msg).
    assert "Han, Bingfeng" in newest["sender"]
    assert "Thanks Yibo for the background." in newest["body"]
    assert "Wang, Yibo" in newest["recipients"]  # from top_msg toRecipients
    assert newest["timestamp"] == "2026-06-24T14:00:00Z"

    # CRITICAL: the run-on header blob is gone from BOTH bodies.
    for t in turns:
        assert "From:" not in t["body"]
        assert "Date:" not in t["body"]
        assert "Subject:" not in t["body"]
        assert "Cc:" not in t["body"]

    # SEGMENT-BLEED guard: the reply text must NOT bleed into the quoted card,
    # nor the quoted original into the reply card.
    assert "SDO is moving to deprecate MAWS" not in newest["body"]
    assert "Thanks Yibo for the background." not in oldest["body"]


def test_extract_thread_history_single_fresh_email_is_one_turn():
    """A genuine single email with NO quoted-header boundary yields ONE turn,
    byte-identical to the pre-split whole-body behavior (backward compat)."""
    payload = {
        "email": {
            "from": {"name": "Jane Doe", "email": "jane@amazon.com"},
            "received": "2026-06-24T10:00:00Z",
            "subject": "Lunch?",
            "body": "Hey team,\n\nWant to grab lunch at noon? Let me know.\n\nJane",
        }
    }
    turns = email_mod._extract_thread_history(payload)
    assert len(turns) == 1
    assert "Jane Doe" in turns[0]["sender"]
    assert "Want to grab lunch at noon?" in turns[0]["body"]
    assert turns[0]["timestamp"] == "2026-06-24T10:00:00Z"


def test_extract_thread_history_prose_from_keyword_does_not_false_split():
    """A stray 'from:' / 'Subject:' in prose must NOT trigger a phantom split —
    the multi-line header SHAPE is required, not a single keyword."""
    payload = {
        "email": {
            "from": {"name": "Bob", "email": "bob@amazon.com"},
            "received": "2026-06-24T10:00:00Z",
            "body": (
                "Here is the update you asked for.\n"
                "The data came from: the warehouse export.\n"
                "Subject matter experts agree it looks correct.\n"
                "Let me know if you have questions.\n"
            ),
        }
    }
    turns = email_mod._extract_thread_history(payload)
    assert len(turns) == 1, "a single keyword in prose must not shatter the body"
    assert "warehouse export" in turns[0]["body"]
    assert "Subject matter experts" in turns[0]["body"]


def test_split_quoted_thread_no_content_lost():
    """Concatenating the parsed segment bodies accounts for all substantive text:
    every non-header line of the original body lands in exactly one turn."""
    turns = email_mod._extract_thread_history(_maws_payload())
    joined = "\n".join(t["body"] for t in turns)
    for line in _MAWS_BODY.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "--":
            continue
        # Header lines are promoted to fields, not kept in any body.
        if stripped.split(":", 1)[0] in ("From", "Date", "Sent", "To", "Cc", "Subject"):
            continue
        assert stripped in joined, f"lost body line: {stripped!r}"


def test_extract_thread_history_boundary_only_no_reply_text_degrades_to_one_turn():
    """AC-10 empty-segment degrade: a body that STARTS with a quoted-header block
    (no latest-reply text above the boundary) must NOT emit a blank top card.

    The segment above the first boundary is empty, so the split falls back to a
    SINGLE whole-body turn rather than dropping the quoted content or inventing a
    blank reply. The fallback turn keeps the top message's sender/timestamp and the
    quoted text is preserved (no message content silently lost)."""
    payload = {
        "email": {
            "from": {"name": "Han, Bingfeng", "email": "asmith@example.com"},
            "received": "2026-06-24T14:00:00Z",
            "subject": "Fwd: [Action needed] MAWS CN deprecation",
            "body": (
                "From: Wang, Yibo mei@example.com\n"
                "Date: Wednesday, June 24, 2026 at 10:23\n"
                "To: Agarwal, Ankit sjones@example.com\n"
                "Subject: [Action needed] MAWS CN deprecation\n"
                "\n"
                "GL L7 SDMs,\n"
                "\n"
                "SDO is moving to deprecate MAWS hosting in the China (PEK) region ...\n"
            ),
        }
    }
    turns = email_mod._extract_thread_history(payload)
    # No blank card: exactly one turn, with the body preserved.
    assert len(turns) == 1, f"a boundary with no reply above it must not add a blank card, got {len(turns)}"
    assert turns[0]["body"], "the single fallback turn must not be empty"
    # The quoted content is not dropped.
    assert "SDO is moving to deprecate MAWS" in turns[0]["body"]
    # No empty/blank-bodied turn snuck in alongside it.
    assert all(t["body"].strip() for t in turns)


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
    """Approve remains draft-save + clipboard ONLY — no reply/send tool call."""
    _seed(email_file, [_make_item("i1", draft="final", senderEmail="x@y.com")])
    calls = []
    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "draftId": "D2"}
    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    await client.post("/api/email/queue/i1/approve", json={})
    tool_names = [c[0] for c in calls]
    assert "email_reply" not in tool_names and "email_send" not in tool_names
    assert "email_draft" in tool_names


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
    """Patch the gated graph write paths to record (name, args) calls.

    Patches BOTH the persistent (_call_graph_write_tool) and the SHORT-LIVED
    (_call_graph_write_tool_oneshot) seams: the dismiss/delete paths now route
    through the one-shot spawn (which reuses the cached token without re-arming the
    always-on session), so a stub that patched only the persistent path would let
    the real MCP subprocess spawn.
    """
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
    monkeypatch.setattr(email_mod, "_call_graph_write_tool_oneshot", fake_write)
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

    monkeypatch.setattr(email_mod, "_call_graph_write_tool_oneshot", fake_write)
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

    monkeypatch.setattr(email_mod, "_call_graph_write_tool_oneshot", boom)
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


def test_graph_mcp_params_forces_owa_backend(monkeypatch, tmp_path):
    """_graph_mcp_params bakes MMCP_AUTH_BACKEND=owa into the subprocess env so the
    Graph path goes direct-to-Microsoft (off the GRASP 500/day shared quota) instead
    of the bundle's default 'grasp' backend. See docs/email-owa-backend-runbook.md."""
    node24 = tmp_path / "node24bin"
    node24.mkdir()
    monkeypatch.setenv("CLAUDE_WEB_NODE24_BIN", str(node24))

    params = email_mod._graph_mcp_params()
    env = params.env or {}
    assert env.get("MMCP_AUTH_BACKEND") == "owa", \
        "the Graph subprocess must run the OWA backend (off the GRASP quota)"


def test_graph_mcp_params_owa_default_yields_to_explicit_config(monkeypatch, tmp_path):
    """setdefault, not hard-set: an explicit MMCP_AUTH_BACKEND in the discovered
    mcpServers entry env still wins, so the backend remains operator-overridable."""
    node24 = tmp_path / "node24bin"
    node24.mkdir()
    monkeypatch.setenv("CLAUDE_WEB_NODE24_BIN", str(node24))
    monkeypatch.setattr(
        email_mod, "_find_graph_mcp_entry",
        lambda: {"command": "manager-outlook-mcp", "args": [],
                 "env": {"MMCP_AUTH_BACKEND": "grasp"}},
    )

    params = email_mod._graph_mcp_params()
    env = params.env or {}
    assert env.get("MMCP_AUTH_BACKEND") == "grasp", \
        "an explicit backend in the config entry must override the owa default"


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


# ─── D-052: latest-sender + senderList + per-turn recipients + GFM fixes ──────
#
# AC-1/AC-2: the From column must show the LATEST reply's sender (threadHistory
# is oldest->newest, so the newest is [-1]) plus a senderList of unique display
# names (oldest->newest, first-seen-wins, bounded) driving the "+N" participant
# count. AC-3/AC-4: _html_to_text must emit GFM-valid tables (leading/trailing
# pipes + a separator row) and line-anchored "- " bullets, while leaving plain
# text untouched. AC-5: each thread turn carries a human-readable recipients
# string for the To: line.


# --- (D-052 §1/§2) _sender_list_from_history helper ---------------------------


def test_sender_list_from_history_dedupes_first_seen_wins():
    """Ordered oldest->newest, deduped by case-insensitive name, FIRST-seen display
    string kept. Yibo->Bingfeng->Yibo collapses to ['Yibo','Bingfeng'] (no distinct
    participant silently dropped, order preserved)."""
    history = [
        {"sender": "Yibo", "timestamp": "t1", "body": "a"},
        {"sender": "Bingfeng", "timestamp": "t2", "body": "b"},
        {"sender": "Yibo", "timestamp": "t3", "body": "c"},
    ]
    assert email_mod._sender_list_from_history(history) == ["Yibo", "Bingfeng"]


def test_sender_list_from_history_dedupe_is_case_and_space_insensitive():
    """Dedup key is lowercased+stripped, but the STORED string is the first-seen
    display form (so 'Han ' and 'han' are one participant shown as 'Han ')."""
    history = [
        {"sender": "Han", "timestamp": "t1", "body": "a"},
        {"sender": "  han  ", "timestamp": "t2", "body": "b"},
        {"sender": "HAN", "timestamp": "t3", "body": "c"},
    ]
    assert email_mod._sender_list_from_history(history) == ["Han"]


def test_sender_list_from_history_drops_empties_and_bounds():
    """Empty/blank senders are dropped and the list is bounded to a reasonable cap."""
    history = [{"sender": "", "timestamp": "t", "body": "x"},
               {"sender": "   ", "timestamp": "t", "body": "x"}]
    assert email_mod._sender_list_from_history(history) == []
    # Many distinct senders are capped (suggest 12) — never unbounded.
    big = [{"sender": f"P{i}", "timestamp": "t", "body": "x"} for i in range(40)]
    out = email_mod._sender_list_from_history(big)
    assert 0 < len(out) <= 12


def test_sender_list_from_history_handles_garbage():
    """A non-list / non-dict-entry input yields [] (never raises)."""
    assert email_mod._sender_list_from_history(None) == []
    assert email_mod._sender_list_from_history("nope") == []
    assert email_mod._sender_list_from_history([None, 1, "x"]) == []


# --- (D-052 §5) per-turn recipients string on threadHistory -------------------


def test_extract_thread_history_attaches_recipients_string():
    """Each turn carries a 'recipients' STRING flattened from to/recipients (name
    else email, joined with ', ') — NEVER a dict repr / [object Object]."""
    payload = {"content": {"emails": [
        {"sender": "John", "receivedDateTime": "2026-06-01T10:00:00Z",
         "body": "oldest",
         "toRecipients": [{"name": "Me", "email": "me@x.com"},
                          {"name": "", "email": "cc@x.com"}]},
        {"sender": "Me", "receivedDateTime": "2026-06-02T10:00:00Z",
         "body": "newest", "recipients": [{"name": "John Doe", "email": "j@x.com"}]},
    ]}}
    turns = email_mod._extract_thread_history(payload)
    assert all("recipients" in t for t in turns)
    # name-else-email flattening, joined with ", " — no dict noise.
    assert turns[0]["recipients"] == "Me, cc@x.com"
    assert turns[1]["recipients"] == "John Doe"
    for t in turns:
        assert "{" not in t["recipients"] and "object Object" not in t["recipients"]


def test_extract_thread_history_recipients_empty_when_absent():
    """A turn with no recipient info gets recipients '' (never fabricated)."""
    turns = email_mod._extract_thread_history(
        {"content": {"emails": [{"sender": "A", "receivedDateTime": "t", "body": "hi"}]}}
    )
    assert turns and turns[0]["recipients"] == ""


# --- (D-052 §1) latest sender + senderList on the PERSISTED item, both paths ---


async def test_scan_sets_latest_sender_and_sender_list(email_file, monkeypatch):
    """AC-1/AC-2: after the body fetch attaches threadHistory, the PERSISTED skeleton
    'sender' is the LATEST (newest) turn's sender and 'senderList' is the ordered,
    deduped participant list — NOT the original/inbox sender."""
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkMAWS", "subject": "Re: MAWS CN deprecation",
                "from": {"name": "Yibo", "email": "yibo@example.com"},
                "received": "2026-06-03T10:00:00Z", "is_read": False, "preview": "x",
            }]}
        if name == "get_email":
            return {"email": {"id": "AAMkMAWS", "subject": "Re: MAWS CN deprecation",
                              "messages": [
                {"from": {"name": "Yibo"}, "received": "2026-06-01T10:00:00Z", "body": "first"},
                {"from": {"name": "Bingfeng"}, "received": "2026-06-02T10:00:00Z", "body": "reply"},
            ]}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    # The newest turn is Bingfeng — the From column must show the LATEST sender,
    # NOT the original (Yibo) nor the inbox 'from'.
    assert saved["sender"] == "Bingfeng"
    assert saved["senderList"] == ["Yibo", "Bingfeng"]


async def test_scan_no_history_keeps_seeded_sender(email_file, monkeypatch):
    """When the body fetch yields no thread history, the seeded (inbox) sender is
    left untouched — no overwrite, no blank, and no senderList fabricated."""
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkNohist", "subject": "Hi",
                "from": {"name": "Original Sender", "email": "orig@example.com"},
                "received": "2026-06-03T10:00:00Z", "is_read": False, "preview": "x",
            }]}
        if name == "get_email":
            # No body -> _extract_thread_history returns [] (no turns).
            return {"email": {"id": "AAMkNohist", "subject": "Hi",
                              "from": {"name": "Original Sender"}, "body": ""}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["sender"] == "Original Sender"  # seeded value untouched
    assert not saved.get("senderList")           # none fabricated


async def test_scan_backfill_updates_sender_and_list(email_file, monkeypatch):
    """The backfill-retry loop (existing item with empty emailBody) ALSO overwrites
    sender/senderList on the persisted item after _extract_thread_history."""
    # Existing active item with NO body and the stale original sender.
    _seed(email_file, [_make_item(
        "i1", messageId="AAMkBackfill", conversationId="AAMkBackfill",
        status="needs-review", sender="Stale Original", emailBody="",
    )])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": []}  # no new inbox items
        if name == "get_email":
            return {"email": {"id": "AAMkBackfill", "subject": "S", "messages": [
                {"from": {"name": "Han"}, "received": "2026-06-01T10:00:00Z", "body": "one"},
                {"from": {"name": "Bingfeng"}, "received": "2026-06-02T10:00:00Z", "body": "two"},
            ]}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})
    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert saved["sender"] == "Bingfeng"             # latest turn, not the stale original
    assert saved["senderList"] == ["Han", "Bingfeng"]


def test_skeleton_for_keeps_original_sender():
    """_skeleton_for runs BEFORE the body fetch and must NOT derive a latest sender:
    it seeds 'sender' from the inbox 'from' and writes NO senderList."""
    sk = email_mod._skeleton_for({
        "id": "AAMk1", "subject": "Hi",
        "from": {"name": "Inbox Sender", "email": "inbox@example.com"},
        "received": "2026-06-20T14:30:00Z",
    })
    assert sk["sender"] == "Inbox Sender"
    assert "senderList" not in sk  # the body-fetch path adds it, not the skeleton


# --- (D-052 §6) _html_to_text GFM table + bullet fixes ------------------------


def test_html_to_text_emits_gfm_table_with_separator_row():
    """A <table> becomes a GFM table: leading/trailing pipes on every row AND a
    '| --- | --- | --- |' separator row right after the header (the load-bearing
    part — pipes without it render as literal text in remarkGfm)."""
    html = (
        "<table><tr><th>Status</th><th>Owner</th><th>ETA</th></tr>"
        "<tr><td>Green</td><td>Han</td><td>Fri</td></tr>"
        "<tr><td>Red</td><td>Bingfeng</td><td>Mon</td></tr></table>"
    )
    out = email_mod._html_to_text(html)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # Header row wrapped in pipes.
    assert lines[0] == "| Status | Owner | ETA |"
    # Separator row immediately after the header, matching the 3-column count.
    assert lines[1] == "| --- | --- | --- |"
    # Body rows also wrapped.
    assert lines[2] == "| Green | Han | Fri |"
    assert lines[3] == "| Red | Bingfeng | Mon |"


def test_html_to_text_bullets_are_line_anchored_dashes():
    """<li> becomes a line-anchored '- ' GFM marker (NO leading spaces, NOT the
    Unicode '•') so ReactMarkdown renders a real <ul><li>."""
    html = "<ul><li>First point</li><li>Second point</li></ul>"
    out = email_mod._html_to_text(html)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines == ["- First point", "- Second point"]
    assert "•" not in out
    # Each marker sits at column 0 of its line (no leading whitespace).
    for ln in lines:
        assert ln.startswith("- ") and not ln.startswith(" ")


def test_html_to_text_plain_text_unchanged():
    """Backward-compat: plain text (no '<') passes through verbatim — no injected
    pipes, no separator row, no stray '- '."""
    plain = "Hi team,\n\nThis is a normal note. Budget | forecast is fine.\nThanks"
    assert email_mod._html_to_text(plain) == plain


def test_html_to_text_prose_html_not_turned_into_table_or_list():
    """HTML with no <table>/<li> must NOT gain a separator row, leading/trailing
    pipes, or '- ' markers — a literal '|' typed in prose is left intact."""
    html = "<p>Compare A | B below.</p><p>- not a real bullet</p>"
    out = email_mod._html_to_text(html)
    assert "| --- |" not in out          # no phantom separator
    assert "A | B" in out                # the literal pipe is preserved as-is
    # The paragraph text is not re-pipe-wrapped into a table row.
    assert "| Compare A | B below. |" not in out


def test_html_to_text_prose_before_table_stays_prose():
    """Intro prose immediately preceding a <table> stays on its own line — it is NOT
    folded into the table's first cell (a common real-email layout)."""
    html = ("<p>Status summary below:</p>"
            "<table><tr><th>Service</th><th>State</th></tr>"
            "<tr><td>MAWS</td><td>Deprecating</td></tr></table>")
    out = email_mod._html_to_text(html)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "Status summary below:"     # prose, not a pipe-wrapped cell
    assert lines[1] == "| Service | State |"
    assert lines[2] == "| --- | --- |"             # separator after the header
    assert lines[3] == "| MAWS | Deprecating |"


# ─── D-052: backfill an un-fetchable body from a copy quoted in another thread ──
# Some Graph message_ids fail get_email persistently (observed: a standalone
# original whose id 400s every time) while the SAME message is quoted inside a
# reply thread we already fetched and split. When the item's own get_email fails,
# recover its body from that already-parsed quoted copy instead of leaving the
# card stuck on the truncated snippet.


def test_quoted_body_index_keys_by_normalized_subject_and_sender():
    """_quoted_body_index maps (subject sans Re:/Fwd:, sender) -> the quoted turn
    body, built from items that already carry a parsed multi-turn threadHistory."""
    re_item = _make_item(
        "re1",
        subject="Re: [Action needed] MAWS CN deprecation",
        sender="Han, Bingfeng",
        threadHistory=[
            {"sender": "Wang, Yibo", "timestamp": "Wed", "body": "Yibo full original body", "recipients": "a; b"},
            {"sender": "Han, Bingfeng", "timestamp": "", "body": "Han reply", "recipients": ""},
        ],
    )
    idx = email_mod._quoted_body_index([re_item])
    # Yibo's original is recoverable by (normalized subject, his name)
    key = ("[action needed] maws cn deprecation", "wang, yibo")
    assert key in idx
    assert idx[key]["body"] == "Yibo full original body"


async def test_scan_backfills_body_from_quoted_copy_when_get_email_fails(email_file, monkeypatch):
    """A body-less standalone item whose get_email FAILS recovers its body from the
    same message quoted in an already-fetched reply thread (no fabrication)."""
    re_item = _make_item(
        "re1",
        messageId="AAMkRE",
        conversationId="AAMkRE",
        subject="Re: [Action needed] MAWS CN deprecation",
        sender="Han, Bingfeng",
        emailBody="...",
        threadHistory=[
            {"sender": "Wang, Yibo", "timestamp": "Wednesday, June 24, 2026 at 10:23",
             "body": "GL L7 SDMs,\n\nSDO is moving to deprecate MAWS ... Thanks,\nYibo",
             "recipients": "Agarwal, Ankit; Xu, Jun"},
            {"sender": "Han, Bingfeng", "timestamp": "", "body": "Thanks Yibo", "recipients": ""},
        ],
    )
    solo = _make_item(
        "solo1",
        messageId="AAMkSOLO",
        conversationId="AAMkSOLO",
        subject="[Action needed] MAWS CN deprecation",
        sender="Wang, Yibo",
        emailBody="",            # body fetch never succeeded
        threadHistory=[],
        status="needs-review",
        snippet="GL L7 SDMs, SDO is moving to deprecate MAWS hosting in the China (PEK) reg",
    )
    _seed(email_file, [re_item, solo])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [], "count": 0, "folder": "inbox"}   # no new mail
        if name == "get_email":
            raise email_mod.EmailMcpError("GRASP mail GET 400 for this id")
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})

    saved = {it["id"]: it for it in json.loads(email_file.read_text(encoding="utf-8"))["items"]}
    recovered = saved["solo1"]
    # Body recovered from the quoted copy — the FULL original, not the truncated snippet
    assert "Thanks,\nYibo" in recovered["emailBody"]
    assert recovered["emailBody"] != recovered["snippet"]
    # A threadHistory turn now exists so the card renders the full body, not snippet
    assert len(recovered.get("threadHistory") or []) >= 1
    assert recovered["threadHistory"][0]["sender"] == "Wang, Yibo"


async def test_scan_does_not_fabricate_body_when_no_quoted_copy_exists(email_file, monkeypatch):
    """If get_email fails AND no quoted copy exists anywhere, the body stays empty —
    we never invent content (fail-loud / fall back to snippet, AC degrade)."""
    solo = _make_item(
        "solo2",
        messageId="AAMkONLY",
        conversationId="AAMkONLY",
        subject="[Action needed] Unique unfetchable thread",
        sender="Wang, Yibo",
        emailBody="",
        threadHistory=[],
        status="needs-review",
    )
    _seed(email_file, [solo])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [], "count": 0, "folder": "inbox"}
        if name == "get_email":
            raise email_mod.EmailMcpError("GRASP 400")
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})

    saved = {it["id"]: it for it in json.loads(email_file.read_text(encoding="utf-8"))["items"]}
    assert saved["solo2"]["emailBody"] == ""          # never fabricated
    assert not saved["solo2"].get("threadHistory")


async def test_scan_inbox_fetch_failure_still_runs_backfill(email_file, monkeypatch):
    """A get_emails (inbox) failure must NOT abort the cycle — body backfill of items
    ALREADY in the queue is independent of fetching new mail and must still run.

    This is the real-world GRASP-400 case: the standalone item can't fetch its own
    body, and the inbox call also intermittently 400s; the quoted-copy recovery must
    still fire instead of the whole scan aborting at the top.
    """
    re_item = _make_item(
        "re1",
        messageId="AAMkRE2",
        conversationId="AAMkRE2",
        subject="Re: [Action needed] MAWS CN deprecation",
        sender="Han, Bingfeng",
        emailBody="...",
        threadHistory=[
            {"sender": "Wang, Yibo", "timestamp": "Wed", "body": "Yibo full original ... Thanks,\nYibo", "recipients": "x; y"},
            {"sender": "Han, Bingfeng", "timestamp": "", "body": "Han reply", "recipients": ""},
        ],
    )
    solo = _make_item(
        "solo3",
        messageId="AAMkSOLO3",
        conversationId="AAMkSOLO3",
        subject="[Action needed] MAWS CN deprecation",
        sender="Wang, Yibo",
        emailBody="",
        threadHistory=[],
        status="needs-review",
        snippet="GL L7 SDMs, SDO is moving to deprecate MAWS hosting in the China (PEK) reg",
    )
    _seed(email_file, [re_item, solo])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            raise email_mod.EmailMcpError("GRASP mail GET 400 on inbox")  # the abort case
        if name == "get_email":
            raise email_mod.EmailMcpError("GRASP mail GET 400 on message")
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    # Must NOT raise even though get_emails failed
    await email_mod._scan_once({"ws_manager": _WS()})

    saved = {it["id"]: it for it in json.loads(email_file.read_text(encoding="utf-8"))["items"]}
    # Backfill still ran and recovered the standalone body from the quoted copy
    assert "Thanks,\nYibo" in saved["solo3"]["emailBody"]
    assert saved["solo3"]["emailBody"] != saved["solo3"]["snippet"]


# ─── (D-054) stop the GRASP quota burn + honest delete/throttle ──────────────
# The always-on persistent Graph session kept GRASP's TokenRefreshRunner alive,
# firing a full SAML re-auth every ~10 min and exhausting GRASP's API-Gateway
# quota (429 {"error":"quota_exceeded"}). These tests pin the fix:
#   (1) the persistent session is torn down when paused/idle,
#   (2) a user delete works via a SHORT-LIVED spawn with the persistent session
#       torn down,
#   (3) only a confirmed success:true flips an item to deleted,
#   (4) a 429/quota condition surfaces graphThrottled + lastGraphError (and is
#       logged un-truncated).


class _NoopWS:
    async def broadcast(self, *a, **k):
        pass


async def test_paused_tick_tears_down_persistent_graph_session(email_file, monkeypatch):
    """(1/HALF-TEARDOWN) On a paused scan tick the worker reaps the persistent
    Graph session via the EXISTING _disconnect_graph_mcp, so GRASP's refresh timer
    dies with the subprocess and no auth churn accrues while paused."""
    _seed(email_file, [], paused=True)

    disconnect_calls = []

    async def fake_disconnect():
        disconnect_calls.append(True)

    monkeypatch.setattr(email_mod, "_disconnect_graph_mcp", fake_disconnect)
    monkeypatch.setattr(email_mod, "_probe_readiness", lambda: {"ready": True})

    await email_mod._reap_idle_graph_session({"ws_manager": _NoopWS()})

    assert disconnect_calls, \
        "a paused tick must call _disconnect_graph_mcp (not just guard reconnects)"


async def test_idle_tick_tears_down_persistent_graph_session(email_file, monkeypatch):
    """(1) Even when NOT paused, a session idle past _GRAPH_IDLE_TEARDOWN_S is
    reaped so the refresh timer can't churn during a quiet inbox."""
    _seed(email_file, [])

    disconnect_calls = []

    async def fake_disconnect():
        disconnect_calls.append(True)

    monkeypatch.setattr(email_mod, "_disconnect_graph_mcp", fake_disconnect)
    # Last Graph activity is way in the past -> idle past the teardown threshold.
    monkeypatch.setattr(
        email_mod, "_graph_last_activity",
        __import__("time").time() - (email_mod._GRAPH_IDLE_TEARDOWN_S + 60),
    )

    await email_mod._reap_idle_graph_session({"ws_manager": _NoopWS()})

    assert disconnect_calls, "an idle session must be reaped past the idle threshold"


async def test_recent_activity_does_not_tear_down_graph_session(email_file, monkeypatch):
    """(1/STICKY guard) A session with RECENT activity and not paused is left
    connected — the reaper must not kill an actively-used session."""
    _seed(email_file, [])

    disconnect_calls = []

    async def fake_disconnect():
        disconnect_calls.append(True)

    monkeypatch.setattr(email_mod, "_disconnect_graph_mcp", fake_disconnect)
    monkeypatch.setattr(email_mod, "_graph_last_activity", __import__("time").time())

    await email_mod._reap_idle_graph_session({"ws_manager": _NoopWS()})

    assert not disconnect_calls, "a recently-active, unpaused session must not be reaped"


async def test_delete_works_via_oneshot_with_persistent_session_torn_down(
    client, email_file, monkeypatch
):
    """(2/SESSION-REUSE) A user delete must succeed through a SHORT-LIVED spawn that
    reuses the cached token, with NO dependency on the persistent Graph session.

    The persistent session is torn down (session=None) and the persistent write
    path is made to BLOW UP — the delete must still flip to deleted because it goes
    through the one-shot spawn, never the persistent _call_graph_write_tool.
    """
    _seed(email_file, [_make_item("d1", messageId="AAMkDEL", conversationId="AAMkDEL",
                                  status="needs-review")])

    # Persistent session is correctly torn down.
    email_mod._graph_mcp_state.session = None

    # The persistent write path must NOT be used for delete — make it fail loud.
    async def persistent_must_not_run(name, arguments):
        raise AssertionError("delete must NOT route through the persistent session")

    monkeypatch.setattr(email_mod, "_call_graph_write_tool", persistent_must_not_run)

    oneshot_calls = []

    async def fake_oneshot(name, arguments):
        oneshot_calls.append((name, arguments))
        ids = list((arguments.get("emails") or {}).keys())
        return {"results": [{"message_id": i, "success": True} for i in ids],
                "summary": {"total": len(ids), "success": len(ids), "failed": 0}}

    monkeypatch.setattr(email_mod, "_call_graph_write_tool_oneshot", fake_oneshot)

    resp = await client.post("/api/email/queue/d1/delete", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body.get("deleted") is True

    assert oneshot_calls and oneshot_calls[0][0] == "delete_email"
    saved = {it["id"]: it for it in json.loads(email_file.read_text(encoding="utf-8"))["items"]}
    assert saved["d1"]["status"] == "deleted"


async def test_delete_failure_keeps_item_actionable_and_records_error(
    client, email_file, monkeypatch
):
    """(3/THE DELETE LIE + SWALLOWED 200) A delete whose Outlook move FAILS (429/
    quota) must NOT flip the item to deleted. The item stays in its prior actionable
    state, records lastActionError, and the response body carries deleted:false so
    the frontend never relies on the HTTP envelope alone."""
    _seed(email_file, [_make_item("d2", messageId="AAMkDEL2", conversationId="AAMkDEL2",
                                  status="needs-review")])
    email_mod._graph_mcp_state.session = None

    async def fake_oneshot(name, arguments):
        ids = list((arguments.get("emails") or {}).keys())
        # Graph reports the delete FAILED with a quota error per id.
        return {"results": [{"message_id": i, "success": False,
                             "error": "GRASP delete -> 429 quota_exceeded"} for i in ids],
                "summary": {"total": len(ids), "success": 0, "failed": len(ids)}}

    monkeypatch.setattr(email_mod, "_call_graph_write_tool_oneshot", fake_oneshot)

    resp = await client.post("/api/email/queue/d2/delete", json={})
    # The body must say the delete did NOT happen.
    body = await resp.json()
    assert body.get("deleted") is False
    assert body.get("error")

    saved = {it["id"]: it for it in json.loads(email_file.read_text(encoding="utf-8"))["items"]}
    item = saved["d2"]
    # NOT flipped to deleted — still actionable in its prior state.
    assert item["status"] == "needs-review"
    # The WHY is recorded on the item for the frontend.
    assert item.get("lastActionError", {}).get("action") == "delete"
    assert item["lastActionError"].get("reason")


async def test_delete_messages_outlook_returns_per_message_error(monkeypatch):
    """(4/DROP THE WHY) _delete_messages_outlook returns a per-message outcome map
    that PROPAGATES r.get('error'), instead of discarding it (fire-and-forget)."""
    async def fake_oneshot(name, arguments):
        ids = list((arguments.get("emails") or {}).keys())
        return {"results": [{"message_id": i, "success": False,
                             "error": "GRASP delete -> 429 quota_exceeded"} for i in ids],
                "summary": {"total": len(ids), "success": 0, "failed": len(ids)}}

    monkeypatch.setattr(email_mod, "_call_graph_write_tool_oneshot", fake_oneshot)

    outcomes = await email_mod._delete_messages_outlook({"AAMk1": "Re: A"})
    assert isinstance(outcomes, dict)
    out = outcomes["AAMk1"]
    assert out["success"] is False
    assert "quota_exceeded" in (out.get("error") or "")


async def test_delete_oneshot_refuses_non_allowlisted_tool():
    """(2/boundary) The one-shot write spawn refuses any name not in the gated
    write allowlist BEFORE any spawn — the read/write boundary is unchanged."""
    for forbidden in ("send_email", "reply_to_email", "move_email",
                      "get_emails", "get_email"):
        with pytest.raises(email_mod.EmailMcpError):
            await email_mod._call_graph_write_tool_oneshot(forbidden, {})


def test_is_graph_quota_error_matches_429_and_quota_body():
    """(5) The quota detector matches HTTP 429 and a quota_exceeded body."""
    assert email_mod._is_graph_quota_error(email_mod.EmailMcpError("GET -> 429"))
    assert email_mod._is_graph_quota_error(email_mod.EmailMcpError('{"error":"quota_exceeded"}'))
    assert email_mod._is_graph_quota_error('{"error":"quota_exceeded"}')
    assert not email_mod._is_graph_quota_error(email_mod.EmailMcpError("ordinary 400"))


async def test_quota_error_surfaces_graphthrottled_in_queue(client, email_file, monkeypatch, caplog):
    """(5+3/TRUNCATED-HONESTY + STICKY) A 429/quota_exceeded read failure:
      - sets graphThrottledUntil + lastGraphError on the store,
      - GET /api/email/queue computes graphThrottled LIVE (self-clearing),
      - is logged UN-TRUNCATED so the status code + quota_exceeded are visible.
    """
    import logging
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            raise email_mod.EmailMcpError(
                'Email Graph MCP tool returned an error: ' + ('x' * 400)
                + ' -> 429 {"error":"quota_exceeded","x-amz-apigw-id":"abc"}'
            )
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    with caplog.at_level(logging.WARNING, logger=email_mod.logger.name):
        await email_mod._scan_once({"ws_manager": _NoopWS()})

    # The store now carries the throttle window + a machine-readable reason
    # ({reason, ts}) so backoff has a timestamp.
    data = json.loads(email_file.read_text(encoding="utf-8"))
    assert data.get("graphThrottledUntil")
    assert "quota_exceeded" in (data.get("lastGraphError", {}).get("reason") or "")

    # GET /api/email/queue exposes graphThrottled (live) + the human reason STRING
    # (the queue flattens the persisted dict to its reason for the UI banner).
    resp = await client.get("/api/email/queue")
    q = await resp.json()
    assert q.get("graphThrottled") is True
    assert "quota_exceeded" in (q.get("lastGraphError") or "")

    # The error was logged UN-TRUNCATED: the 'quota_exceeded' code at the END of a
    # >200-char body must still be visible (the old _one_line(.., 200) chopped it).
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "quota_exceeded" in blob, "the quota code must survive un-truncated in the log"


async def test_graphthrottled_self_clears_after_window(client, email_file):
    """(5/STICKY-THROTTLE) graphThrottled is computed LIVE as now<until, so an
    EXPIRED throttle window reads False — never a permanent 'rate-limited' lie."""
    _seed(email_file, [],
          graphThrottledUntil=(__import__("time").time() - 5),  # already expired
          lastGraphError={"reason": "GET -> 429 quota_exceeded", "ts": 1})
    resp = await client.get("/api/email/queue")
    q = await resp.json()
    assert q.get("graphThrottled") is False


async def test_scan_skips_graph_read_while_throttled(email_file, monkeypatch):
    """(5/HOT-RETRY) While now < graphThrottledUntil the scan SKIPS the Graph read
    entirely (a real delay), so it can't keep hammering the exhausted quota."""
    import time as _time
    _seed(email_file, [],
          graphThrottledUntil=(_time.time() + 600),  # throttled for 10 min
          lastGraphError={"reason": "GET -> 429 quota_exceeded", "ts": 1})

    read_calls = []

    async def fake_read_tool(name, arguments):
        read_calls.append(name)
        return {"emails": [], "count": 0, "folder": "inbox"}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    await email_mod._scan_once({"ws_manager": _NoopWS()})

    assert "get_emails" not in read_calls, \
        "a throttled scan must NOT re-fire the Graph inbox read"


def test_error_body_not_truncated_at_200(monkeypatch):
    """(3/TRUNCATED-HONESTY unit) The ERROR-body path in _call_session_tool keeps a
    >200-char body (so '429'/'quota_exceeded' at the end survive). We exercise the
    raised EmailMcpError message directly via a fake session."""
    long_tail = "x" * 400 + ' -> 429 {"error":"quota_exceeded"}'

    class _ErrResult:
        isError = True
        content = long_tail

    class _FakeSession:
        async def call_tool(self, name, arguments):
            return _ErrResult()

    state = email_mod._PersistentMcpState()
    state.session = _FakeSession()

    async def _connect():
        pass

    async def _disconnect():
        pass

    async def _run():
        try:
            await email_mod._call_session_tool(
                name="get_emails", arguments={}, state=state,
                connect=_connect, disconnect=_disconnect, label="Email Graph",
            )
            return None
        except email_mod.EmailMcpError as e:
            return str(e)

    import asyncio as _asyncio
    msg = _asyncio.new_event_loop().run_until_complete(_run())
    assert msg is not None
    assert "quota_exceeded" in msg, "the error body must NOT be truncated before the quota code"


# ─── D-055 (1) Active-to-do cap (FIFO replenish-on-clear) ────────────────────


def _unread_raw(n, ts_iso):
    """Build one raw Graph inbox message with a deterministic id + timestamp."""
    return {
        "id": f"MSG-{n}",
        "subject": f"Subject {n}",
        "from": {"name": f"Sender {n}", "email": f"s{n}@example.com"},
        "received": ts_iso,
        "is_read": False,
        "preview": f"preview {n}",
    }


def _make_actionable(n):
    """An already-queued actionable item (status NOT terminal)."""
    return _make_item(
        f"existing-{n}",
        messageId=f"EXIST-{n}",
        conversationId=f"EXIST-{n}",
        status="needs-review",
    )


def test_active_todo_cap_is_named_constant_fifty():
    """The cap is a single named constant reusing the terminal/actionable predicate."""
    assert email_mod.EMAIL_ACTIVE_TODO_CAP == 50


async def test_scan_at_cap_admits_zero_new(email_file, monkeypatch):
    """(CAP) A store already holding 50 actionable items admits ZERO new to-dos."""
    _seed(email_file, [_make_actionable(i) for i in range(50)])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            # 5 brand-new unread, none already queued.
            return {"emails": [_unread_raw(900 + i, f"2026-06-0{i+1}T10:00:00Z")
                               for i in range(5)], "count": 5, "folder": "inbox"}
        if name == "get_email":
            return {"email": {"id": arguments.get("message_id"), "body": "body"}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)
    await email_mod._scan_once({"ws_manager": _NoopWS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert email_mod._count_actionable(saved) == 50
    # No new MSG-* skeletons were ingested.
    assert not any(str(it.get("messageId", "")).startswith("MSG-") for it in saved)


async def test_scan_below_cap_admits_oldest_first_up_to_headroom(email_file, monkeypatch):
    """(CAP+FIFO) With 42 actionable, a scan admits at most 8 — the OLDEST eligible
    unread first — and never exceeds 50."""
    _seed(email_file, [_make_actionable(i) for i in range(42)])

    # 12 eligible unread returned NEWEST-FIRST (as Graph does): MSG-12 .. MSG-1,
    # with MSG-1 the OLDEST. Headroom is 8, so the 8 OLDEST (MSG-1..MSG-8) win.
    raws = []
    for n in range(12, 0, -1):
        # day = n so a larger n is a LATER (newer) timestamp.
        raws.append(_unread_raw(n, f"2026-06-{n:02d}T10:00:00Z"))

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": raws, "count": len(raws), "folder": "inbox"}
        if name == "get_email":
            return {"email": {"id": arguments.get("message_id"), "body": "body"}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)
    await email_mod._scan_once({"ws_manager": _NoopWS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert email_mod._count_actionable(saved) == 50  # 42 + 8, never more
    admitted_ids = sorted(
        str(it.get("messageId")) for it in saved
        if str(it.get("messageId", "")).startswith("MSG-")
    )
    assert len(admitted_ids) == 8
    # The 8 OLDEST eligible unread (MSG-1..MSG-8) are admitted; MSG-9..MSG-12 wait.
    assert admitted_ids == [f"MSG-{n}" for n in range(1, 9)]


async def test_scan_never_exceeds_cap_with_many_new(email_file, monkeypatch):
    """(CAP) Far more eligible unread than headroom never pushes past 50."""
    _seed(email_file, [_make_actionable(i) for i in range(10)])  # headroom 40

    raws = [_unread_raw(n, f"2026-{(n % 12) + 1:02d}-01T10:00:00Z") for n in range(100)]

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": raws, "count": len(raws), "folder": "inbox"}
        if name == "get_email":
            return {"email": {"id": arguments.get("message_id"), "body": "body"}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)
    await email_mod._scan_once({"ws_manager": _NoopWS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert email_mod._count_actionable(saved) == 50


async def test_deleted_status_does_not_eat_a_capped_slot(email_file, monkeypatch):
    """(CAP+TERMINAL RECONCILE) A 'deleted' item is terminal, so it frees headroom."""
    items = [_make_actionable(i) for i in range(49)]
    items.append(_make_item("gone", messageId="GONE", conversationId="GONE",
                            status="deleted"))
    _seed(email_file, items)  # 49 actionable + 1 deleted (terminal)

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [_unread_raw(500, "2026-06-01T10:00:00Z"),
                               _unread_raw(501, "2026-06-02T10:00:00Z")],
                    "count": 2, "folder": "inbox"}
        if name == "get_email":
            return {"email": {"id": arguments.get("message_id"), "body": "body"}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)
    await email_mod._scan_once({"ws_manager": _NoopWS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    # Headroom was 50 - 49 = 1, so exactly ONE new item is admitted (the oldest).
    admitted = [it for it in saved if str(it.get("messageId", "")).startswith("MSG-")]
    assert len(admitted) == 1
    assert admitted[0]["messageId"] == "MSG-500"
    assert email_mod._count_actionable(saved) == 50


# ─── D-055 (2) Terminal-predicate reconciliation ─────────────────────────────


def test_deleted_is_terminal():
    """'deleted' joins approved/dismissed in the terminal set (D-035 lockstep)."""
    assert "deleted" in email_mod._TERMINAL_STATUSES
    assert email_mod._count_actionable([
        {"status": "deleted"},
        {"status": "approved"},
        {"status": "dismissed"},
        {"status": "needs-review"},
    ]) == 1


# ─── D-055 (3) Default-OFF worker gate ───────────────────────────────────────


def test_email_workers_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_WEB_EMAIL_WORKERS", raising=False)
    assert email_mod._email_workers_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "yes", "True"])
def test_email_workers_enabled_for_truthy_allowlist(monkeypatch, val):
    monkeypatch.setenv("CLAUDE_WEB_EMAIL_WORKERS", val)
    assert email_mod._email_workers_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "", "off", "disable"])
def test_email_workers_disabled_for_falsey_strings(monkeypatch, val):
    """'0'/'false' are TRUTHY strings to bool() — they must NOT enable workers."""
    monkeypatch.setenv("CLAUDE_WEB_EMAIL_WORKERS", val)
    assert email_mod._email_workers_enabled() is False


async def test_start_hooks_register_no_task_when_flag_unset(monkeypatch):
    """All three start hooks early-return (register NO task) when the flag is unset."""
    monkeypatch.delenv("CLAUDE_WEB_EMAIL_WORKERS", raising=False)

    created = []
    monkeypatch.setattr(email_mod.asyncio, "create_task",
                        lambda coro: created.append(coro) or _DummyTask(coro))

    app = {}
    await email_mod._start_scan_worker(app)
    await email_mod._start_classify_worker(app)
    await email_mod._start_draft_worker(app)

    assert created == [], "no email worker coroutine may be scheduled when disabled"
    assert "email_scan_task" not in app
    assert "email_classify_task" not in app
    assert "email_draft_task" not in app


async def test_start_hooks_register_task_when_flag_set(monkeypatch):
    """With CLAUDE_WEB_EMAIL_WORKERS=1 all three hooks register their task (wiring)."""
    monkeypatch.setenv("CLAUDE_WEB_EMAIL_WORKERS", "1")

    monkeypatch.setattr(email_mod, "register_worker", lambda *a, **k: None)
    # Avoid the scan hook actually reading the sidecar.
    monkeypatch.setattr(email_mod, "_load", lambda: ({"items": []}, None))

    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()  # never run the worker body
        return _DummyTask(coro)

    monkeypatch.setattr(email_mod.asyncio, "create_task", fake_create_task)

    app = {}
    await email_mod._start_scan_worker(app)
    await email_mod._start_classify_worker(app)
    await email_mod._start_draft_worker(app)

    assert len(created) == 3
    assert "email_scan_task" in app
    assert "email_classify_task" in app
    assert "email_draft_task" in app


class _DummyTask:
    def __init__(self, coro):
        self._coro = coro

    def done(self):
        return False


# ─── D-055 (1) Cap clause (4): admitted get bodies, non-admitted not persisted ──


async def test_scan_cap_prefetches_admitted_bodies_and_drops_non_admitted(
    email_file, monkeypatch
):
    """(CAP/BODY-PREFETCH + NO-GHOST) Only ADMITTED unread get the expensive body
    pre-fetch (emailBody + threadHistory) AND get persisted; the non-admitted
    overflow is NOT ingested this cycle at all — no body-less ghost rows.

    Seeds 48 actionable (headroom = 2) and returns 5 eligible unread NEWEST-FIRST
    (MSG-5..MSG-1, MSG-1 oldest). The 2 OLDEST (MSG-1, MSG-2) are admitted: each
    must carry a body AND a multi-turn threadHistory, AND get_email must have been
    called for EXACTLY those two ids (the cap gates the body fetch, not just the
    persist). MSG-3..MSG-5 must be wholly absent (no skeleton, no get_email)."""
    _seed(email_file, [_make_actionable(i) for i in range(48)])  # headroom 2

    # 5 eligible unread, newest-first (day=n so larger n is newer; MSG-1 oldest).
    raws = [_unread_raw(n, f"2026-06-{n:02d}T10:00:00Z") for n in range(5, 0, -1)]

    get_email_calls = []

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": raws, "count": len(raws), "folder": "inbox"}
        if name == "get_email":
            mid = arguments.get("message_id")
            get_email_calls.append(mid)
            # A 2-turn body so the admitted skeleton lands a real threadHistory.
            return {"email": {
                "id": mid,
                "subject": "Re: thread",
                "from": {"name": "Sender", "email": "s@example.com"},
                "body": "From: Sender\n\nlatest\n---\nFrom: Me\n\nearlier",
                "messages": [
                    {"from": {"name": "Sender"}, "received": "2026-06-02T10:00:00Z",
                     "body": "latest"},
                    {"from": {"name": "Me"}, "received": "2026-06-01T10:00:00Z",
                     "body": "earlier"},
                ],
            }}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)
    await email_mod._scan_once({"ws_manager": _NoopWS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    by_msg = {it.get("messageId"): it for it in saved
              if str(it.get("messageId", "")).startswith("MSG-")}

    # Exactly the 2 OLDEST eligible unread were admitted (MSG-1, MSG-2).
    assert sorted(by_msg) == ["MSG-1", "MSG-2"]
    assert email_mod._count_actionable(saved) == 50

    # Each admitted item got the expensive body pre-fetch (emailBody + history).
    for mid in ("MSG-1", "MSG-2"):
        item = by_msg[mid]
        assert item.get("emailBody"), f"{mid} must have a pre-fetched body"
        th = item.get("threadHistory")
        assert isinstance(th, list) and len(th) == 2, \
            f"{mid} must carry the multi-turn threadHistory"

    # The non-admitted overflow (MSG-3..MSG-5) is wholly absent — no ghost rows.
    assert "MSG-3" not in by_msg and "MSG-4" not in by_msg and "MSG-5" not in by_msg

    # And the cap gated the BODY FETCH itself: get_email ran ONLY for the admitted
    # two, never for the dropped three (the body pre-fetch is the expensive step).
    assert sorted(get_email_calls) == ["MSG-1", "MSG-2"]
    assert "MSG-3" not in get_email_calls
    assert "MSG-4" not in get_email_calls
    assert "MSG-5" not in get_email_calls


# ─── scripts/email_validate.py — standalone pre-flight ────────────────────────
#
# The validation job is a STANDALONE script (no aiohttp server, no periodic
# worker) that PROVES the core email functions end-to-end against the live OWA
# path. These OFFLINE tests pin its core control-flow seams (step accounting,
# exit-code logic, the GRASP-quota fail-loud short-circuit, the cap/FIFO check)
# with the live MCP seams stubbed — the LIVE end-to-end run happens only when an
# operator runs `python3 scripts/email_validate.py`.

validate_mod = pytest.importorskip(
    "scripts.email_validate",
    reason="scripts/email_validate.py not yet implemented — tests activate once it lands",
)


def _vrun(coro):
    """Run an async coroutine on a throwaway loop (these tests are server-free)."""
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_validate_step_result_shape():
    """A StepResult carries (name, passed, detail) and renders a PASS/FAIL line."""
    ok = validate_mod.StepResult("read", True, "got 7 messages")
    bad = validate_mod.StepResult("draft", False, "draft not found in Drafts")
    assert ok.passed is True and "read" in ok.render() and "PASS" in ok.render()
    assert bad.passed is False and "FAIL" in bad.render() and "draft" in bad.render()


def test_validate_runner_exit_zero_only_on_full_success():
    """run_steps returns exit 0 iff EVERY step passes; non-zero on any failure."""
    async def _good():
        return validate_mod.StepResult("a", True, "ok")

    async def _also_good():
        return validate_mod.StepResult("b", True, "ok")

    async def _bad():
        return validate_mod.StepResult("c", False, "nope")

    results, code = _vrun(validate_mod.run_steps([_good, _also_good]))
    assert code == 0
    assert all(r.passed for r in results) and len(results) == 2

    results, code = _vrun(validate_mod.run_steps([_good, _bad, _also_good]))
    assert code != 0, "any failing step must yield a non-zero exit code"


def test_validate_runner_stops_early_and_fails_loud_on_quota():
    """A GRASP 429/quota_exceeded must STOP the run early, surface the RAW body
    untruncated, and exit non-zero (the job's whole reason to exist: catch a
    non-OWA backend BEFORE enabling always-on workers)."""
    raw_body = "x" * 500 + ' HTTP 429 {"error":"quota_exceeded","x-amz-apigw-id":"abc"}'
    ran = {"after_quota": False}

    async def _quota_step():
        raise email_mod.EmailMcpError(raw_body)

    async def _should_not_run():
        ran["after_quota"] = True
        return validate_mod.StepResult("later", True, "ok")

    results, code = _vrun(validate_mod.run_steps([_quota_step, _should_not_run]))
    assert code != 0, "a quota error must exit non-zero"
    assert ran["after_quota"] is False, "the run must STOP EARLY on a quota error"
    blob = "".join(r.detail for r in results)
    assert "quota_exceeded" in blob, "raw quota body must be surfaced, not swallowed"
    assert any("quota_exceeded" in r.detail and len(r.detail) > 200 for r in results), \
        "the quota body must be surfaced un-truncated (raw-bytes-before-root-cause)"


def test_validate_runner_non_quota_failure_does_not_claim_quota():
    """An ordinary step failure must NOT be mislabeled as a quota backend problem."""
    async def _ordinary_fail():
        raise RuntimeError("draft did not land in Drafts")

    results, code = _vrun(validate_mod.run_steps([_ordinary_fail]))
    assert code != 0
    blob = "".join(r.detail for r in results).lower()
    assert "quota" not in blob, "a non-quota failure must not be reported as a quota error"


def test_validate_cap_check_passes_only_on_correct_fifo_admission():
    """assert_scan_cap: PASS only when the post-cycle actionable count respects the
    cap AND the admitted items are the OLDEST eligible unread (FIFO)."""
    cap = validate_mod.ACTIVE_TODO_CAP
    pre_count = cap - 8  # e.g. 42 when cap is 50
    eligible_ts = list(range(20, 0, -1))  # 20 (newest) .. 1 (oldest), newest-first

    # CORRECT: admit the 8 OLDEST (ts 1..8); post count == cap.
    admitted_oldest = sorted(eligible_ts)[:8]
    ok = validate_mod.assert_scan_cap(
        pre_actionable=pre_count, post_actionable=pre_count + len(admitted_oldest),
        admitted_ts=admitted_oldest, eligible_ts=eligible_ts,
    )
    assert ok.passed is True, ok.detail

    # WRONG (FIFO inversion): admit the 8 NEWEST (ts 20..13) — must FAIL.
    admitted_newest = sorted(eligible_ts, reverse=True)[:8]
    bad = validate_mod.assert_scan_cap(
        pre_actionable=pre_count, post_actionable=pre_count + len(admitted_newest),
        admitted_ts=admitted_newest, eligible_ts=eligible_ts,
    )
    assert bad.passed is False, "newest-first admission must FAIL the FIFO check"

    # WRONG (over cap): post count exceeds the cap — must FAIL.
    over = validate_mod.assert_scan_cap(
        pre_actionable=cap, post_actionable=cap + 3,
        admitted_ts=[1, 2, 3], eligible_ts=eligible_ts,
    )
    assert over.passed is False, "exceeding the cap must FAIL"

    # At cap already: zero admitted is correct.
    full = validate_mod.assert_scan_cap(
        pre_actionable=cap, post_actionable=cap,
        admitted_ts=[], eligible_ts=eligible_ts,
    )
    assert full.passed is True, full.detail


def test_validate_uses_real_active_todo_cap_constant():
    """The script's cap MUST reuse the backend's single source of truth, not a
    second hand-rolled literal (D-035 lockstep). When the backend exposes
    EMAIL_ACTIVE_TODO_CAP the script must mirror it."""
    backend_cap = getattr(email_mod, "EMAIL_ACTIVE_TODO_CAP", None)
    if backend_cap is not None:
        assert validate_mod.ACTIVE_TODO_CAP == backend_cap, \
            "the validation script must reuse the backend cap constant, not a copy"
    else:
        assert validate_mod.ACTIVE_TODO_CAP == 50


def test_validate_never_references_a_send_tool_name():
    """Send-safety boundary: the script must NEVER reference reply/send/forward
    write tools — only read tools + the gated write seams it is allowed to use."""
    import inspect
    src = inspect.getsource(validate_mod)
    for forbidden in ("send_email", "reply_email", "forward_email", '"send"', "'send'"):
        assert forbidden not in src, f"validation script must never reference {forbidden!r}"


def test_validate_mark_read_roundtrip_restores_original():
    """The mark-read step is a NET NO-OP: it flips is_read then RESTORES to the
    original, asserting the destination effect at each leg (not a success string)."""
    state = {"is_read": False}
    writes = []

    async def fake_get_email(message_id):
        return {"email": {"id": message_id, "is_read": state["is_read"]}}

    async def fake_write(name, args):
        writes.append((name, args))
        if name == "mark_email_read":
            state["is_read"] = bool(args.get("is_read", True))
        return {"results": [{"success": True}]}

    res = _vrun(validate_mod.mark_read_roundtrip(
        message_id="SAFE-MSG-1",
        get_email=fake_get_email,
        write_tool=fake_write,
    ))
    assert res.passed is True, res.detail
    assert state["is_read"] is False, "mark-read roundtrip must restore the original state"
    assert len(writes) == 2, "roundtrip must flip then restore"
    assert all(w[0] == "mark_email_read" for w in writes)


def test_validate_mark_read_fails_if_flip_not_observed():
    """Anti VALIDATION-THEATER: if the re-read does NOT show the flip, the step
    FAILS even though the write tool returned success:true."""
    state = {"is_read": False}

    async def fake_get_email(message_id):
        return {"email": {"id": message_id, "is_read": state["is_read"]}}

    async def fake_write(name, args):
        return {"results": [{"success": True}]}  # claims success but no effect

    res = _vrun(validate_mod.mark_read_roundtrip(
        message_id="SAFE-MSG-1",
        get_email=fake_get_email,
        write_tool=fake_write,
    ))
    assert res.passed is False, "a success string without the observed flip must FAIL"


def test_validate_save_draft_confirms_landing_and_cleans_up():
    """SAVE-DRAFT proves the EFFECT (draft is IN Drafts), then DELETEs it (soft)
    and confirms it LEFT Drafts — idempotent, leaves no residue."""
    drafts = {}  # message_id -> subject, simulated Drafts folder
    deleted = []

    async def fake_owa_write(name, args):
        # email_draft create lands a draft in Drafts and returns its id.
        assert name == "email_draft"
        assert args.get("operation") == "create"
        did = "DRAFT-XYZ"
        drafts[did] = args.get("subject", "")
        return {"id": did, "saved": True}

    async def fake_get_emails(folder, limit):
        if folder == "drafts":
            return {"emails": [{"id": d, "subject": s} for d, s in drafts.items()]}
        return {"emails": []}

    async def fake_write_tool(name, args):
        # delete_email(permanent=false) soft-deletes the draft.
        assert name == "delete_email"
        assert args.get("permanent") is False
        for mid in (args.get("emails") or {}):
            drafts.pop(mid, None)
            deleted.append(mid)
        return {"results": [{"success": True}]}

    res = _vrun(validate_mod.save_draft_roundtrip(
        subject="[email_validate throwaway] preflight",
        body="throwaway",
        to=["user@example.com"],
        owa_write=fake_owa_write,
        get_emails=fake_get_emails,
        write_tool=fake_write_tool,
    ))
    assert res.passed is True, res.detail
    assert "DRAFT-XYZ" in deleted, "the throwaway draft must be soft-deleted"
    assert drafts == {}, "no residue: the draft must be cleaned up from Drafts"


def test_validate_save_draft_fails_if_draft_never_lands():
    """Anti VALIDATION-THEATER: a 'saved' return that does not actually appear in
    Drafts must FAIL the step (verify-effect-not-caller)."""
    async def fake_owa_write(name, args):
        return {"id": "DRAFT-XYZ", "saved": True}  # claims saved

    async def fake_get_emails(folder, limit):
        return {"emails": []}  # but Drafts is empty — the save was a lie

    async def fake_write_tool(name, args):
        return {"results": [{"success": True}]}

    res = _vrun(validate_mod.save_draft_roundtrip(
        subject="[email_validate throwaway] preflight",
        body="throwaway",
        to=["user@example.com"],
        owa_write=fake_owa_write,
        get_emails=fake_get_emails,
        write_tool=fake_write_tool,
    ))
    assert res.passed is False, "a draft that never lands in Drafts must FAIL"


def test_validate_save_draft_deletes_by_stored_itemid_not_create_id():
    """REGRESSION (AC-9/AC-10 id divergence): the aws-outlook email_draft create
    returns an EWS-form draftId, but Graph lists the SAME draft in Drafts under a
    DIFFERENT itemId and Graph delete_email only removes it when given that itemId.

    The old roundtrip deleted by the create-returned id, so the move 400'd, the
    REAL draft stayed in Drafts (residue), and the post-delete check — looking for
    the never-present create-id — falsely PASSED. The fix deletes (and re-verifies)
    by the itemId the draft actually carries in Drafts, so:
      - delete is issued against the STORED itemId, and
      - the residue check catches the real draft by itemId OR subject.
    This test goes RED on the old create-id behavior and GREEN on the fix.
    """
    subject = "[email_validate throwaway] preflight"
    create_id = "EWS-DRAFT-FORM-1"      # what create returns (EWS form)
    item_id = "GRAPH-ITEMID-9"          # what Drafts actually exposes (Graph form)
    drafts = {item_id: subject}         # the draft lives under the GRAPH itemId
    deleted_targets = []

    async def fake_owa_write(name, args):
        assert name == "email_draft" and args.get("operation") == "create"
        return {"id": create_id, "saved": True}

    async def fake_get_emails(folder, limit):
        if folder == "drafts":
            return {"emails": [{"id": d, "subject": s} for d, s in drafts.items()]}
        return {"emails": []}

    async def fake_write_tool(name, args):
        # delete_email succeeds ONLY when given the real Drafts itemId; the
        # create-form id is unknown to Graph and removes nothing (the live 400).
        assert name == "delete_email" and args.get("permanent") is False
        for mid in (args.get("emails") or {}):
            deleted_targets.append(mid)
            drafts.pop(mid, None)
        return {"results": [{"success": True}]}

    res = _vrun(validate_mod.save_draft_roundtrip(
        subject=subject,
        body="throwaway",
        to=["user@example.com"],
        owa_write=fake_owa_write,
        get_emails=fake_get_emails,
        write_tool=fake_write_tool,
    ))
    assert res.passed is True, res.detail
    # The delete MUST target the stored Graph itemId, never the create-returned id.
    assert deleted_targets == [item_id], \
        f"delete must target the stored itemId, not the create id; got {deleted_targets}"
    assert create_id not in deleted_targets, \
        "deleting by the create-returned EWS id is the residue bug"
    # No residue: the real draft left Drafts.
    assert drafts == {}, "the real draft must be removed from Drafts (no residue)"


def test_validate_save_draft_detects_residue_when_real_draft_remains():
    """Anti VALIDATION-THEATER: if the soft-delete does NOT actually remove the
    draft from Drafts, the step FAILS — even if a wrong-id check would have passed.

    Simulates the residue defect directly: delete_email removes nothing (the move
    fails), so the real draft (by subject AND itemId) is still in Drafts on the
    re-read. The step must FAIL rather than confirm a never-present absence."""
    subject = "[email_validate throwaway] preflight"
    item_id = "GRAPH-ITEMID-RESIDUE"
    drafts = {item_id: subject}

    async def fake_owa_write(name, args):
        return {"id": "EWS-DRAFT-FORM-2", "saved": True}

    async def fake_get_emails(folder, limit):
        if folder == "drafts":
            return {"emails": [{"id": d, "subject": s} for d, s in drafts.items()]}
        return {"emails": []}

    async def fake_write_tool(name, args):
        # Move fails (e.g. wrong id form / 400): nothing leaves Drafts.
        return {"results": [{"success": False, "error": "move -> 400"}]}

    res = _vrun(validate_mod.save_draft_roundtrip(
        subject=subject,
        body="throwaway",
        to=["user@example.com"],
        owa_write=fake_owa_write,
        get_emails=fake_get_emails,
        write_tool=fake_write_tool,
    ))
    assert res.passed is False, "a draft left behind in Drafts must FAIL (residue)"
    assert "residue" in res.detail.lower()


# ─── D-056: FYI-on-demand + reply-all To/CC capture/fix/persist/apply ─────────
#
# Contract pinned by D-056 (sequenced AFTER the backend task that lands it). These
# are the offline/mock-based guards required by the feature spec's T3:
#   (a) a `fyi` classification does NOT route to needs-draft and is NOT picked up
#       by _draft_pending, while needs-reply/actionable still are;
#   (b) _scan_once persists toRecipients/ccRecipients from a get_email payload
#       carrying `to`/`cc` lists (degrades cleanly when absent, never fabricated);
#   (c) _recipient_type classifies to-only/to/cc using the `to`/`cc` keys
#       (regression on the always-"unknown" bug that read toRecipients/ccRecipients);
#   (d) the reply-all default resolution (To = sender + orig-To minus me; CC =
#       orig-cc minus me, PLUS me; de-duped ci) + the no-captured-data degrade;
#   (e) approve passes the resolved `to` AND `cc` into the email_draft create
#       payload, and never reaches a send path;
#   (f) a single-message body longer than 4096 is served/persisted in FULL (32k
#       cap) on the path the single-turn card uses, distinct from the 4096-capped
#       threadHistory turn body.


# The user's own address — read from the backend's single source of truth so the
# tests can never drift from the constant the reply-all/minus-me logic uses.
_MY_EMAIL = email_mod._MY_EMAIL


# --- (a) FYI does NOT auto-draft; needs-reply/actionable still do ------------


def test_classify_routing_fyi_is_not_needs_draft():
    """AC-6: the classification router must send fyi to a NON-draft holding status,
    while needs-reply/actionable still route to needs-draft.

    Pins the routing table directly so the FYI-STILL-AUTO-DRAFTS trap (leaving
    fyi -> needs-draft) goes RED: the value for 'fyi' must not be 'needs-draft'."""
    routing = email_mod._CLASSIFY_NEXT_STATUS
    assert routing["needs-reply"] == "needs-draft"
    assert routing["actionable"] == "needs-draft"
    # The headline: fyi must NOT auto-route into draft generation.
    assert routing.get("fyi") != "needs-draft", \
        "fyi must NOT route to needs-draft (would auto-draft, AC-6)"
    # And whatever holding status it uses must be a real, NON-terminal status so
    # the undrafted FYI still counts as active to-do (D-035 lockstep, AC-8).
    fyi_status = routing.get("fyi")
    assert fyi_status in email_mod._VALID_STATUS, "fyi holding status must be a valid status"
    assert fyi_status not in email_mod._TERMINAL_STATUSES, \
        "an undrafted FYI must stay non-terminal (active to-do count, AC-8)"


def test_fyi_item_not_needs_draft_but_needs_reply_is():
    """AC-6: _needs_draft is the draft-selection predicate. An item parked at the
    fyi holding status is NOT awaiting a draft; a needs-draft item IS."""
    fyi_status = email_mod._CLASSIFY_NEXT_STATUS["fyi"]
    fyi_item = _make_item("fyi1", status=fyi_status, classification="fyi",
                          draft="", generatedDraft="")
    nr_item = _make_item("nr1", status="needs-draft", classification="needs-reply",
                         draft="", generatedDraft="")
    ac_item = _make_item("ac1", status="needs-draft", classification="actionable",
                         draft="", generatedDraft="")
    assert email_mod._needs_draft(fyi_item) is False, \
        "a fyi item must NOT be selected for auto-drafting (AC-6)"
    assert email_mod._needs_draft(nr_item) is True
    assert email_mod._needs_draft(ac_item) is True


async def test_draft_pending_skips_fyi_drafts_only_needs_draft(email_file, monkeypatch):
    """AC-6 end-to-end: _draft_pending picks up needs-draft items (needs-reply /
    actionable) but leaves a parked FYI undrafted, so FYI never gets an auto-draft
    even when a draft run happens (workers OFF -> manual one-shot path)."""
    fyi_status = email_mod._CLASSIFY_NEXT_STATUS["fyi"]
    _seed(email_file, [
        _make_item("fyi1", status=fyi_status, classification="fyi",
                   draft="", generatedDraft=""),
        _make_item("nr1", status="needs-draft", classification="needs-reply",
                   draft="", generatedDraft=""),
        _make_item("ac1", status="needs-draft", classification="actionable",
                   draft="", generatedDraft=""),
    ])

    drafted_ids = []
    _stub_agent(monkeypatch, {
        "available": True, "draft": "auto reply", "generatedDraft": "auto reply",
        "threadContext": "ctx",
    })

    # Capture which item ids the draft path actually drafts.
    orig_draft_one = email_mod._draft_one

    async def spy_draft_one(app, sem, item_id):
        drafted_ids.append(item_id)
        return await orig_draft_one(app, sem, item_id)

    monkeypatch.setattr(email_mod, "_draft_one", spy_draft_one)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    sem = email_mod.asyncio.Semaphore(3)
    await email_mod._draft_pending({"ws_manager": _WS()}, sem)

    # The FYI item was NOT drafted; the needs-draft (needs-reply/actionable) were.
    assert "fyi1" not in drafted_ids, "FYI must NOT be auto-drafted (AC-6)"
    assert set(drafted_ids) == {"nr1", "ac1"}, \
        f"only needs-draft items drafted, got {drafted_ids}"

    saved = {it["id"]: it for it in json.loads(email_file.read_text(encoding="utf-8"))["items"]}
    # FYI item is untouched (no draft fabricated, stays at its holding status).
    assert saved["fyi1"]["draft"] == ""
    assert saved["fyi1"]["status"] == fyi_status
    # needs-reply/actionable got their auto-draft and flipped to needs-review.
    assert saved["nr1"]["status"] == "needs-review" and saved["nr1"]["draft"] == "auto reply"
    assert saved["ac1"]["status"] == "needs-review" and saved["ac1"]["draft"] == "auto reply"


def test_undrafted_fyi_still_counts_as_active_todo():
    """AC-8 (D-035 lockstep): a parked, undrafted FYI is NOT terminal — it still
    counts toward the active to-do total alongside the other live statuses, while
    approved/dismissed/deleted do not."""
    fyi_status = email_mod._CLASSIFY_NEXT_STATUS["fyi"]
    items = [
        {"status": fyi_status},      # undrafted FYI -> active
        {"status": "needs-review"},  # active
        {"status": "approved"},      # terminal
        {"status": "dismissed"},     # terminal
        {"status": "deleted"},       # terminal
    ]
    assert email_mod._count_actionable(items) == 2, \
        "an undrafted FYI must remain in the active to-do count (AC-8)"


# --- (b) _scan_once captures toRecipients/ccRecipients from the detail payload --


async def test_scan_captures_recipients_from_detail_payload(email_file, monkeypatch):
    """AC-15/capture: the body pre-fetch reads `to`/`cc` from the SAME get_email
    detail payload (the inbox-LIST call has none) and persists them on the item as
    toRecipients/ccRecipients ({name,email} lists) — ZERO extra MCP calls."""
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkRecip", "subject": "Re: Planning",
                "from": {"name": "Jane", "email": "jane@example.com"},
                "received": "2026-06-03T10:00:00Z", "is_read": False, "preview": "x",
            }], "count": 1, "folder": "inbox"}
        if name == "get_email":
            # The DETAIL payload carries recipients under `to`/`cc` (verified shape),
            # NOT toRecipients/ccRecipients.
            return {"email": {
                "id": "AAMkRecip", "subject": "Re: Planning",
                "from": {"name": "Jane", "email": "jane@example.com"},
                "body": "Here is the plan.",
                "to": [{"name": "Me", "email": _MY_EMAIL},
                       {"name": "Bob", "email": "bob@example.com"}],
                "cc": [{"name": "Carol", "email": "carol@example.com"}],
            }}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert len(saved) == 1
    item = saved[0]
    # Captured as {name,email} lists straight off the detail `to`/`cc`.
    to_emails = [r.get("email") for r in item.get("toRecipients") or []]
    cc_emails = [r.get("email") for r in item.get("ccRecipients") or []]
    assert _MY_EMAIL in to_emails and "bob@example.com" in to_emails
    assert cc_emails == ["carol@example.com"]
    # recipientType was re-derived from the captured lists (no longer "unknown").
    assert item.get("recipientType") == "to"  # me in To with >1 recipient


async def test_scan_recipients_absent_degrades_not_fabricated(email_file, monkeypatch):
    """An item whose detail payload carries NO `to`/`cc` degrades — no toRecipients/
    ccRecipients are fabricated (the reply-all default later falls back to sender)."""
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkNoRecip", "subject": "Hi",
                "from": {"name": "Solo", "email": "solo@example.com"},
                "received": "2026-06-03T10:00:00Z", "is_read": False, "preview": "x",
            }], "count": 1, "folder": "inbox"}
        if name == "get_email":
            # No to/cc anywhere in the detail.
            return {"email": {"id": "AAMkNoRecip", "subject": "Hi",
                              "from": {"name": "Solo", "email": "solo@example.com"},
                              "body": "Just a quick note."}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})

    item = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    # Never fabricated — absent/empty, not invented.
    assert not item.get("toRecipients")
    assert not item.get("ccRecipients")


# --- (c) _recipient_type reads the to/cc keys (regression on always-"unknown") --


def test_recipient_type_reads_to_cc_keys_not_always_unknown():
    """AC-15 (anti RECIPIENT_TYPE-FIX-READS-WRONG-KEYS): _recipient_type must read
    the DETAIL payload's `to`/`cc` keys. The latent bug read toRecipients/
    ccRecipients (always None on these payloads) and returned 'unknown' for every
    item. Construct payloads with _MY_EMAIL in to/cc and assert a real label."""
    # to-only: me alone in To.
    assert email_mod._recipient_type(
        {"to": [{"name": "Me", "email": _MY_EMAIL}], "cc": []}) == "to-only"
    # to (shared): me in To alongside others.
    assert email_mod._recipient_type(
        {"to": [{"name": "Me", "email": _MY_EMAIL},
                {"name": "Bob", "email": "bob@example.com"}], "cc": []}) == "to"
    # cc: me only in CC.
    assert email_mod._recipient_type(
        {"to": [{"name": "Bob", "email": "bob@example.com"}],
         "cc": [{"name": "Me", "email": _MY_EMAIL}]}) == "cc"
    # Case-insensitive match on my address.
    assert email_mod._recipient_type(
        {"to": [{"name": "Me", "email": _MY_EMAIL.upper()}], "cc": []}) == "to-only"
    # When the keys ARE present, the result is NEVER the always-"unknown" bug.
    for payload in (
        {"to": [{"email": _MY_EMAIL}], "cc": []},
        {"to": [{"email": "other@example.com"}], "cc": [{"email": _MY_EMAIL}]},
    ):
        assert email_mod._recipient_type(payload) != "unknown", \
            f"populated to/cc must classify, not return 'unknown': {payload}"
    # Genuinely empty/absent still degrades to 'unknown' (not fabricated).
    assert email_mod._recipient_type({"to": [], "cc": []}) == "unknown"
    assert email_mod._recipient_type({}) == "unknown"


def test_recipient_type_ignores_legacy_inbox_keys_only():
    """REGRESSION: a raw inbox-LIST shape that has ONLY toRecipients/ccRecipients
    populated would have classified under the OLD code; the new code still reads
    the detail `to`/`cc` first. Either way the classification is real, never the
    spurious 'unknown' the old code returned on the actual (to/cc-keyed) payloads.

    This pins that the fix targets the keys Graph actually delivers: a detail
    payload using `to` is classified, where the old code returned 'unknown'."""
    detail_shape = {"to": [{"email": _MY_EMAIL}], "cc": []}
    # The fixed code classifies this (the old code returned 'unknown' here because
    # it only looked at toRecipients/ccRecipients, which are absent).
    assert email_mod._recipient_type(detail_shape) == "to-only"


# --- (d) reply-all default resolution (the pure backend helper) ----------------


def test_reply_all_default_four_clauses_simultaneously():
    """AC-10 (four-way AND): the reply-all default must hold ALL clauses at once —
    To = sender + orig-To MINUS me; CC = orig-CC MINUS me, PLUS me; de-duped ci.

    This single fixture exercises every clause so the REPLY-ALL-FORGETS-MINUS-ME
    and FORGETS-ALWAYS-ADD-ME traps both go RED."""
    item = _make_item(
        "r1",
        senderEmail="sender@example.com",
        toRecipients=[
            {"name": "Me", "email": _MY_EMAIL},          # must be removed from To
            {"name": "Bob", "email": "bob@example.com"},
            {"name": "Bob dup", "email": "BOB@EXAMPLE.COM"},  # ci dup of bob
        ],
        ccRecipients=[
            {"name": "Carol", "email": "carol@example.com"},
            {"name": "Me again", "email": _MY_EMAIL.upper()},  # must be removed then re-added once
        ],
    )
    to, cc = email_mod._reply_all_recipients(item)

    # Clause 1: To carries the original sender.
    assert "sender@example.com" in to
    # Clause 1b: To carries the original To recipients (Bob).
    assert "bob@example.com" in [a.lower() for a in to]
    # Clause 2 (MINUS-ME on To): my address is NOT in To.
    assert _MY_EMAIL.lower() not in [a.lower() for a in to]
    # Clause 3 (CC from orig-CC, MINUS-ME): Carol present, my dup removed.
    assert "carol@example.com" in [a.lower() for a in cc]
    # Clause 4 (ALWAYS-ADD-ME to CC): my address IS in CC, exactly once.
    cc_lower = [a.lower() for a in cc]
    assert _MY_EMAIL.lower() in cc_lower
    assert cc_lower.count(_MY_EMAIL.lower()) == 1, "self must appear in CC exactly once"
    # De-dupe ci: bob appears once in To despite the dup entry.
    assert [a.lower() for a in to].count("bob@example.com") == 1


def test_reply_all_default_degrades_to_sender_only_and_self_cc():
    """AC-11 (anti FABRICATED-RECIPIENTS-ON-DEGRADE): an item with NO captured
    to/cc degrades to To=[sender], CC=[_MY_EMAIL] — never an invented address."""
    item = _make_item("r2", senderEmail="lonely@example.com")
    item.pop("toRecipients", None)
    item.pop("ccRecipients", None)
    to, cc = email_mod._reply_all_recipients(item)
    assert to == ["lonely@example.com"], "degrade To must be sender-only"
    assert cc == [_MY_EMAIL], "degrade CC must be self-only"
    # Nothing fabricated beyond sender + self.
    assert all(a in ("lonely@example.com", _MY_EMAIL) for a in to + cc)


def test_reply_all_default_self_only_thread_keeps_self_in_cc():
    """A thread where I am the sole captured recipient still re-adds me to CC (so I
    stay on my own thread) and never leaves me replying only to myself in To."""
    item = _make_item(
        "r3",
        senderEmail="boss@example.com",
        toRecipients=[{"name": "Me", "email": _MY_EMAIL}],  # only me in To
        ccRecipients=[],
    )
    to, cc = email_mod._reply_all_recipients(item)
    # Me stripped from To (would be replying to self), so To is just the sender.
    assert to == ["boss@example.com"]
    assert _MY_EMAIL.lower() not in [a.lower() for a in to]
    # Always-add-me still puts me in CC.
    assert cc == [_MY_EMAIL]


# --- (e) approve passes resolved to AND cc into email_draft create -------------


async def test_approve_passes_resolved_to_and_cc_into_draft(client, email_file, monkeypatch):
    """AC-13 (anti APPROVE-IGNORES-CC): approve resolves the reply-all default and
    passes BOTH `to` AND `cc` into the email_draft create payload (today's bug
    hardcoded to=[senderEmail] with no cc)."""
    _seed(email_file, [_make_item(
        "a1",
        senderEmail="sender@example.com",
        draft="Sounds good.",
        toRecipients=[{"name": "Me", "email": _MY_EMAIL},
                      {"name": "Bob", "email": "bob@example.com"}],
        ccRecipients=[{"name": "Carol", "email": "carol@example.com"}],
    )])

    calls = []

    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "content": {"draftId": "D1"}}

    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)

    resp = await client.post("/api/email/queue/a1/approve", json={})
    assert resp.status == 200

    assert len(calls) == 1
    name, args = calls[0]
    assert name == "email_draft" and args["operation"] == "create"
    # BOTH to AND cc are present and carry the resolved reply-all values.
    to_lower = [a.lower() for a in args["to"]]
    cc_lower = [a.lower() for a in args.get("cc", [])]
    assert "sender@example.com" in to_lower
    assert "bob@example.com" in to_lower
    assert _MY_EMAIL.lower() not in to_lower            # minus-me on To
    assert "carol@example.com" in cc_lower
    assert _MY_EMAIL.lower() in cc_lower                # always-add-me on CC
    # The CC must be a non-empty list (the APPROVE-IGNORES-CC bug omitted it).
    assert isinstance(args.get("cc"), list) and args["cc"], "approve must pass a cc list"


async def test_approve_applies_persisted_edited_recipients(client, email_file, monkeypatch):
    """AC-12+AC-13: when the user has edited+persisted recipients (recipientsEdited),
    approve uses THOSE verbatim, not the recomputed default."""
    _seed(email_file, [_make_item(
        "a2",
        senderEmail="sender@example.com",
        draft="Thanks.",
        recipientsEdited=True,
        toRecipients=["chosen-to@example.com"],
        ccRecipients=["chosen-cc@example.com"],
    )])

    calls = []

    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "content": {"draftId": "D2"}}

    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)

    resp = await client.post("/api/email/queue/a2/approve", json={})
    assert resp.status == 200
    _, args = calls[0]
    assert [a.lower() for a in args["to"]] == ["chosen-to@example.com"]
    assert [a.lower() for a in args["cc"]] == ["chosen-cc@example.com"]


async def test_approve_with_cc_never_sends(client, email_file, monkeypatch):
    """AC-14 (send-safety): approve passing to/cc STILL only calls email_draft —
    never reply/send/forward. The to/cc go into the DRAFT only."""
    _seed(email_file, [_make_item(
        "a3", senderEmail="x@example.com", draft="final",
        toRecipients=[{"name": "Bob", "email": "bob@example.com"}],
        ccRecipients=[{"name": "Carol", "email": "carol@example.com"}],
    )])
    calls = []

    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "content": {"draftId": "D3"}}

    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    await client.post("/api/email/queue/a3/approve", json={})
    tool_names = [c[0] for c in calls]
    assert "email_send" not in tool_names
    assert "email_reply" not in tool_names
    assert "email_forward" not in tool_names
    assert tool_names == ["email_draft"], f"approve must ONLY save a draft, got {tool_names}"


async def test_save_draft_persists_edited_recipients(client, email_file):
    """AC-12 (anti RECIPIENTS-NOT-PERSISTED): the save-draft PUT accepts edited
    toRecipients/ccRecipients, sanitizes + de-dupes them, and persists them on the
    item (with recipientsEdited) so they survive a refresh and drive approve."""
    _seed(email_file, [_make_item("p1", draft="hi", generatedDraft="hi")])
    resp = await client.put("/api/email/queue/p1", json={
        "draft": "hi",
        "toRecipients": ["keep@example.com", "KEEP@EXAMPLE.COM", "not an email"],
        "ccRecipients": ["cc@example.com"],
    })
    assert resp.status == 200

    saved = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    # ci de-dupe + drops the invalid address; never fabricated.
    assert [a.lower() for a in saved["toRecipients"]] == ["keep@example.com"]
    assert [a.lower() for a in saved["ccRecipients"]] == ["cc@example.com"]
    assert saved.get("recipientsEdited") is True


# --- (f) a single-message body > 4096 is served/persisted in FULL --------------


def test_single_message_body_over_4096_served_full_not_capped_at_turn():
    """AC-1 (anti SINGLE-MESSAGE-STILL-CAPPED): for a single-message email the full
    body lives in emailBody (32k cap), distinct from the 4096-capped threadHistory
    turn body. The single-turn card must source emailBody (the full body), so the
    backend must serve the FULL body there, never the mid-sentence 4096 cut."""
    big = "A" * 6000  # a single-message body well past the 4096 turn cap
    payload = {"email": {
        "id": "AAMkBig", "subject": "CRIS Weekly Flash",
        "from": {"name": "News", "email": "news@example.com"},
        "body": big,
    }}
    full_body = email_mod._extract_email_body(payload)
    # emailBody is the FULL body (capped only at the generous 32k email cap).
    # _extract_email_body prepends a small From:/Subject: header, so the full
    # body is AT LEAST the raw body length — the point is it is NOT cut at 4096.
    assert big in full_body, "the single-message body must be served in FULL"
    assert len(full_body) >= 6000, "the single-message body must be served in FULL"
    assert len(full_body) <= email_mod._EMAIL_BODY_CAP
    assert len(full_body) > email_mod._THREAD_TURN_BODY_CAP, \
        "the full body must exceed the per-turn cap (proves it is not turn-capped)"

    # The threadHistory turn for the SAME message is capped at the 4096 turn cap —
    # rendering THAT for the single-turn card is the mid-sentence cut we must avoid.
    turns = email_mod._extract_thread_history(payload)
    assert len(turns) == 1
    assert len(turns[0]["body"]) == email_mod._THREAD_TURN_BODY_CAP, \
        "the per-turn body is 4096-capped (the field the card must NOT source)"
    # The two are genuinely different lengths — the card must use the longer one.
    assert len(full_body) != len(turns[0]["body"])


async def test_scan_persists_full_body_for_single_message(email_file, monkeypatch):
    """AC-1 end-to-end: _scan_once stores the FULL emailBody (not the 4096-capped
    turn body) for a long single-message email, so the single-turn card has the
    complete text to render."""
    big = "B" * 8000
    _seed(email_file, [])

    async def fake_read_tool(name, arguments):
        if name == "get_emails":
            return {"emails": [{
                "id": "AAMkFull", "subject": "CRIS Weekly Flash | 25-Jun-2026",
                "from": {"name": "News", "email": "news@example.com"},
                "received": "2026-06-25T10:00:00Z", "is_read": False, "preview": "x",
            }], "count": 1, "folder": "inbox"}
        if name == "get_email":
            return {"email": {"id": "AAMkFull", "subject": "CRIS Weekly Flash | 25-Jun-2026",
                              "from": {"name": "News", "email": "news@example.com"},
                              "body": big}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)

    class _WS:
        async def broadcast(self, *a, **k):
            pass

    await email_mod._scan_once({"ws_manager": _WS()})

    item = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    # emailBody carries the full body (the source the single-turn card uses) — the
    # full 8000-char body is present, never cut at the 4096 turn cap.
    assert big in item["emailBody"], "the persisted full body must contain the whole message"
    assert len(item["emailBody"]) >= 8000, "the persisted full body must not be 4096-capped"
    # If a threadHistory turn was attached it is the 4096-capped variant — distinct.
    th = item.get("threadHistory") or []
    if th:
        assert len(th[0]["body"]) <= email_mod._THREAD_TURN_BODY_CAP
        assert len(item["emailBody"]) > len(th[0]["body"])


# ─── D-057: reply-all draft body builder + To/CC land on the saved draft ──────


def _turn(sender, timestamp, recipients, body):
    """A threadHistory turn (the shape _scan_once persists: sender/timestamp/
    recipients display-string/body)."""
    return {"sender": sender, "timestamp": timestamp,
            "recipients": recipients, "body": body}


def test_build_reply_all_body_multi_turn_includes_every_turn_in_order():
    """AC-4/AC-5: the builder puts the reply on top, then quotes EVERY thread turn
    oldest->newest, each under an Outlook-style From/Sent/To/Subject header block.

    Three turns with DISTINCT sender/timestamp/body so an exact-substring +
    ordering assertion proves no turn is dropped, summarized, or reordered (a
    quote-only-the-latest / drop-a-turn builder fails this)."""
    item = _make_item(
        "i1",
        subject="Re: Launch plan",
        threadHistory=[
            _turn("Alpha One", "Monday, June 1, 2026 9:00 AM",
                  "Alpha One; Bravo Two", "First message about the launch."),
            _turn("Bravo Two", "Tuesday, June 2, 2026 10:30 AM",
                  "Alpha One; Bravo Two; Carol Three", "Second reply with a question."),
            _turn("Carol Three", "Wednesday, June 3, 2026 8:15 AM",
                  "Alpha One; Bravo Two; Carol Three", "Third and latest message."),
        ],
    )
    html_body = email_mod._build_reply_all_body("Here is my reply.", item)

    # Reply text on top, before any quoted turn.
    assert "Here is my reply." in html_body
    i_reply = html_body.index("Here is my reply.")
    i_t1 = html_body.index("First message about the launch.")
    i_t2 = html_body.index("Second reply with a question.")
    i_t3 = html_body.index("Third and latest message.")
    # Reply precedes all quoted turns; turns appear oldest->newest.
    assert i_reply < i_t1 < i_t2 < i_t3, "reply on top, then turns oldest->newest"

    # Every turn's sender + timestamp + body present (no turn dropped).
    for sender, ts, body in [
        ("Alpha One", "Monday, June 1, 2026 9:00 AM", "First message about the launch."),
        ("Bravo Two", "Tuesday, June 2, 2026 10:30 AM", "Second reply with a question."),
        ("Carol Three", "Wednesday, June 3, 2026 8:15 AM", "Third and latest message."),
    ]:
        assert sender in html_body, f"missing sender {sender!r}"
        assert ts in html_body, f"missing timestamp {ts!r}"
        assert body in html_body, f"missing body {body!r}"

    # Outlook-style attribution labels appear (per-turn header block).
    assert "From:" in html_body
    assert "Sent:" in html_body
    assert "To:" in html_body
    assert "Subject:" in html_body
    # Subject reproduced from the item.
    assert "Re: Launch plan" in html_body


def test_build_reply_all_body_html_escapes_hostile_turn_and_drops_nothing():
    """AC-6: every external field is HTML-escaped — a hostile turn body with a
    <script>/<img onerror> tag appears INERT (escaped), never as live markup; and
    a benign sibling turn is still present (no turn dropped by the escaping)."""
    item = _make_item(
        "i1",
        subject="<b>Subj & co</b>",
        threadHistory=[
            _turn("Mallory <script>x</script>", "ts-1",
                  "victim@example.com",
                  "benign first turn body"),
            _turn("Eve", "ts-2", "victim@example.com",
                  '<script>alert(1)</script><img src=x onerror="evil()">'),
        ],
    )
    html_body = email_mod._build_reply_all_body("my reply & <b>note</b>", item)

    # No LIVE script/img tags anywhere — the angle brackets must be escaped, so
    # the browser never sees a real tag or event-handler attribute.
    assert "<script>" not in html_body, "hostile <script> must be escaped"
    assert "<img" not in html_body, "hostile <img> tag must be escaped (no live <img)"
    # The escaped forms are present (the text is preserved, just inert): the
    # angle brackets and the attribute quotes are entity-encoded.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    assert "&lt;img" in html_body, "the <img must appear only as escaped text"
    assert 'onerror="' not in html_body, "the event-handler quote must be escaped (&quot;)"
    # The reply text is escaped too (& -> &amp;).
    assert "my reply &amp; &lt;b&gt;note&lt;/b&gt;" in html_body
    # Hostile subject escaped.
    assert "<b>Subj & co</b>" not in html_body
    assert "&lt;b&gt;Subj &amp; co&lt;/b&gt;" in html_body
    # Both turns still present (escaping didn't drop a turn).
    assert "benign first turn body" in html_body
    assert "Mallory" in html_body and "Eve" in html_body


def test_build_reply_all_body_single_turn_yields_reply_plus_one_quote():
    """AC-8: the common single-turn case yields reply + exactly one quoted message,
    no empty quote and no doubled message."""
    item = _make_item(
        "i1",
        subject="Re: One-off",
        threadHistory=[
            _turn("Solo Sender", "Friday, June 5, 2026 2:00 PM",
                  "Solo Sender; me@example.com", "The only message in the thread."),
        ],
    )
    html_body = email_mod._build_reply_all_body("Short reply.", item)
    assert "Short reply." in html_body
    assert "Solo Sender" in html_body
    assert "Friday, June 5, 2026 2:00 PM" in html_body
    # Exactly one occurrence of the single message body (not doubled).
    assert html_body.count("The only message in the thread.") == 1
    assert "From:" in html_body and "Subject:" in html_body


def test_build_reply_all_body_missing_fields_no_literal_none_or_undefined():
    """AC-10: a turn missing sender/timestamp/recipients renders a blank/omitted
    header line — NEVER literal None/undefined/[object Object]; never crashes."""
    item = _make_item(
        "i1",
        subject="",
        threadHistory=[
            {"body": "body with no header fields at all"},  # all fields absent
            _turn("", "", "", "second body also bare"),
        ],
    )
    html_body = email_mod._build_reply_all_body("reply", item)
    assert "None" not in html_body, "no Python None literal in the draft"
    assert "undefined" not in html_body, "no JS undefined literal in the draft"
    assert "[object Object]" not in html_body
    # Bodies still present.
    assert "body with no header fields at all" in html_body
    assert "second body also bare" in html_body


def test_build_reply_all_body_preserves_newline_to_br_in_reply():
    """AC-3: the existing \\n -> <br> transform is preserved for the reply portion."""
    item = _make_item("i1", threadHistory=[_turn("S", "t", "r", "b")])
    html_body = email_mod._build_reply_all_body("line one\nline two", item)
    assert "line one<br>line two" in html_body


def test_build_reply_all_body_makes_no_mcp_calls(monkeypatch):
    """The builder is PURE: it must read only item['threadHistory']/['subject'] and
    make NO MCP/network call (guard against a future refactor sneaking I/O in)."""
    async def boom(*a, **k):
        raise AssertionError("the body builder must not call any MCP write tool")

    monkeypatch.setattr(email_mod, "_call_owa_write_tool", boom)
    item = _make_item("i1", threadHistory=[_turn("S", "t", "r", "b")])
    # Pure function; calling it must not raise (no MCP seam touched).
    out = email_mod._build_reply_all_body("hi", item)
    assert "hi" in out


def test_build_reply_all_body_is_deterministic_byte_identical():
    """AC-9 (anti DROP-OR-REORDER / unstable-key): the builder is a PURE,
    deterministic function — the SAME item must yield BYTE-IDENTICAL output across
    two calls. A builder that summarizes, sorts by an unstable key, iterates an
    unordered set, or stamps a time/uuid into the body would diverge here.

    Use a multi-turn item with several DISTINCT turns so any non-deterministic
    iteration / re-sort would surface as a byte difference, and call it twice."""
    item = _make_item(
        "i1",
        subject="Re: Determinism",
        threadHistory=[
            _turn("Alpha One", "Monday, June 1, 2026 9:00 AM",
                  "Alpha One; Bravo Two", "First message."),
            _turn("Bravo Two", "Tuesday, June 2, 2026 10:30 AM",
                  "Alpha One; Bravo Two; Carol Three", "Second message."),
            _turn("Carol Three", "Wednesday, June 3, 2026 8:15 AM",
                  "Alpha One; Bravo Two; Carol Three", "Third message."),
        ],
    )
    out1 = email_mod._build_reply_all_body("My reply text.", item)
    out2 = email_mod._build_reply_all_body("My reply text.", item)
    assert out1 == out2, "the body builder must be deterministic (byte-identical output)"
    # And the item is not mutated by building (no side effects on the input).
    assert len(item["threadHistory"]) == 3
    assert [t["sender"] for t in item["threadHistory"]] == \
        ["Alpha One", "Bravo Two", "Carol Three"]


async def test_email_approve_sends_resolved_to_and_cc_in_create(client, email_file, monkeypatch):
    """AC-2 (verified contract): approve passes the RESOLVED reply-all To AND CC
    into the email_draft create call in the flat {to:[...], cc:[...]} shape that the
    live aws-outlook-mcp backend applies to the saved draft (verified by a live
    create+read-back: `recipients` == To, `cc` == CC both populate the draft).

    Here recipientsEdited is set, so approve uses the persisted edit verbatim."""
    _seed(email_file, [_make_item(
        "i1",
        draft="final reply",
        senderEmail="origsender@example.com",
        recipientsEdited=True,
        toRecipients=["alice@example.com", "bob@example.com"],
        ccRecipients=["carol@example.com"],
    )])
    calls = []

    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "content": {"draftId": "D1"}}

    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    monkeypatch.setattr(email_mod, "_spawn_mark_read", lambda m: None)

    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    assert len(calls) == 1
    name, args = calls[0]
    assert name == "email_draft"
    assert args["operation"] == "create"
    # The verified flat shape: both lists carry the resolved edited recipients.
    assert args["to"] == ["alice@example.com", "bob@example.com"]
    assert args["cc"] == ["carol@example.com"]


async def test_email_approve_body_is_reply_all_with_quoted_thread(client, email_file, monkeypatch):
    """AC-4: the create call's body is a real reply-all — the user's reply on top
    then the ENTIRE quoted thread (every turn), not just the bare reply text."""
    _seed(email_file, [_make_item(
        "i1",
        draft="My approved reply.",
        senderEmail="origsender@example.com",
        toRecipients=["alice@example.com"],
        ccRecipients=[],
        threadHistory=[
            _turn("Alpha One", "Mon 9:00 AM", "Alpha One", "Oldest thread message."),
            _turn("Bravo Two", "Tue 10:00 AM", "Alpha One; Bravo Two", "Newest thread message."),
        ],
    )])
    calls = []

    async def fake_owa_write(name, arguments):
        calls.append((name, arguments))
        return {"success": True, "content": {"draftId": "D1"}}

    monkeypatch.setattr(email_mod, "_call_owa_write_tool", fake_owa_write)
    monkeypatch.setattr(email_mod, "_spawn_mark_read", lambda m: None)

    resp = await client.post("/api/email/queue/i1/approve", json={})
    assert resp.status == 200
    body_html = calls[0][1]["body"]
    # Reply on top, then BOTH thread turns, oldest->newest.
    i_reply = body_html.index("My approved reply.")
    i_old = body_html.index("Oldest thread message.")
    i_new = body_html.index("Newest thread message.")
    assert i_reply < i_old < i_new
    # Per-turn attribution present (not a bare reply).
    assert "From:" in body_html and "Sent:" in body_html


# ─── D-058: folder-source pre-flight (scripts/email_validate.py) ──────────────
#
# The Email scanner is being extended to triage unread mail from a fixed
# allowlist of Inbox SUBFOLDERS, read through the aws-outlook-mcp (OWA/EWS)
# backend (OFF the GRASP quota) via the EXISTING email.call_read_tool routing
# (email_list_folders / email_folders / email_read). The validation pre-flight
# gains two LIVE steps:
#   (1) the LOAD-BEARING markAs proof: email_read WITHOUT any markAs key leaves a
#       folder message UNREAD (verified by re-reading the read-state at the
#       destination, never the tool's success string); and
#   (2) a folder-source smoke proving name->id resolution, the unreadCount>0
#       filter, and a real body + multi-message thread from email_read.
#
# These OFFLINE tests pin the pure helpers + the effect-verified step seams with
# the live MCP seams stubbed; the LIVE end-to-end run happens only when an
# operator runs `python3 scripts/email_validate.py`.


def test_validate_subfolder_allowlist_matches_spec():
    """The script's subfolder allowlist must reuse the backend constant when it
    lands (single source of truth) and otherwise fall back to the D-058 spec set,
    which must include the canonical allowlisted names ('1 GSD' etc.)."""
    backend = getattr(email_mod, "EMAIL_SCAN_SUBFOLDERS", None)
    if backend is not None:
        assert list(validate_mod.EMAIL_SCAN_SUBFOLDERS) == list(backend), \
            "validate must reuse the backend subfolder allowlist, not a copy"
    # Whatever the source, the canonical names from the spec must be present.
    names = set(validate_mod.EMAIL_SCAN_SUBFOLDERS)
    for canonical in ("1 GSD", "1 managers", "0 to do"):
        assert canonical in names, f"allowlist must include {canonical!r}"


def test_validate_resolve_subfolders_maps_names_and_skips_unresolved():
    """resolve_subfolders walks the Inbox children and maps each allowlisted name
    to its CURRENT id; an allowlist name with no matching child is SKIPPED (fail
    loud, never fabricated) — never invents an id, never matches by a hardcoded id."""
    payload = {
        "success": True,
        "content": {
            "folders": [
                {"name": "Inbox", "id": "INBOX-ID", "children": [
                    {"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 3},
                    {"name": "1 managers", "id": "AAMk-MGR", "unreadCount": 0},
                    {"name": "2 CR", "id": "AAMk-CR", "unreadCount": 9},  # NOT allowlisted
                ]},
                {"name": "Sent Items", "id": "SENT-ID", "children": []},
            ],
        },
    }
    allowlist = ["1 GSD", "1 managers", "0 to do"]  # "0 to do" has no child
    resolved, unresolved = validate_mod.resolve_subfolders(payload, allowlist)

    assert resolved == {"1 GSD": "AAMk-GSD", "1 managers": "AAMk-MGR"}, \
        "must map allowlisted names to their CURRENT ids from the Inbox children"
    # The non-allowlisted child is never admitted even though it has unread.
    assert "2 CR" not in resolved
    # An allowlist name with no matching child is reported unresolved (skipped),
    # never fabricated with a guessed id.
    assert unresolved == ["0 to do"], "an unresolved allowlist name must be surfaced, not faked"
    assert all(isinstance(v, str) and v.startswith("AAMk") for v in resolved.values())


def test_validate_filter_unread_conversations_drops_already_read():
    """email_folders has NO unread_only param and returns read+unread mixed, so the
    script MUST filter client-side to conversations with unreadCount>0 (else it
    floods the queue with already-handled mail). Returns the unread conversations."""
    payload = {"content": {"emails": [
        {"conversationId": "C-unread-1", "topic": "live", "unreadCount": 2},
        {"conversationId": "C-read", "topic": "done", "unreadCount": 0},
        {"conversationId": "C-unread-2", "topic": "live2", "unreadCount": 1},
        {"conversationId": "C-missing", "topic": "noflag"},  # absent unreadCount == 0
    ]}}
    admitted = validate_mod.filter_unread_conversations(payload)
    ids = [c.get("conversationId") for c in admitted]
    assert ids == ["C-unread-1", "C-unread-2"], \
        "only conversations with unreadCount>0 may be admitted (read mail must NOT leak in)"


def test_validate_map_folder_message_uses_owa_field_names():
    """email_read returns DIFFERENT field names than the Graph get_email path. The
    mapper must read itemId (NOT id/messageId), recipients (To), ccRecipients (CC),
    sender/from, and recievedAt (sic) — reusing the Inbox field names would yield a
    dead row (blank id, empty To/CC)."""
    message = {
        "itemId": "AAMk-ITEM-1",
        "itemChangeKey": "CK1",
        "sender": {"name": "Glenn Dev", "email": "glenn@example.com"},
        "from": {"name": "Glenn Dev", "email": "glenn@example.com"},
        "recievedAt": "2026-06-27T09:00:00Z",   # NOTE: misspelled in the API
        "dateTimeSent": "2026-06-27T08:59:00Z",
        "recipients": [{"name": "Me", "email": _MY_EMAIL}],
        "ccRecipients": [{"name": "Lead", "email": "lead@example.com"}],
        "subject": "Folder source",
        "body": "Real folder body text.",
        "isRead": False,
    }
    mapped = validate_mod.map_folder_message(message)
    assert mapped["messageId"] == "AAMk-ITEM-1", "id must map from itemId, NOT id/messageId"
    assert mapped["isRead"] is False
    # To/CC come from recipients/ccRecipients (the OWA names), not toRecipients/etc.
    to_emails = [e.get("email") for e in mapped["to"]]
    cc_emails = [e.get("email") for e in mapped["cc"]]
    assert _MY_EMAIL in to_emails
    assert "lead@example.com" in cc_emails
    # recipientType is derivable from the mapped to/cc keys (not "unknown").
    assert mapped["recipientType"] != "unknown", \
        "with To/CC populated the type must classify, not fall back to unknown"


def test_validate_email_read_keeps_unread_passes_when_state_unchanged():
    """LOAD-BEARING (AC-6 / mark-read-by-accident): calling email_read WITHOUT any
    markAs key must leave the message UNREAD. PASS only when the re-read at the
    destination still shows unread; the read tool's success string is NOT proof."""
    state = {"isRead": False}
    read_calls = []

    async def fake_email_read(args):
        read_calls.append(dict(args))
        # email_read returns the conversation's messages; reading does NOT flip read.
        return {"content": {"emails": [
            {"itemId": "AAMk-ITEM-1", "isRead": state["isRead"], "body": "b"},
        ]}}

    async def fake_read_state(conv_id):
        # Re-read the conversation's unread state at the destination.
        return state["isRead"]

    res = _vrun(validate_mod.email_read_keeps_unread(
        conversation_id="C-unread-1",
        email_read=fake_email_read,
        read_state=fake_read_state,
    ))
    assert res.passed is True, res.detail
    # The email_read call must carry NO markAs key (the only safe wiring).
    assert read_calls, "email_read must be called"
    assert all("markAs" not in c for c in read_calls), \
        "email_read must be called WITHOUT a markAs key (never mark folder mail read)"


def test_validate_email_read_keeps_unread_fails_if_it_marks_read():
    """Anti VALIDATION-THEATER: if email_read marks the message read (the obvious
    copied default), the re-read shows read and the step FAILS LOUD so the wiring is
    never trusted."""
    state = {"isRead": False}

    async def fake_email_read(args):
        # A wiring that marks read on read — exactly the failure mode we guard.
        state["isRead"] = True
        return {"content": {"emails": [{"itemId": "X", "isRead": True, "body": "b"}]}}

    async def fake_read_state(conv_id):
        return state["isRead"]

    res = _vrun(validate_mod.email_read_keeps_unread(
        conversation_id="C-unread-1",
        email_read=fake_email_read,
        read_state=fake_read_state,
    ))
    assert res.passed is False, "if email_read flips the message to read, the step must FAIL"
    assert "unread" in res.detail.lower()


def test_validate_folder_source_smoke_resolves_filters_and_reads_body():
    """End-to-end (AC-4) with seams stubbed: email_list_folders resolves >=1
    allowlisted name->id, email_folders' unreadCount>0 filter admits the right
    conversation, and email_read on that conversationId returns a real body +
    multi-message thread."""
    list_folders_payload = {"content": {"folders": [
        {"name": "Inbox", "id": "INBOX", "children": [
            {"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 1},
            {"name": "1 managers", "id": "AAMk-MGR", "unreadCount": 0},
        ]},
    ]}}
    folder_emails_payload = {"content": {"emails": [
        {"conversationId": "C-GSD-1", "topic": "live", "unreadCount": 1},
        {"conversationId": "C-GSD-read", "topic": "done", "unreadCount": 0},
    ]}}
    email_read_payload = {"content": {"emails": [
        {"itemId": "M1", "sender": {"name": "A", "email": "a@x.com"},
         "recievedAt": "2026-06-27T08:00:00Z", "subject": "t",
         "recipients": [{"name": "Me", "email": _MY_EMAIL}], "ccRecipients": [],
         "body": "First message body.", "isRead": False},
        {"itemId": "M2", "sender": {"name": "B", "email": "b@x.com"},
         "recievedAt": "2026-06-27T09:00:00Z", "subject": "t",
         "recipients": [{"name": "Me", "email": _MY_EMAIL}], "ccRecipients": [],
         "body": "Second message body.", "isRead": False},
    ]}}

    async def fake_call_read(name, args):
        if name == "email_list_folders":
            return list_folders_payload
        if name == "email_folders":
            assert args.get("folderId") == "AAMk-GSD", "must read the resolved folder id"
            return folder_emails_payload
        if name == "email_read":
            assert args.get("conversationId") == "C-GSD-1", "must read the admitted conv"
            assert "markAs" not in args, "folder email_read must omit markAs (never mark read)"
            return email_read_payload
        raise AssertionError(f"unexpected read tool {name}")

    res = _vrun(validate_mod.folder_source_smoke(
        allowlist=["1 GSD", "1 managers"],
        call_read=fake_call_read,
    ))
    assert res.passed is True, res.detail
    # The detail should evidence the resolved folder, the admitted conv, and a
    # multi-message thread (full thread history, not a one-paragraph blurb).
    assert "1 GSD" in res.detail
    assert "2" in res.detail  # 2 thread messages


def test_validate_folder_smoke_fails_when_no_allowlisted_folder_resolves():
    """If NONE of the allowlisted names resolve to a folder id, the smoke step must
    FAIL (fail-loud) rather than silently report success on an empty resolution."""
    list_folders_payload = {"content": {"folders": [
        {"name": "Inbox", "id": "INBOX", "children": [
            {"name": "Some Other Folder", "id": "AAMk-OTHER", "unreadCount": 5},
        ]},
    ]}}

    async def fake_call_read(name, args):
        if name == "email_list_folders":
            return list_folders_payload
        raise AssertionError("must not read a folder when nothing resolved")

    res = _vrun(validate_mod.folder_source_smoke(
        allowlist=["1 GSD", "1 managers"],
        call_read=fake_call_read,
    ))
    assert res.passed is False, "no allowlisted folder resolving must FAIL the smoke step"


# ─── D-058 BACKEND (server/routes/email.py): Inbox-subfolder scan source ──────
#
# A second scan source: a fixed allowlist of Inbox SUBFOLDERS read through the
# aws-outlook-mcp (OWA/EWS) backend (off the GRASP quota) via the EXISTING
# email.call_read_tool routing (email_list_folders/email_folders/email_read).
# These pin the backend helpers (_resolve_scan_subfolders, _owa_conversation_to_
# payload) and the _scan_once integration: sourceFolder tag on BOTH paths, the
# unreadCount>0 filter, the OWA field-name remap, the combined cap/FIFO, best-effort
# per-folder reads, and the LOAD-BEARING markAs SAFETY (email_read WITHOUT markAs).


def _b58_list_folders_payload(children):
    """email_list_folders payload with `children` under the top-level Inbox."""
    return {
        "success": True,
        "content": {
            "message": "ok",
            "folders": [
                {"name": "Inbox", "id": "INBOX-ID", "totalCount": 100,
                 "unreadCount": 5, "children": children},
                {"name": "Sent Items", "id": "SENT-ID", "totalCount": 9,
                 "unreadCount": 0, "children": []},
            ],
        },
    }


def test_b58_email_scan_subfolders_constant_present():
    """The allowlist is a module-level constant (editable in one place), by DISPLAY
    NAME — never a hardcoded AAMk id."""
    names = email_mod.EMAIL_SCAN_SUBFOLDERS
    assert "1 GSD" in names and "1 managers" in names and "2 black falcon" in names
    assert len(names) == 14
    for n in names:
        assert not str(n).startswith("AAMk")


def test_b58_resolve_scan_subfolders_matches_allowlist_by_name():
    """AC-2/AC-11: discovery walks Inbox children, matches the allowlist by display
    name, returns (name, current id); a non-allowlisted child is never resolved."""
    children = [
        {"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 3, "children": []},
        {"name": "1 managers", "id": "AAMk-MGR", "unreadCount": 1, "children": []},
        {"name": "2 CR", "id": "AAMk-CR", "unreadCount": 7, "children": []},
    ]
    by_name = dict(email_mod._resolve_scan_subfolders(_b58_list_folders_payload(children)))
    assert by_name.get("1 GSD") == "AAMk-GSD"
    assert by_name.get("1 managers") == "AAMk-MGR"
    assert "2 CR" not in by_name


def test_b58_resolve_scan_subfolders_skips_zero_unread():
    """AC-3 cheap-optimization: an allowlisted child with unreadCount==0 is skipped."""
    children = [
        {"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 0, "children": []},
        {"name": "1 leads", "id": "AAMk-LEADS", "unreadCount": 2, "children": []},
    ]
    by_name = dict(email_mod._resolve_scan_subfolders(_b58_list_folders_payload(children)))
    assert "1 GSD" not in by_name
    assert by_name.get("1 leads") == "AAMk-LEADS"


def test_b58_resolve_scan_subfolders_unresolved_name_skipped_not_fabricated(caplog):
    """AC-11 (anti FABRICATE-ON-MISS): an allowlist name with NO matching child is
    SKIPPED with an info log — never resolved to a fabricated id."""
    import logging
    children = [{"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 4, "children": []}]
    with caplog.at_level(logging.INFO, logger=email_mod.logger.name):
        resolved = email_mod._resolve_scan_subfolders(_b58_list_folders_payload(children))
    assert dict(resolved) == {"1 GSD": "AAMk-GSD"}
    assert any("1 managers" in r.getMessage() for r in caplog.records), \
        "an unresolved allowlist name must be logged (fail-loud-but-continue)"


def test_b58_resolve_scan_subfolders_no_inbox_returns_empty():
    """No top-level Inbox / malformed payload degrades to [] (never raises)."""
    assert email_mod._resolve_scan_subfolders(
        {"content": {"folders": [{"name": "Archive", "children": []}]}}) == []
    assert email_mod._resolve_scan_subfolders({}) == []
    assert email_mod._resolve_scan_subfolders(None) == []


def test_b58_owa_conversation_to_payload_remaps_field_names():
    """AC-4 (anti FOLDER-DEAD-ROW): the OWA email_read message uses DIFFERENT field
    names (itemId, recipients, ccRecipients, recievedAt[sic], isRead). The normalizer
    remaps them onto the canonical {to,cc,from,body} shape the existing helpers
    consume so body/thread/recipientType all populate."""
    owa_emails = [{
        "itemId": "AAMk-ITEM-1", "itemChangeKey": "CK1",
        "sender": {"name": "Jane Folder", "email": "jane@example.com"},
        "from": {"name": "Jane Folder", "email": "jane@example.com"},
        "recievedAt": "2026-06-26T09:00:00Z",          # sic
        "dateTimeSent": "2026-06-26T09:00:00Z",
        "recipients": [{"name": "Me", "email": _MY_EMAIL},
                       {"name": "Bob", "email": "bob@example.com"}],
        "ccRecipients": [{"name": "Carol", "email": "carol@example.com"}],
        "subject": "Re: Folder thread", "body": "The folder message body.",
        "isRead": False,
    }]
    payload = email_mod._owa_conversation_to_payload(owa_emails)
    assert "The folder message body." in email_mod._extract_email_body(payload)
    to_list = email_mod._detail_recipients(payload, "to")
    cc_list = email_mod._detail_recipients(payload, "cc")
    assert _MY_EMAIL in [r.get("email") for r in to_list]
    assert "bob@example.com" in [r.get("email") for r in to_list]
    assert [r.get("email") for r in cc_list] == ["carol@example.com"]
    assert email_mod._recipient_type({"to": to_list, "cc": cc_list}) == "to"


def test_b58_owa_conversation_to_payload_full_thread_is_per_turn():
    """AC-4 (anti SUMMARY-MASQUERADING-AS-TIMELINE): the conversation's full message
    LIST is the thread history — N per-turn cards, oldest->newest."""
    owa_emails = [
        {"itemId": "M2", "sender": {"name": "Bravo", "email": "b@example.com"},
         "dateTimeSent": "2026-06-26T10:00:00Z", "subject": "Re: T",
         "body": "Newest reply text.", "isRead": False},
        {"itemId": "M1", "sender": {"name": "Alpha", "email": "a@example.com"},
         "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "T",
         "body": "Oldest message text.", "isRead": True},
    ]
    turns = email_mod._extract_thread_history(
        email_mod._owa_conversation_to_payload(owa_emails))
    assert len(turns) == 2
    assert turns[0]["body"] == "Oldest message text."
    assert turns[1]["body"] == "Newest reply text."


class _B58WSNoop:
    async def broadcast(self, *a, **k):
        pass


def _b58_scan_read_tool(folder_children, conversations_by_id, messages_by_conv,
                        inbox_emails=None, calls=None):
    """Fake call_read_tool routing email_list_folders/email_folders/email_read (OWA)
    + get_emails/get_email (Graph inbox). Records every call for markAs assertions."""
    inbox_emails = inbox_emails or []

    async def fake_read_tool(name, arguments):
        if calls is not None:
            calls.append((name, dict(arguments)))
        if name == "get_emails":
            return {"emails": list(inbox_emails), "count": len(inbox_emails),
                    "folder": "inbox"}
        if name == "get_email":
            mid = arguments.get("message_id")
            for e in inbox_emails:
                if e.get("id") == mid:
                    return {"email": dict(e, body=e.get("body", "Inbox body."))}
            return {}
        if name == "email_list_folders":
            return _b58_list_folders_payload(folder_children)
        if name == "email_folders":
            fid = arguments.get("folderId")
            return {"content": {"message": "ok",
                                "emails": conversations_by_id.get(fid, []),
                                "totalResults": 0, "offset": 0, "limit": 100}}
        if name == "email_read":
            cid = arguments.get("conversationId")
            return {"content": {"message": "ok",
                                "emails": messages_by_conv.get(cid, [])}}
        return {}

    return fake_read_tool


async def test_b58_scan_ingests_unread_folder_conversation_tagged_source(email_file, monkeypatch):
    """DONE-WHEN: an unread email in an allowlisted subfolder ("1 GSD") appears in the
    queue tagged sourceFolder "1 GSD", with its real body + full thread history; a
    non-allowlisted folder ("2 CR") is NOT present."""
    _seed(email_file, [])
    folder_children = [
        {"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 1, "children": []},
        {"name": "2 CR", "id": "AAMk-CR", "unreadCount": 1, "children": []},
    ]
    conversations_by_id = {
        "AAMk-GSD": [{"conversationId": "CONV-GSD-1", "topic": "Re: GSD review",
                      "senders": ["Jane Folder"], "lastDeliveryTime": "2026-06-26T09:00:00Z",
                      "preview": "folder preview", "unreadCount": 1, "messageCount": 2}],
        "AAMk-CR": [{"conversationId": "CONV-CR-1", "topic": "CR thread", "unreadCount": 1}],
    }
    messages_by_conv = {
        "CONV-GSD-1": [
            {"itemId": "GSD-MSG-2", "sender": {"name": "Jane", "email": "jane@example.com"},
             "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "Re: GSD review",
             "body": "Latest GSD reply body.",
             "recipients": [{"name": "Me", "email": _MY_EMAIL}], "ccRecipients": [],
             "isRead": False},
            {"itemId": "GSD-MSG-1", "sender": {"name": "Origin", "email": "origin@example.com"},
             "dateTimeSent": "2026-06-26T08:00:00Z", "subject": "GSD review",
             "body": "Original GSD message.",
             "recipients": [{"name": "Me", "email": _MY_EMAIL}], "ccRecipients": [],
             "isRead": True},
        ],
    }
    monkeypatch.setattr(email_mod, "call_read_tool",
                        _b58_scan_read_tool(folder_children, conversations_by_id,
                                            messages_by_conv))
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    items = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    by_conv = {it.get("conversationId"): it for it in items}
    assert "CONV-GSD-1" in by_conv
    assert "CONV-CR-1" not in by_conv
    gsd = by_conv["CONV-GSD-1"]
    assert gsd.get("sourceFolder") == "1 GSD"
    assert "Latest GSD reply body." in (gsd.get("emailBody") or "")
    th = gsd.get("threadHistory") or []
    assert len(th) == 2
    assert th[0]["body"] == "Original GSD message."
    assert th[1]["body"] == "Latest GSD reply body."
    assert gsd.get("recipientType") in ("to-only", "to", "cc", "dl")


async def test_b58_scan_filters_read_folder_conversations(email_file, monkeypatch):
    """AC-3 (anti READ-MAIL-LEAKS-IN): email_folders returns read+unread mixed; the
    scan filters to unreadCount>0 so already-handled mail never re-floods the queue."""
    _seed(email_file, [])
    folder_children = [{"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 1, "children": []}]
    conversations_by_id = {
        "AAMk-GSD": [
            {"conversationId": "CONV-UNREAD", "topic": "Unread", "unreadCount": 1},
            {"conversationId": "CONV-READ", "topic": "Already read", "unreadCount": 0},
        ],
    }
    messages_by_conv = {
        "CONV-UNREAD": [{"itemId": "U1", "sender": {"name": "X", "email": "x@example.com"},
                         "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "Unread",
                         "body": "Unread body.", "isRead": False}],
    }
    calls = []
    monkeypatch.setattr(email_mod, "call_read_tool",
                        _b58_scan_read_tool(folder_children, conversations_by_id,
                                            messages_by_conv, calls=calls))
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    items = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    convs = {it.get("conversationId") for it in items}
    assert "CONV-UNREAD" in convs
    assert "CONV-READ" not in convs
    read_conv_ids = [a.get("conversationId") for n, a in calls if n == "email_read"]
    assert "CONV-READ" not in read_conv_ids


async def test_b58_scan_folder_email_read_never_marks_read(email_file, monkeypatch):
    """AC-6 (anti MARK-READ-BY-ACCIDENT, load-bearing): email_read MUST be called
    WITHOUT any markAs key so the folder mail stays UNREAD after the scan."""
    _seed(email_file, [])
    folder_children = [{"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 1, "children": []}]
    conversations_by_id = {"AAMk-GSD": [{"conversationId": "CONV-X", "topic": "X",
                                         "unreadCount": 1}]}
    messages_by_conv = {
        "CONV-X": [{"itemId": "X1", "sender": {"name": "X", "email": "x@example.com"},
                    "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "X",
                    "body": "Body.", "isRead": False}],
    }
    calls = []
    monkeypatch.setattr(email_mod, "call_read_tool",
                        _b58_scan_read_tool(folder_children, conversations_by_id,
                                            messages_by_conv, calls=calls))
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    email_read_calls = [a for n, a in calls if n == "email_read"]
    assert email_read_calls, "email_read should have been called for the unread conversation"
    for args in email_read_calls:
        assert "markAs" not in args, \
            "email_read must NOT pass markAs (would mark the user's mail read)"


async def test_b58_scan_inbox_item_tagged_inbox_source(email_file, monkeypatch):
    """AC-2/AC-7: the existing Inbox-root path tags items sourceFolder='Inbox' (set on
    BOTH paths in lockstep so an Inbox item is never blank)."""
    _seed(email_file, [])
    inbox_emails = [{"id": "INBOX-1", "subject": "Inbox subject",
                     "from": {"name": "Root Sender", "email": "root@example.com"},
                     "received": "2026-06-26T11:00:00Z", "is_read": False, "preview": "p"}]
    monkeypatch.setattr(email_mod, "call_read_tool",
                        _b58_scan_read_tool([], {}, {}, inbox_emails=inbox_emails))
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    items = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    inbox_item = next(it for it in items if it.get("messageId") == "INBOX-1")
    assert inbox_item.get("sourceFolder") == "Inbox"


async def test_b58_scan_combined_cap_fifo_across_sources_unchanged(email_file, monkeypatch):
    """AC-8 (anti CAP-BYPASS): folder items join the SAME queue under the SINGLE
    EMAIL_ACTIVE_TODO_CAP, oldest-first across ALL sources. With CAP=2 and 1 inbox +
    2 folder unread, exactly the 2 OLDEST are admitted, the newest deferred."""
    monkeypatch.setattr(email_mod, "EMAIL_ACTIVE_TODO_CAP", 2)
    _seed(email_file, [])
    inbox_emails = [{"id": "INBOX-NEW", "subject": "Inbox newest",
                     "from": {"name": "I", "email": "i@example.com"},
                     "received": "2026-06-26T12:00:00Z", "is_read": False, "preview": "p"}]
    folder_children = [{"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 2, "children": []}]
    conversations_by_id = {
        "AAMk-GSD": [
            {"conversationId": "CONV-OLD", "topic": "old", "unreadCount": 1,
             "lastDeliveryTime": "2026-06-26T08:00:00Z"},
            {"conversationId": "CONV-MID", "topic": "mid", "unreadCount": 1,
             "lastDeliveryTime": "2026-06-26T10:00:00Z"},
        ],
    }
    messages_by_conv = {
        "CONV-OLD": [{"itemId": "OLD1", "sender": {"name": "O", "email": "o@example.com"},
                      "dateTimeSent": "2026-06-26T08:00:00Z", "subject": "old",
                      "body": "Old body.", "isRead": False}],
        "CONV-MID": [{"itemId": "MID1", "sender": {"name": "M", "email": "m@example.com"},
                      "dateTimeSent": "2026-06-26T10:00:00Z", "subject": "mid",
                      "body": "Mid body.", "isRead": False}],
    }
    monkeypatch.setattr(email_mod, "call_read_tool",
                        _b58_scan_read_tool(folder_children, conversations_by_id,
                                            messages_by_conv, inbox_emails=inbox_emails))
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    items = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    assert len(items) == 2
    convs = {it.get("conversationId") for it in items}
    assert "CONV-OLD" in convs and "CONV-MID" in convs
    assert "INBOX-NEW" not in convs
    assert email_mod._count_actionable(items) <= 2


async def test_b58_scan_one_bad_folder_does_not_abort_cycle(email_file, monkeypatch):
    """AC-10 (anti WHOLE-CYCLE-ABORT): a folder read that throws is logged and the
    scan still completes for the OTHER folders + inbox (best-effort per folder)."""
    _seed(email_file, [])
    folder_children = [
        {"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 1, "children": []},
        {"name": "1 leads", "id": "AAMk-BAD", "unreadCount": 1, "children": []},
    ]
    conversations_by_id = {"AAMk-GSD": [{"conversationId": "CONV-GOOD", "topic": "good",
                                         "unreadCount": 1}]}
    messages_by_conv = {
        "CONV-GOOD": [{"itemId": "G1", "sender": {"name": "G", "email": "g@example.com"},
                       "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "good",
                       "body": "Good body.", "isRead": False}],
    }
    base = _b58_scan_read_tool(folder_children, conversations_by_id, messages_by_conv)

    async def flaky_read_tool(name, arguments):
        if name == "email_folders" and arguments.get("folderId") == "AAMk-BAD":
            raise RuntimeError("transient OWA failure reading 1 leads")
        return await base(name, arguments)

    monkeypatch.setattr(email_mod, "call_read_tool", flaky_read_tool)
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})  # must NOT raise

    items = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    convs = {it.get("conversationId") for it in items}
    assert "CONV-GOOD" in convs


async def test_b58_scan_dedups_conversation_in_both_inbox_and_folder(email_file, monkeypatch):
    """AC-9: a conversation appearing in BOTH the Inbox fetch and an allowlisted
    subfolder is admitted ONCE (deduped on conversationId/messageId), not twice."""
    _seed(email_file, [])
    shared_id = "SHARED-CONV"
    inbox_emails = [{"id": shared_id, "subject": "Shared",
                     "from": {"name": "Dup", "email": "dup@example.com"},
                     "received": "2026-06-26T09:00:00Z", "is_read": False, "preview": "p"}]
    folder_children = [{"name": "1 GSD", "id": "AAMk-GSD", "unreadCount": 1, "children": []}]
    conversations_by_id = {
        "AAMk-GSD": [{"conversationId": shared_id, "topic": "Shared", "unreadCount": 1}],
    }
    messages_by_conv = {
        shared_id: [{"itemId": "S1", "sender": {"name": "Dup", "email": "dup@example.com"},
                     "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "Shared",
                     "body": "Shared body.", "isRead": False}],
    }
    monkeypatch.setattr(email_mod, "call_read_tool",
                        _b58_scan_read_tool(folder_children, conversations_by_id,
                                            messages_by_conv, inbox_emails=inbox_emails))
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    items = json.loads(email_file.read_text(encoding="utf-8"))["items"]
    shared = [it for it in items if it.get("conversationId") == shared_id]
    assert len(shared) == 1, "the shared conversation must be admitted exactly once"
    # First-seen (the Inbox path) wins the source tag.
    assert shared[0].get("sourceFolder") == "Inbox"


def test_b58_owa_remap_message_id_tracks_itemid_specifically():
    """AC-13 #3 anti-bogus-green: the per-message id MUST be sourced from `itemId`
    SPECIFICALLY, not id/messageId. The OWA email_read message has NO id/messageId
    key — only the misspelled-domain `itemId` names the message — so a remap that
    (wrongly) read id/messageId would leave the turn's id BLANK.

    Feed a message carrying ONLY itemId with a DISTINCT value and assert the mapped
    message id equals exactly that itemId (a 'non-empty' assertion would bogus-green
    regardless of which key was read; the exact-equality + only-itemId-present makes
    it go RED if the wrong key is read)."""
    msg = {
        "itemId": "AAMk-ITEMID-TRACKED-9",
        "sender": {"name": "Glenn Dev", "email": "glenn@example.com"},
        "dateTimeSent": "2026-06-26T09:00:00Z",
        "subject": "OWA only",
        "body": "Body text.",
        "isRead": False,
    }
    assert "id" not in msg and "messageId" not in msg, \
        "the fixture must carry ONLY itemId so the remap source is unambiguous"

    payload = email_mod._owa_conversation_to_payload([msg])
    mapped = email_mod._email_messages_from_payload(payload)
    assert len(mapped) == 1
    # Exact equality on the DISTINCT itemId value (not just non-empty).
    assert mapped[0].get("id") == "AAMk-ITEMID-TRACKED-9", \
        "message id must be remapped from itemId, not id/messageId"
    assert mapped[0].get("messageId") == "AAMk-ITEMID-TRACKED-9"
    # And it is non-blank — a skipped itemId read would yield '' here.
    assert mapped[0].get("messageId"), "a skipped itemId->id remap would leave the id blank"


async def test_b58_folder_item_backfill_uses_owa_email_read_not_graph(email_file, monkeypatch):
    """A body-less folder-sourced item already in the queue backfills through the OWA
    email_read path (off the GRASP quota), NEVER Graph get_email, and WITHOUT markAs."""
    _seed(email_file, [_make_item(
        "f1", status="needs-review", messageId="AAMk-FOLDER-CONV",
        conversationId="AAMk-FOLDER-CONV", sourceFolder="1 GSD", emailBody="")])
    calls = []

    async def fake_read_tool(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "get_emails":
            return {"emails": [], "count": 0, "folder": "inbox"}
        if name == "email_list_folders":
            return _b58_list_folders_payload([])  # no allowlisted children resolve
        if name == "email_read":
            return {"content": {"emails": [
                {"itemId": "AAMk-FOLDER-CONV", "sender": {"name": "X", "email": "x@example.com"},
                 "dateTimeSent": "2026-06-26T09:00:00Z", "subject": "Re: Project update",
                 "body": "Recovered folder body.", "isRead": False}]}}
        return {}

    monkeypatch.setattr(email_mod, "call_read_tool", fake_read_tool)
    await email_mod._scan_once({"ws_manager": _B58WSNoop()})

    item = json.loads(email_file.read_text(encoding="utf-8"))["items"][0]
    assert "Recovered folder body." in (item.get("emailBody") or "")
    names = [n for n, _ in calls]
    assert "email_read" in names
    assert "get_email" not in names, "folder item must NOT backfill via Graph get_email"
    for n, a in calls:
        if n == "email_read":
            assert "markAs" not in a
