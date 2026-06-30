// Plain-Node branch-table test for the extracted EmailPage decision logic.
// Run: `node frontend/src/pages/emailDetail.test.mjs`
// No npm dependency, no jsdom/vitest — this is the interaction-path verification
// for the bits of EmailPage's new behavior that are pure functions of their
// inputs (thread-history normalization, mute-key labels, the Polish source/guard
// decision, the auto-size height, the muted-key ordering). The component imports
// these EXACT functions, so a regression here is caught without a browser. Render
// wiring (the actual cards/buttons) stays browser-unverified — these tests cover
// the load-bearing data decisions behind them.
import assert from 'node:assert/strict';
import {
  threadTurns,
  latestThreadView,
  threadSummaryParts,
  fromColumnLabel,
  sourceFolderLabel,
  mutedLabel,
  sortedMutedKeys,
  polishSource,
  autoSizeHeight,
  displayTimestamp,
  deleteOutcome,
  autoFormatBody,
  replyAllRecipients,
  seedRecipients,
  threadCardStates,
  previewOf,
  setMyEmail,
  getMyEmail,
  approveOutcome,
  dismissOutcome,
} from './emailDetail.js';

// Set the test email so replyAllRecipients/seedRecipients tests have a known
// value. This mirrors what the page component does at runtime after /api/config.
setMyEmail('testuser@example.com');

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

// --- threadTurns: read item.threadHistory defensively ---------------------

test('threadTurns returns [] for a missing field (old item) — never crashes', () => {
  assert.deepEqual(threadTurns({}), []);
  assert.deepEqual(threadTurns({ threadHistory: null }), []);
  assert.deepEqual(threadTurns({ threadHistory: 'a string, not an array' }), []);
  assert.deepEqual(threadTurns(undefined), []);
});

test('threadTurns preserves N turns in order', () => {
  const hist = [
    { sender: 'Alice', timestamp: 1, body: 'first' },
    { sender: 'Bob', timestamp: 2, body: 'second' },
    { sender: 'Alice', timestamp: 3, body: 'third' },
  ];
  const out = threadTurns({ threadHistory: hist });
  assert.equal(out.length, 3);
  assert.deepEqual(out.map(t => t.body), ['first', 'second', 'third']);
});

test('threadTurns keeps a single turn as a single turn', () => {
  const out = threadTurns({ threadHistory: [{ sender: 'X', timestamp: 9, body: 'only' }] });
  assert.equal(out.length, 1);
  assert.equal(out[0].body, 'only');
});

// --- latestThreadView: latest-message-by-default vs full-timeline decision -
// Thread Context shows the NEWEST turn by default and a "Show full email (N
// messages)" expander. N is the REAL turn count (count), never item.messageCount
// (the wrong-N trap). With no structured history it falls back to the full
// body/snippet — and fallbackBody is ALWAYS a string, never the literal
// 'undefined'.

test('latestThreadView: N turns -> latest is the LAST (newest) turn, count === N', () => {
  const hist = [
    { sender: 'Alice', timestamp: 1, body: 'first' },
    { sender: 'Bob', timestamp: 2, body: 'second' },
    { sender: 'Alice', timestamp: 3, body: 'third (newest)' },
  ];
  const v = latestThreadView({ threadHistory: hist });
  assert.equal(v.count, 3);
  assert.equal(v.latest.body, 'third (newest)');
  assert.equal(v.latest.sender, 'Alice');
  assert.deepEqual(v.turns.map(t => t.body), ['first', 'second', 'third (newest)']);
  assert.equal(v.fallbackBody, '');
});

test('latestThreadView: a single turn -> that turn, count 1', () => {
  const v = latestThreadView({ threadHistory: [{ sender: 'X', timestamp: 9, body: 'only' }] });
  assert.equal(v.count, 1);
  assert.equal(v.latest.body, 'only');
  assert.equal(v.turns.length, 1);
  assert.equal(v.fallbackBody, '');
});

test('latestThreadView: N derives from the REAL turns, NOT item.messageCount', () => {
  const hist = [
    { sender: 'Alice', timestamp: 1, body: 'a' },
    { sender: 'Bob', timestamp: 2, body: 'b' },
  ];
  // messageCount lies (99) — count must follow the actual cards we'd render (2).
  const v = latestThreadView({ threadHistory: hist, messageCount: 99 });
  assert.equal(v.count, 2);
  assert.equal(v.turns.length, 2);
});

test('latestThreadView: missing threadHistory -> latest null, count 0, fallbackBody = emailBody', () => {
  const v = latestThreadView({ emailBody: 'full concatenated body', snippet: 'snip' });
  assert.equal(v.latest, null);
  assert.equal(v.count, 0);
  assert.deepEqual(v.turns, []);
  assert.equal(v.fallbackBody, 'full concatenated body');
});

test('latestThreadView: null / non-array threadHistory -> latest null, count 0', () => {
  const a = latestThreadView({ threadHistory: null, emailBody: 'body A' });
  assert.equal(a.latest, null);
  assert.equal(a.count, 0);
  assert.equal(a.fallbackBody, 'body A');

  const b = latestThreadView({ threadHistory: 'not an array', emailBody: 'body B' });
  assert.equal(b.latest, null);
  assert.equal(b.count, 0);
  assert.equal(b.fallbackBody, 'body B');
});

test('latestThreadView: empty history + no emailBody -> falls back to snippet', () => {
  const v = latestThreadView({ threadHistory: [], snippet: 'just a snippet' });
  assert.equal(v.latest, null);
  assert.equal(v.count, 0);
  assert.equal(v.fallbackBody, 'just a snippet');
});

test('latestThreadView: no history, no body, no snippet -> fallbackBody is "" (never "undefined")', () => {
  const v = latestThreadView({});
  assert.equal(v.latest, null);
  assert.equal(v.count, 0);
  assert.equal(v.fallbackBody, '');
  assert.notEqual(v.fallbackBody, 'undefined');

  const n = latestThreadView(undefined);
  assert.equal(n.latest, null);
  assert.equal(n.count, 0);
  assert.equal(n.fallbackBody, '');
});

test('latestThreadView: emailBody preferred over snippet when both present (no history)', () => {
  const v = latestThreadView({ emailBody: 'the body', snippet: 'the snippet' });
  assert.equal(v.fallbackBody, 'the body');
});

// --- threadSummaryParts: Thread Summary's two AI-derived sub-parts ----------
// The Thread Summary section shows a "Summary" paragraph (item.threadContext)
// and a "What they need from you" line (item.threadAsk). BOTH are AI-produced
// and may be missing/empty (older items, an FYI with no real ask, or a
// generation that didn't emit one). The decision of real-text-vs-placeholder
// is pure: trim, and report whether each part has content so the render shows a
// muted placeholder instead of the literal 'undefined' and never fabricates.

test('threadSummaryParts: both present -> trimmed text, hasSummary/hasAsk true', () => {
  const p = threadSummaryParts({
    threadContext: '  The team is planning the Q3 roadmap.  ',
    threadAsk: '  Do you approve the timeline?  ',
  });
  assert.equal(p.summary, 'The team is planning the Q3 roadmap.');
  assert.equal(p.ask, 'Do you approve the timeline?');
  assert.equal(p.hasSummary, true);
  assert.equal(p.hasAsk, true);
});

test('threadSummaryParts: missing ask (FYI / no real ask) -> hasAsk false, ask "" (never fabricated)', () => {
  const p = threadSummaryParts({ threadContext: 'Just an FYI newsletter.' });
  assert.equal(p.hasSummary, true);
  assert.equal(p.summary, 'Just an FYI newsletter.');
  assert.equal(p.hasAsk, false);
  assert.equal(p.ask, '');
});

test('threadSummaryParts: empty/whitespace ask -> hasAsk false (placeholder territory)', () => {
  const p = threadSummaryParts({ threadContext: 'x', threadAsk: '   ' });
  assert.equal(p.hasAsk, false);
  assert.equal(p.ask, '');
});

test('threadSummaryParts: older item with neither field -> both false, both "" (never "undefined")', () => {
  const p = threadSummaryParts({});
  assert.equal(p.summary, '');
  assert.equal(p.ask, '');
  assert.equal(p.hasSummary, false);
  assert.equal(p.hasAsk, false);
  assert.notEqual(p.summary, 'undefined');
  assert.notEqual(p.ask, 'undefined');
});

test('threadSummaryParts: null/undefined item -> safe empty parts', () => {
  for (const bad of [null, undefined]) {
    const p = threadSummaryParts(bad);
    assert.equal(p.summary, '');
    assert.equal(p.ask, '');
    assert.equal(p.hasSummary, false);
    assert.equal(p.hasAsk, false);
  }
});

test('threadSummaryParts: non-string fields are coerced safely, never "undefined"/"null"', () => {
  const p = threadSummaryParts({ threadContext: 123, threadAsk: null });
  assert.equal(p.summary, '123');
  assert.equal(p.hasSummary, true);
  assert.equal(p.ask, '');
  assert.equal(p.hasAsk, false);
});

// --- fromColumnLabel: the email-list "From" cell text ---------------------
// The list From cell shows the LATEST reply's sender (backfilled server-side
// onto item.sender) plus a "+N" participant count where N = the number of
// ADDITIONAL unique senders. So a 2-sender thread reads "+1", a 3-sender "+2".
// A single-sender OR a missing/empty senderList (older items predate the field)
// renders the base sender with NO suffix — never "+0", never a bare "+". The
// existing `sender || 'unknown'` fallback is preserved. Pure — no DOM.

test('fromColumnLabel: single-sender senderList -> base name only, no "+N"', () => {
  assert.equal(fromColumnLabel({ sender: 'Yibo', senderList: ['Yibo'] }), 'Yibo');
});

test('fromColumnLabel: 2 unique senders -> "+1" (additional uniques, not total)', () => {
  assert.equal(
    fromColumnLabel({ sender: 'Bingfeng', senderList: ['Yibo', 'Bingfeng'] }),
    'Bingfeng +1',
  );
});

test('fromColumnLabel: 3 unique senders -> "+2"', () => {
  assert.equal(
    fromColumnLabel({ sender: 'Bingfeng', senderList: ['Han', 'Yibo', 'Bingfeng'] }),
    'Bingfeng +2',
  );
});

test('fromColumnLabel: missing senderList (old item) -> base sender, no suffix', () => {
  assert.equal(fromColumnLabel({ sender: 'Yibo' }), 'Yibo');
});

test('fromColumnLabel: empty senderList -> base sender, no suffix (never "+0"/"+")', () => {
  const out = fromColumnLabel({ sender: 'Yibo', senderList: [] });
  assert.equal(out, 'Yibo');
  assert.equal(out.includes('+'), false);
});

test('fromColumnLabel: non-array senderList is ignored -> base sender only', () => {
  assert.equal(fromColumnLabel({ sender: 'Yibo', senderList: 'not an array' }), 'Yibo');
});

test('fromColumnLabel: empty sender -> "unknown" fallback preserved', () => {
  assert.equal(fromColumnLabel({}), 'unknown');
  assert.equal(fromColumnLabel({ sender: '' }), 'unknown');
  assert.equal(fromColumnLabel(undefined), 'unknown');
});

test('fromColumnLabel: empty sender but multi senderList -> "unknown +N" still counts', () => {
  assert.equal(fromColumnLabel({ sender: '', senderList: ['A', 'B'] }), 'unknown +1');
});

// --- sourceFolderLabel: the per-row source-folder badge label (D-058 §5) --
// Every item carries a sourceFolder set by the scan worker: the folder display
// name ("1 GSD", "1 managers", ...) for folder-sourced items and "Inbox" for the
// root path. The badge renders it VERBATIM. The ONLY decision is the safe default
// for an OLDER persisted item that predates the field (or a blank value): default
// to "Inbox" so no row reads blank — but NEVER invent a folder name and NEVER
// render a raw AAMk... id (the backend always sets a real display name on both
// paths; the helper only guards the missing/blank case, it does not transform a
// present value).

test('sourceFolderLabel: a folder item renders its display name verbatim', () => {
  assert.equal(sourceFolderLabel({ sourceFolder: '1 GSD' }), '1 GSD');
  assert.equal(sourceFolderLabel({ sourceFolder: '1 managers' }), '1 managers');
  assert.equal(sourceFolderLabel({ sourceFolder: '2 black falcon' }), '2 black falcon');
});

test('sourceFolderLabel: an Inbox-root item renders "Inbox" verbatim', () => {
  assert.equal(sourceFolderLabel({ sourceFolder: 'Inbox' }), 'Inbox');
});

test('sourceFolderLabel: a missing/blank field defaults to "Inbox" (older item never reads blank)', () => {
  assert.equal(sourceFolderLabel({}), 'Inbox');
  assert.equal(sourceFolderLabel(undefined), 'Inbox');
  assert.equal(sourceFolderLabel({ sourceFolder: '' }), 'Inbox');
  assert.equal(sourceFolderLabel({ sourceFolder: '   ' }), 'Inbox');
  assert.equal(sourceFolderLabel({ sourceFolder: null }), 'Inbox');
});

test('sourceFolderLabel: a non-string value defaults to "Inbox" (never renders [object Object])', () => {
  assert.equal(sourceFolderLabel({ sourceFolder: 42 }), 'Inbox');
  assert.equal(sourceFolderLabel({ sourceFolder: { name: '1 GSD' } }), 'Inbox');
});

test('sourceFolderLabel: a present value is trimmed but otherwise verbatim (no fabrication)', () => {
  assert.equal(sourceFolderLabel({ sourceFolder: '  1 CRIS OOR  ' }), '1 CRIS OOR');
});

// --- mutedLabel: resolve a friendly label from a mute key -----------------

const items = [
  { id: 'i1', conversationId: 'conv-A', sender: 'Carol', subject: 'Budget review' },
  { id: 'i2', conversationId: 'conv-B', sender: 'Dan', subject: '' },
];

test('mutedLabel resolves the subject from a matching conversationId', () => {
  assert.equal(mutedLabel('conv-A', items), 'Budget review');
});

test('mutedLabel falls back to the sender when the subject is empty', () => {
  assert.equal(mutedLabel('conv-B', items), 'Dan');
});

test('mutedLabel falls back to the raw key when no item matches', () => {
  assert.equal(mutedLabel('conv-Z', items), 'conv-Z');
  assert.equal(mutedLabel('conv-Z', []), 'conv-Z');
});

// --- sortedMutedKeys: newest mute first -----------------------------------

test('sortedMutedKeys orders keys newest-muted first', () => {
  const muted = { old: 100, newer: 300, mid: 200 };
  assert.deepEqual(sortedMutedKeys(muted), ['newer', 'mid', 'old']);
});

test('sortedMutedKeys returns [] for a non-object', () => {
  assert.deepEqual(sortedMutedKeys(null), []);
  assert.deepEqual(sortedMutedKeys(undefined), []);
});

// --- polishSource: which text Polish operates on, and the empty guard ------

test('polishSource uses the live edit text when this row is being edited', () => {
  const r = polishSource('i1', 'i1', 'my unsaved edit', { id: 'i1', draft: 'stored' });
  assert.equal(r.ok, true);
  assert.equal(r.text, 'my unsaved edit');
});

test('polishSource uses the stored draft when this row is NOT being edited', () => {
  const r = polishSource('i1', null, 'leftover from elsewhere', { id: 'i1', draft: 'stored draft' });
  assert.equal(r.ok, true);
  assert.equal(r.text, 'stored draft');
});

test('polishSource refuses (ok:false) when there is nothing to polish', () => {
  const r = polishSource('i1', null, '', { id: 'i1', draft: '   ' });
  assert.equal(r.ok, false);
  assert.equal(r.text, '');
});

// --- autoSizeHeight: shrink-then-grow to content (capped) -----------------

test('autoSizeHeight grows to scrollHeight + 2 below the cap', () => {
  assert.equal(autoSizeHeight(100, 320), 102);
});

test('autoSizeHeight is capped for a very long draft', () => {
  assert.equal(autoSizeHeight(5000, 320), 320);
});

// --- displayTimestamp: the per-turn timestamp slot text (D-053 clause 7) ----
// renderTurnCard shows a per-turn timestamp. A turn from the Graph message has
// an epoch/ISO timestamp that resolves to a relative-time string ("3h ago").
// But a turn PARSED from a quoted-reply header carries the human date string
// verbatim ("Wednesday, June 24, 2026 at 10:23") which `new Date(ts)` can't
// parse — we MUST show that string as-is, NEVER the broken-looking "--"
// placeholder (AC-8). An empty/missing timestamp yields "" so no slot renders
// at all. Pure — no DOM, no fetch.

test('displayTimestamp: null/undefined/empty -> "" (no slot renders)', () => {
  assert.equal(displayTimestamp(null), '');
  assert.equal(displayTimestamp(undefined), '');
  assert.equal(displayTimestamp(''), '');
  assert.equal(displayTimestamp('   '), '');
});

test('displayTimestamp: a parseable epoch ms -> a relative-ish string (not "--", not the input)', () => {
  const fiveMinAgo = Date.now() - 5 * 60000;
  const out = displayTimestamp(fiveMinAgo);
  assert.notEqual(out, '');
  assert.notEqual(out, '--');
  assert.notEqual(out, String(fiveMinAgo));
  assert.equal(out, '5m ago');
});

test('displayTimestamp: a parseable ISO string -> a relative-ish string (not "--", not verbatim)', () => {
  const iso = new Date(Date.now() - 3 * 3600000).toISOString();
  const out = displayTimestamp(iso);
  assert.notEqual(out, '');
  assert.notEqual(out, '--');
  assert.notEqual(out, iso); // not echoed back verbatim
  assert.equal(out, '3h ago');
});

test('displayTimestamp: matches relativeTime math across units (just now / m / h / d)', () => {
  assert.equal(displayTimestamp(Date.now() - 10000), 'just now'); // <1 min
  assert.equal(displayTimestamp(Date.now() - 30 * 60000), '30m ago');
  assert.equal(displayTimestamp(Date.now() - 5 * 3600000), '5h ago');
  assert.equal(displayTimestamp(Date.now() - 2 * 86400000), '2d ago');
  assert.equal(displayTimestamp(Date.now() + 60000), 'just now'); // future -> just now
});

test('displayTimestamp: an UNPARSEABLE human date string -> returned VERBATIM, never "--" (AC-8)', () => {
  const human = 'Wednesday, June 24, 2026 at 10:23';
  const out = displayTimestamp(human);
  assert.equal(out, human);
  assert.notEqual(out, '--');
});

test('displayTimestamp: an unparseable string with surrounding whitespace -> trimmed verbatim', () => {
  assert.equal(displayTimestamp('  Wednesday, June 24, 2026 at 10:23  '), 'Wednesday, June 24, 2026 at 10:23');
});

test('displayTimestamp: a free-text date-like string Date cannot parse -> verbatim, not "--"', () => {
  const out = displayTimestamp('sometime next week');
  assert.equal(out, 'sometime next week');
  assert.notEqual(out, '--');
});

// renderTurnCard's timestamp slot is `displayTimestamp(turn.timestamp)`, NOT
// relativeTime — a quoted-header turn carries the verbatim human date, which
// relativeTime would render as the broken "--". This pins the slot's exact
// two-branch contract straight off the turn shape so a regression to
// relativeTime (or back to "--") goes RED here, even though the JSX itself is
// browser-unverified (no jsdom/vitest).
test('displayTimestamp: drives the turn-card slot for a MAWS quoted-header turn (verbatim, renders)', () => {
  const quotedTurn = {
    sender: 'Wang, Yibo',
    timestamp: 'Wednesday, June 24, 2026 at 10:23',
    body: 'GL L7 SDMs, ...',
    recipients: 'Agarwal, Ankit; Lakshmanan, Geetika',
  };
  const slot = displayTimestamp(quotedTurn.timestamp);
  // The page renders the slot only when this is truthy -> a real date shows,
  // never "--" and never empty (so the date is NOT silently dropped).
  assert.equal(slot, 'Wednesday, June 24, 2026 at 10:23');
  assert.notEqual(slot, '--');
  assert.ok(slot, 'a real quoted-header date must produce a non-empty slot');
});

test('displayTimestamp: an empty turn timestamp -> "" so the slot renders nothing (no "--")', () => {
  const freshTurn = { sender: 'Han, Bingfeng', timestamp: '', body: 'Thanks Yibo.' };
  const slot = displayTimestamp(freshTurn.timestamp);
  // Falsy -> the page's `{turn.timestamp && ...}`-style guard skips the slot.
  assert.equal(slot, '');
  assert.notEqual(slot, '--');
});

// --- deleteOutcome: honest delete confirmation (Fix task 6 frontend half) ---
// The delete POST is fire-and-forget at the HTTP envelope (the backend can
// return 200/ok:true while the Outlook MOVE later 429s on the GRASP quota), so
// the row must flip to "deleted" ONLY when the response BODY confirms
// `deleted === true`. A non-2xx, a 409 conflict, OR a 2xx-with-`deleted:false`
// (or no `deleted` flag) all leave the row VISIBLE in its prior state with an
// inline error — never the SWALLOWED-200 / FRONTEND-ONLY-409 lie. deleteOutcome
// turns (httpStatus, body) into {outcome, etag, reason}:
//   outcome 'deleted'  -> collapse/move the row to Deleted; adopt body.etag
//   outcome 'conflict' -> the queue moved elsewhere (409); re-read + banner
//   outcome 'failed'   -> keep the row, show `reason` inline (e.g. quota)
// Pure — no DOM, no fetch.

test('deleteOutcome: 2xx with deleted:true -> "deleted", adopts the etag', () => {
  const r = deleteOutcome(200, { ok: true, deleted: true, id: 'i1', etag: 'e9' });
  assert.equal(r.outcome, 'deleted');
  assert.equal(r.etag, 'e9');
});

test('deleteOutcome: 409 conflict -> "conflict" (re-read, never treated as deleted)', () => {
  const r = deleteOutcome(409, { error: 'conflict', etag: 'e2', current: {} });
  assert.equal(r.outcome, 'conflict');
  assert.notEqual(r.outcome, 'deleted');
});

test('deleteOutcome: SWALLOWED-200 trap — 2xx ok:true but deleted:false -> "failed", row stays', () => {
  // The HTTP layer "handled" it (200, ok:true) but the Outlook move 429d on the
  // GRASP quota: deleted is false. This MUST be a failure, not a clean delete.
  const r = deleteOutcome(200, {
    ok: true, deleted: false, id: 'i1',
    error: 'Outlook rate-limited (GRASP quota -> 429)', etag: 'e3',
  });
  assert.equal(r.outcome, 'failed');
  assert.notEqual(r.outcome, 'deleted');
  assert.ok(r.reason.includes('GRASP quota'));
  // still adopt the etag so a follow-up write isn't stale
  assert.equal(r.etag, 'e3');
});

test('deleteOutcome: 2xx with NO deleted flag (old backend) -> "failed", not a silent delete', () => {
  // Anti-FRONTEND-ONLY-409: keying on res.ok alone would treat this as deleted.
  // Absence of an explicit deleted:true is NOT a confirmation.
  const r = deleteOutcome(200, { ok: true, id: 'i1', etag: 'e4' });
  assert.equal(r.outcome, 'failed');
  assert.notEqual(r.outcome, 'deleted');
});

test('deleteOutcome: non-2xx (500) -> "failed" with a readable reason', () => {
  const r = deleteOutcome(500, { error: 'boom' });
  assert.equal(r.outcome, 'failed');
  assert.ok(r.reason && typeof r.reason === 'string');
});

test('deleteOutcome: failure reason prefers the body error, falls back to a default string', () => {
  const withErr = deleteOutcome(200, { ok: true, deleted: false, error: 'quota_exceeded -> 429' });
  assert.equal(withErr.reason, 'quota_exceeded -> 429');
  const noErr = deleteOutcome(200, { ok: true, deleted: false });
  assert.ok(noErr.reason && noErr.reason.length > 0);
  assert.notEqual(noErr.reason, 'undefined');
});

test('deleteOutcome: a null/garbage body never crashes and is treated as failure', () => {
  assert.equal(deleteOutcome(200, null).outcome, 'failed');
  assert.equal(deleteOutcome(502, undefined).outcome, 'failed');
});

// --- latestThreadView: a SINGLE-turn item exposes the FULL emailBody --------
// FEATURE #1: for a single-turn item the turn card IS the whole email, so the
// card body source must be the FULL item.emailBody (32k cap), NOT the
// 4096-capped threadHistory[0].body that cut the CRIS Flash mid-sentence (the
// SINGLE-MESSAGE-STILL-CAPPED trap). latestThreadView returns the full body on
// the single card's `body`, falling back to the turn body then snippet, while
// multi-turn threads keep one card per real turn (who-said-what preserved).

test('latestThreadView: single turn -> latest.body is the FULL item.emailBody, not the capped turn body', () => {
  const cappedTurnBody = 'A'.repeat(4096); // the silently-truncated threadHistory[0].body
  const fullBody = 'A'.repeat(4096) + ' ...and the rest down to the Weblab Exakt wiki line.';
  const v = latestThreadView({
    threadHistory: [{ sender: 'CRIS', timestamp: 't', body: cappedTurnBody }],
    emailBody: fullBody,
  });
  assert.equal(v.count, 1);
  assert.equal(v.latest.body, fullBody);
  assert.equal(v.latest.sender, 'CRIS'); // who-said-what preserved on the card
  assert.ok(v.latest.body.length > 4096, 'the single card must serve the FULL body, never the 4096 cut');
});

test('latestThreadView: single turn with no emailBody falls back to the turn body then snippet', () => {
  const a = latestThreadView({ threadHistory: [{ sender: 'X', timestamp: 1, body: 'turn body only' }] });
  assert.equal(a.latest.body, 'turn body only');
  const b = latestThreadView({ threadHistory: [{ sender: 'X', timestamp: 1, body: '' }], snippet: 'just a snip' });
  assert.equal(b.latest.body, 'just a snip');
});

test('latestThreadView: MULTI-turn item keeps each per-turn body (does NOT swap in the full body)', () => {
  const hist = [
    { sender: 'Alice', timestamp: 1, body: 'first turn' },
    { sender: 'Bob', timestamp: 2, body: 'second turn (newest)' },
  ];
  const v = latestThreadView({ threadHistory: hist, emailBody: 'WHOLE concatenated thread' });
  assert.equal(v.count, 2);
  // The newest card shows the real per-turn body, NOT the full concatenation.
  assert.equal(v.latest.body, 'second turn (newest)');
  assert.deepEqual(v.turns.map(t => t.body), ['first turn', 'second turn (newest)']);
});

// --- autoFormatBody: conservative, idempotent, content-preserving formatter --
// FEATURE #1: flat newsletter prose becomes scannable WITHOUT an LLM. Promote a
// bare line to a markdown subheading ONLY when it is SHORT and (ends in '?' OR
// is a Title-Case header-like line) AND is FOLLOWED BY A BLANK LINE. Convert a
// contiguous run of dash/number/bullet-like lines into a real markdown list.
// PRESERVE paragraph breaks, real lists, GFM tables, links. Guarantees: every
// word of the original present in the same order after one pass; a normal
// paragraph untouched; f(f(x)) === f(x).

const words = (s) => s.split(/\s+/).filter(Boolean);

test('autoFormatBody: a normal paragraph is left untouched (no over-eager headings)', () => {
  const para = 'We met yesterday to discuss the roadmap and agreed to ship in Q3. '
    + 'The team will follow up with the detailed plan next week.';
  assert.equal(autoFormatBody(para), para);
});

test('autoFormatBody: a normal multi-paragraph body keeps its blank-line breaks (no reflow/collapse)', () => {
  const body = 'First paragraph of normal prose that is fairly long and just keeps going.\n\n'
    + 'Second paragraph, also normal prose, nothing to promote here at all.';
  const out = autoFormatBody(body);
  assert.ok(out.includes('\n\n'), 'paragraph break must be preserved');
  assert.deepEqual(words(out), words(body), 'no words added/dropped/reordered');
});

test('autoFormatBody: a long line ending in "?" is NOT promoted (heading bar is SHORT + blank-after)', () => {
  const body = 'This is a genuinely long sentence that happens to end with a question mark '
    + 'because the author was being rhetorical about the whole situation here?\n\nSome follow-up text.';
  const out = autoFormatBody(body);
  assert.ok(!out.includes('### This is a genuinely long sentence'), 'a long question line is prose, not a heading');
});

test('autoFormatBody: a SHORT question line followed by a blank line becomes a subheading', () => {
  const body = 'How do I request access?\n\nFile a ticket with the team and they will grant it within a day.';
  const out = autoFormatBody(body);
  assert.ok(/^###?\s+How do I request access\?/m.test(out), 'short question + blank-after -> heading');
  // content preserved (heading markers are non-word, words intact)
  assert.deepEqual(words(out.replace(/#/g, '')), words(body));
});

test('autoFormatBody: a question line NOT followed by a blank line stays prose', () => {
  const body = 'How do I request access?\nFile a ticket and they will grant it.';
  const out = autoFormatBody(body);
  assert.ok(!/^#+\s+How do I request access\?/m.test(out), 'no blank-after -> not a heading');
});

test('autoFormatBody: a short Title-Case header-like line followed by a blank line becomes a subheading', () => {
  const body = 'Weekly Highlights\n\nWe shipped three features and fixed a dozen bugs this sprint.';
  const out = autoFormatBody(body);
  assert.ok(/^###?\s+Weekly Highlights$/m.test(out), 'short Title-Case + blank-after -> heading');
});

test('autoFormatBody: a contiguous run of dash lines becomes a real markdown list', () => {
  const body = 'Action items:\n\n- buy milk\n- walk the dog\n- file the report\n\nThanks!';
  const out = autoFormatBody(body);
  // already markdown-dash lines stay dash lines (idempotent, real list preserved)
  assert.ok(out.includes('- buy milk'));
  assert.deepEqual(words(out), words(body));
});

test('autoFormatBody: bullet-glyph / numbered lines normalize to markdown list markers, content preserved', () => {
  const body = 'Steps:\n\n1. open the door\n2. walk inside\n3. close the door\n\nDone.';
  const out = autoFormatBody(body);
  assert.deepEqual(words(out), words(body), 'numbered list content preserved');
});

test('autoFormatBody: a GFM table is left intact', () => {
  const table = '| Name | Role |\n| --- | --- |\n| Alice | SDE |\n| Bob | PM |';
  const body = 'Roster:\n\n' + table;
  const out = autoFormatBody(body);
  assert.ok(out.includes('| Name | Role |'));
  assert.ok(out.includes('| --- | --- |'));
  assert.ok(out.includes('| Alice | SDE |'));
});

test('autoFormatBody: a markdown link is left clickable (not mangled)', () => {
  const body = 'See [the wiki](https://wiki.example.com/page) for details.';
  assert.equal(autoFormatBody(body), body);
});

test('autoFormatBody: IDEMPOTENT — f(f(x)) === f(x) on a mixed body', () => {
  const body = 'Weekly Update\n\n'
    + 'We made good progress this week on the migration.\n\n'
    + 'How do I get involved?\n\n'
    + 'Ping the team channel and pick up a task.\n\n'
    + '- review the doc\n- run the tests\n- ship it\n\n'
    + 'Thanks everyone.';
  const once = autoFormatBody(body);
  const twice = autoFormatBody(once);
  assert.equal(twice, once, 'second pass must be a no-op');
});

test('autoFormatBody: NEVER drops content — every original word survives one pass (in order)', () => {
  const body = 'CRIS Weekly Flash\n\n'
    + 'This week the team launched the new pipeline.\n\n'
    + 'What changed?\n\n'
    + 'The build is now 30% faster and uses less memory.\n\n'
    + '- faster builds\n- less memory\n- happier engineers\n\n'
    + 'Weblab Exakt wiki';
  const out = autoFormatBody(body);
  assert.deepEqual(words(out.replace(/[#]/g, '')).filter(Boolean), words(body));
  assert.ok(out.includes('Weblab Exakt wiki'), 'the final line is never cut');
});

test('autoFormatBody: empty / non-string input -> "" (never crashes, never "undefined")', () => {
  assert.equal(autoFormatBody(''), '');
  assert.equal(autoFormatBody(null), '');
  assert.equal(autoFormatBody(undefined), '');
  assert.equal(autoFormatBody(42), '');
});

test('autoFormatBody: drops empty-emphasis artifact lines left by stripped inline images', () => {
  // HTML newsletters (e.g. the "new sql genAI editor" mail) convert to markdown
  // with leftover emphasis markers where inline gifs were stripped: "****",
  // "********", and bold wrapping only a non-breaking space ("** **"). These
  // render as stray <hr>/empty-bold noise. They carry NO words, so dropping the
  // whole artifact-only line preserves all real content.
  const body = 'Real intro paragraph.\n\n'
    + '****\n\n'
    + '** **\n\n'
    + '********\n\n'
    + 'Real closing paragraph.';
  const out = autoFormatBody(body);
  assert.ok(out.includes('Real intro paragraph.'), 'real content kept');
  assert.ok(out.includes('Real closing paragraph.'), 'real content kept');
  assert.ok(!/\*{3,}/.test(out), 'no run of 3+ asterisks (empty-emphasis artifact) survives');
  assert.ok(!out.includes('** **'), 'bold-around-nbsp artifact is gone');
});

test('autoFormatBody: NEVER strips emphasis that wraps real text', () => {
  // Conservative: only artifact lines with NO word characters are dropped. Real
  // bold/italic must be untouched.
  const body = '**Important:** the launch is Friday.\n\n'
    + 'See *the wiki* for details, and **bold inline** stays bold.';
  assert.equal(autoFormatBody(body), body);
});

test('autoFormatBody: a line that is bold-wrapping REAL words is kept (not treated as artifact)', () => {
  const body = '**Natural Language business questions converted to SQL:**';
  assert.equal(autoFormatBody(body), body);
});

// --- replyAllRecipients: reply-all default To/CC resolution -----------------
// FEATURE #3: when the draft editor opens, default To = sender + original To
// MINUS me; CC = original CC MINUS me, then ALWAYS add me; de-dupe BOTH
// case-insensitively by email. No captured to/cc => To=[sender], CC=[me] only
// (never fabricate). getMyEmail() returns the runtime value set via setMyEmail().

test('setMyEmail/getMyEmail: stores and returns the lowercased email', () => {
  setMyEmail('TestUser@Example.com');
  assert.equal(getMyEmail(), 'testuser@example.com');
});

test('replyAllRecipients: full reply-all — To=sender+origTo-me, CC=origCc-me+me, de-duped (all four clauses)', () => {
  const item = {
    senderEmail: 'alice@amazon.com',
    toRecipients: [
      { name: 'Me', email: 'testuser@example.com' }, // me must be stripped from To
      { name: 'Bob', email: 'bob@amazon.com' },
    ],
    ccRecipients: [
      { name: 'Carol', email: 'carol@amazon.com' },
      { name: 'Me', email: 'TESTUSER@example.com' }, // me (different case) stripped from CC
    ],
  };
  const r = replyAllRecipients(item);
  // To = sender + (original To minus me)
  assert.deepEqual(r.to, ['alice@amazon.com', 'bob@amazon.com']);
  // CC = (original CC minus me) + me (always)
  assert.deepEqual(r.cc, ['carol@amazon.com', 'testuser@example.com']);
});

test('replyAllRecipients: de-dupes case-insensitively (sender also appearing in To is not doubled)', () => {
  const item = {
    senderEmail: 'Alice@Amazon.com',
    toRecipients: [
      { name: 'Alice', email: 'alice@amazon.com' }, // dup of sender (different case)
      { name: 'Bob', email: 'bob@amazon.com' },
    ],
    ccRecipients: [],
  };
  const r = replyAllRecipients(item);
  // sender kept once, no case-variant duplicate
  assert.equal(r.to.filter(e => e.toLowerCase() === 'alice@amazon.com').length, 1);
  assert.ok(r.to.includes('bob@amazon.com'));
  // CC degrades to just me
  assert.deepEqual(r.cc, ['testuser@example.com']);
});

test('replyAllRecipients: always adds me to CC even when original CC is empty', () => {
  const item = { senderEmail: 'alice@amazon.com', toRecipients: [], ccRecipients: [] };
  const r = replyAllRecipients(item);
  assert.deepEqual(r.cc, ['testuser@example.com']);
});

test('replyAllRecipients: DEGRADE — no captured to/cc => To=[sender], CC=[me] (never fabricated)', () => {
  const item = { senderEmail: 'dana@amazon.com' }; // older item, no toRecipients/ccRecipients
  const r = replyAllRecipients(item);
  assert.deepEqual(r.to, ['dana@amazon.com']);
  assert.deepEqual(r.cc, ['testuser@example.com']);
});

test('replyAllRecipients: only me in original To/CC => To=[sender], CC=[me] (no self-reply)', () => {
  const item = {
    senderEmail: 'eve@amazon.com',
    toRecipients: [{ name: 'Me', email: 'testuser@example.com' }],
    ccRecipients: [{ name: 'Me', email: 'testuser@example.com' }],
  };
  const r = replyAllRecipients(item);
  assert.deepEqual(r.to, ['eve@amazon.com']); // me stripped from To, never reply to self
  assert.deepEqual(r.cc, ['testuser@example.com']); // me re-added (always-CC-me)
});

test('replyAllRecipients: tolerates string-email entries and skips blank/invalid ones (no fabrication)', () => {
  const item = {
    senderEmail: 'frank@amazon.com',
    toRecipients: ['grace@amazon.com', { email: '' }, { email: 'not-an-email' }, { name: 'no email' }],
    ccRecipients: ['heidi@amazon.com'],
  };
  const r = replyAllRecipients(item);
  assert.ok(r.to.includes('frank@amazon.com'));
  assert.ok(r.to.includes('grace@amazon.com'));
  assert.ok(!r.to.includes('')); // blank dropped
  assert.ok(!r.to.includes('not-an-email')); // invalid shape dropped, never fabricated
  assert.ok(r.cc.includes('heidi@amazon.com'));
  assert.ok(r.cc.includes('testuser@example.com'));
});

test('replyAllRecipients: empty/missing sender still degrades safely (To may be empty, CC=[me])', () => {
  const r = replyAllRecipients({});
  assert.deepEqual(r.cc, ['testuser@example.com']);
  assert.ok(Array.isArray(r.to));
});

// --- seedRecipients: which recipients the editor opens with (persist vs default)
// FEATURE #3 (AC-12). The editor seeds from the item's PERSISTED edit when the
// backend flipped recipientsEdited (save_draft stored plain email-string lists),
// so a page refresh keeps the user's exact To/CC. Otherwise it computes the
// reply-all default via replyAllRecipients (reading the captured {name,email}
// lists). This is the load-bearing decision that makes a refresh NOT reset the
// edit (the RECIPIENTS-NOT-PERSISTED trap).

test('seedRecipients: recipientsEdited -> uses the PERSISTED email lists verbatim (refresh keeps the edit)', () => {
  const item = {
    senderEmail: 'alice@amazon.com',
    recipientsEdited: true,
    toRecipients: ['bob@amazon.com'], // user removed the sender; must NOT be re-added
    ccRecipients: ['carol@amazon.com'], // user removed me; must NOT be re-added
  };
  const r = seedRecipients(item);
  assert.deepEqual(r.to, ['bob@amazon.com']);
  assert.deepEqual(r.cc, ['carol@amazon.com']);
});

test('seedRecipients: NOT edited -> computes the reply-all default from captured {name,email} lists', () => {
  const item = {
    senderEmail: 'alice@amazon.com',
    toRecipients: [
      { name: 'Me', email: 'testuser@example.com' },
      { name: 'Bob', email: 'bob@amazon.com' },
    ],
    ccRecipients: [{ name: 'Carol', email: 'carol@amazon.com' }],
  };
  const r = seedRecipients(item);
  assert.deepEqual(r.to, ['alice@amazon.com', 'bob@amazon.com']); // sender added, me stripped
  assert.deepEqual(r.cc, ['carol@amazon.com', 'testuser@example.com']); // me always added
});

test('seedRecipients: edited but To cleared -> To stays empty (the persisted edit is authoritative)', () => {
  const item = { senderEmail: 'alice@amazon.com', recipientsEdited: true, toRecipients: [], ccRecipients: [] };
  const r = seedRecipients(item);
  assert.deepEqual(r.to, []);
  assert.deepEqual(r.cc, []);
});

test('seedRecipients: edited persisted lists are still de-duped/validated (no fabrication)', () => {
  const item = {
    senderEmail: 'alice@amazon.com',
    recipientsEdited: true,
    toRecipients: ['bob@amazon.com', 'BOB@amazon.com', 'garbage'],
    ccRecipients: ['carol@amazon.com'],
  };
  const r = seedRecipients(item);
  assert.deepEqual(r.to, ['bob@amazon.com']); // de-duped CI, invalid dropped
  assert.deepEqual(r.cc, ['carol@amazon.com']);
});

// --- previewOf: a one-line, display-only truncation of a turn body ----------
// FEATURE #3. A collapsed older-message card shows a one-line preview so the
// reader can tell what it is without expanding. The preview is DISPLAY-ONLY: it
// is NEVER the stored body and NEVER replaces it (the full verbatim body is
// still rendered when the card expands — anti SUMMARY-MASQUERADING-AS-TIMELINE).
// It collapses internal whitespace/newlines to single spaces, trims, and caps at
// a small length with an ellipsis. Pure — no DOM, no fetch.

test('previewOf: a short single-line body is returned as-is (no ellipsis)', () => {
  assert.equal(previewOf('Thanks Yibo, will review.'), 'Thanks Yibo, will review.');
});

test('previewOf: collapses newlines and runs of whitespace into single spaces (one line)', () => {
  const body = 'First line.\n\nSecond   line\twith\ttabs.\nThird.';
  const out = previewOf(body);
  assert.ok(!out.includes('\n'), 'no newline survives — it is one line');
  assert.ok(!/\s{2,}/.test(out), 'no run of 2+ whitespace survives');
  assert.equal(out, 'First line. Second line with tabs. Third.');
});

test('previewOf: a long body is truncated with an ellipsis (display-only, never the full body)', () => {
  const body = 'word '.repeat(80).trim(); // far longer than the cap
  const out = previewOf(body);
  assert.ok(out.length <= 123, 'capped near 120 chars + ellipsis');
  assert.ok(out.endsWith('…'), 'ends with a single-char ellipsis');
  assert.notEqual(out, body, 'the preview is NOT the full stored body');
  assert.ok(body.startsWith(out.replace(/…$/, '').trimEnd()), 'preview is a prefix of the body (no fabrication)');
});

test('previewOf: empty / whitespace / non-string -> "" (never crashes, never "undefined")', () => {
  assert.equal(previewOf(''), '');
  assert.equal(previewOf('   \n\t  '), '');
  assert.equal(previewOf(null), '');
  assert.equal(previewOf(undefined), '');
  assert.equal(previewOf(42), '');
  assert.notEqual(previewOf(null), 'undefined');
});

// --- threadCardStates: the per-turn render-plan (Original/Latest/N-of-M) -----
// FEATURE #3 (intuitive multi-reply threading). The reader must instantly grok
// the conversation shape: which message is the ORIGINAL, which are NEWER replies,
// and the order. threadCardStates(item) reuses latestThreadView/threadTurns and
// returns ONE entry per turn — { index, label, isOriginal, isLatest,
// isExpandedByDefault, preview } — in oldest→newest order:
//   - label: 'Original' for the FIRST (oldest) turn, 'Latest' for the newest,
//            'Message N of M' for the middle turns (1-based N).
//   - the NEWEST turn is isExpandedByDefault:true (it's what the user acts on);
//     older turns are collapsed (isExpandedByDefault:false).
//   - preview: a one-line display-only truncation of that turn's body (the FULL
//     body is still rendered when expanded — the preview never replaces it).
// Every turn yields a DISTINCT entry (anti COLLAPSE-LOSES-WHO-SAID-WHAT): a
// 14-message thread => 14 entries, never merged/summarized. Edge cases: a
// 1-message thread => a single card (isOriginal && isLatest, expanded, no broken
// 'N of M'); an older item with no threadHistory => falls back through
// latestThreadView to ZERO cards, no crash, no literal 'undefined' (AC-16).
// Pure — no DOM, no fetch.

test('threadCardStates: a 3-message thread -> 3 distinct entries, oldest→newest', () => {
  const hist = [
    { sender: 'Alice', timestamp: 1, body: 'kick-off (original)' },
    { sender: 'Bob', timestamp: 2, body: 'a reply in the middle' },
    { sender: 'Alice', timestamp: 3, body: 'the freshest reply' },
  ];
  const states = threadCardStates({ threadHistory: hist });
  assert.equal(states.length, 3);
  assert.deepEqual(states.map(s => s.index), [0, 1, 2]);
  // labels: Original / Message 2 of 3 / Latest
  assert.deepEqual(states.map(s => s.label), ['Original', 'Message 2 of 3', 'Latest']);
});

test('threadCardStates: Original is the first/oldest, Latest is the newest, middle is "N of M"', () => {
  const hist = [
    { sender: 'A', timestamp: 1, body: 'one' },
    { sender: 'B', timestamp: 2, body: 'two' },
    { sender: 'C', timestamp: 3, body: 'three' },
    { sender: 'D', timestamp: 4, body: 'four' },
  ];
  const states = threadCardStates({ threadHistory: hist });
  assert.equal(states[0].isOriginal, true);
  assert.equal(states[0].isLatest, false);
  assert.equal(states[0].label, 'Original');

  assert.equal(states[3].isLatest, true);
  assert.equal(states[3].isOriginal, false);
  assert.equal(states[3].label, 'Latest');

  // the middle turns carry "Message N of M" (1-based N, M = total)
  assert.equal(states[1].label, 'Message 2 of 4');
  assert.equal(states[2].label, 'Message 3 of 4');
  assert.equal(states[1].isOriginal, false);
  assert.equal(states[1].isLatest, false);
});

test('threadCardStates: only the NEWEST turn is expanded by default; older turns are collapsed', () => {
  const hist = [
    { sender: 'A', timestamp: 1, body: 'oldest' },
    { sender: 'B', timestamp: 2, body: 'middle' },
    { sender: 'C', timestamp: 3, body: 'newest' },
  ];
  const states = threadCardStates({ threadHistory: hist });
  assert.deepEqual(states.map(s => s.isExpandedByDefault), [false, false, true]);
  // exactly one expanded-by-default card (the newest)
  assert.equal(states.filter(s => s.isExpandedByDefault).length, 1);
});

test('threadCardStates: each entry carries a one-line preview of ITS OWN turn body (display-only)', () => {
  const hist = [
    { sender: 'A', timestamp: 1, body: 'first turn\n\nwith a second paragraph' },
    { sender: 'B', timestamp: 2, body: 'second turn body' },
  ];
  const states = threadCardStates({ threadHistory: hist });
  assert.equal(states[0].preview, 'first turn with a second paragraph'); // newlines collapsed
  assert.equal(states[1].preview, 'second turn body');
  // the preview is DISPLAY-ONLY — the full per-turn body is still on the turn
  // (latestThreadView keeps each per-turn body verbatim), the preview never
  // replaces it.
  assert.ok(!states[0].preview.includes('\n'));
});

test('threadCardStates: a 1-message thread -> a SINGLE card, Original && Latest, expanded, no "N of M"', () => {
  const states = threadCardStates({ threadHistory: [{ sender: 'X', timestamp: 9, body: 'the only message' }] });
  assert.equal(states.length, 1);
  const only = states[0];
  assert.equal(only.index, 0);
  assert.equal(only.isOriginal, true);
  assert.equal(only.isLatest, true);
  assert.equal(only.isExpandedByDefault, true);
  // a single-message thread is labeled 'Latest' (it IS the newest) — never a
  // broken "Message 1 of 1".
  assert.equal(only.label, 'Latest');
  assert.notEqual(only.label, 'Message 1 of 1');
});

test('threadCardStates: a 14-message thread -> 14 DISTINCT entries (never merged/summarized)', () => {
  const hist = Array.from({ length: 14 }, (_, i) => ({
    sender: `S${i}`, timestamp: i + 1, body: `body number ${i + 1}`,
  }));
  const states = threadCardStates({ threadHistory: hist });
  // anti COLLAPSE-LOSES-WHO-SAID-WHAT / AC-11: N turns => N cards, never fewer.
  assert.equal(states.length, 14);
  assert.deepEqual(states.map(s => s.index), Array.from({ length: 14 }, (_, i) => i));
  // newest expanded + 13 collapsed
  assert.equal(states.filter(s => s.isExpandedByDefault).length, 1);
  assert.equal(states[13].isExpandedByDefault, true);
  assert.equal(states.filter(s => !s.isExpandedByDefault).length, 13);
  // exactly one Original, one Latest, twelve "Message N of 14"
  assert.equal(states.filter(s => s.isOriginal).length, 1);
  assert.equal(states.filter(s => s.isLatest).length, 1);
  assert.equal(states.filter(s => /^Message \d+ of 14$/.test(s.label)).length, 12);
  assert.equal(states[0].label, 'Original');
  assert.equal(states[13].label, 'Latest');
  assert.equal(states[6].label, 'Message 7 of 14');
  // who-said-what preserved per card (distinct senders/bodies, never merged)
  assert.equal(states[0].preview, 'body number 1');
  assert.equal(states[13].preview, 'body number 14');
});

test('threadCardStates: an older item with NO threadHistory -> [] (AC-16: no crash, no "undefined")', () => {
  assert.deepEqual(threadCardStates({}), []);
  assert.deepEqual(threadCardStates({ threadHistory: null }), []);
  assert.deepEqual(threadCardStates({ threadHistory: 'not an array' }), []);
  assert.deepEqual(threadCardStates(undefined), []);
  // an empty thread is also zero cards
  assert.deepEqual(threadCardStates({ threadHistory: [], emailBody: 'a body' }), []);
});

test('threadCardStates: previews never contain the literal "undefined" for a missing body', () => {
  const hist = [
    { sender: 'A', timestamp: 1 }, // no body
    { sender: 'B', timestamp: 2, body: 'real body' },
  ];
  const states = threadCardStates({ threadHistory: hist });
  assert.equal(states.length, 2);
  assert.equal(states[0].preview, ''); // a missing body -> empty preview, NOT 'undefined'
  assert.notEqual(states[0].preview, 'undefined');
  assert.equal(states[1].preview, 'real body');
});

test('threadCardStates: a 2-message thread -> Original + Latest only (no "N of M" between)', () => {
  const hist = [
    { sender: 'A', timestamp: 1, body: 'original' },
    { sender: 'B', timestamp: 2, body: 'reply' },
  ];
  const states = threadCardStates({ threadHistory: hist });
  assert.equal(states.length, 2);
  assert.deepEqual(states.map(s => s.label), ['Original', 'Latest']);
  assert.equal(states[0].isExpandedByDefault, false);
  assert.equal(states[1].isExpandedByDefault, true);
});

// --- autoFormatBody: an image marker in the body is PROSE — survives verbatim -
// FEATURE #2 (AC-13). The backend (_html_to_text) produces the image placeholder
// marker "🖼 [image: <filename>]" at strip time; autoFormatBody must treat it as
// ordinary prose — it is NOT promoted to a heading, NOT dropped as an
// empty-emphasis artifact (it carries real word chars), and survives a pass
// VERBATIM. The transform stays idempotent + no-word-loss with the marker present.

test('autoFormatBody: a 🖼 image marker line survives VERBATIM (treated as prose, not stripped)', () => {
  const body = 'Here is the dashboard:\n\n🖼 [image: chart001.gif]\n\nLet me know if it looks right.';
  const out = autoFormatBody(body);
  assert.ok(out.includes('🖼 [image: chart001.gif]'), 'the image marker is preserved verbatim');
  // it is NOT promoted to a heading (no "### 🖼 [image: …]")
  assert.ok(!/^#+\s+🖼/m.test(out), 'the image marker is not turned into a heading');
});

test('autoFormatBody: a body with an image marker is IDEMPOTENT — f(f(x)) === f(x)', () => {
  const body = 'Quarterly numbers attached.\n\n'
    + '🖼 [image: revenue_q3.png]\n\n'
    + 'And the breakdown:\n\n'
    + '🖼 [image]\n\n'
    + 'Thanks.';
  const once = autoFormatBody(body);
  const twice = autoFormatBody(once);
  assert.equal(twice, once, 'second pass must be a no-op with image markers present');
});

test('autoFormatBody: NO WORD LOSS with image markers — every original word survives one pass (in order)', () => {
  const body = 'See the attached screenshot.\n\n'
    + '🖼 [image: screenshot_final.gif]\n\n'
    + 'It shows the new SQL genAI editor in action.\n\n'
    + '🖼 [image]\n\n'
    + 'Reach out with questions.';
  const out = autoFormatBody(body);
  // The marker carries real word chars ("image", the filename) — they must all
  // survive; nothing reordered or dropped.
  assert.deepEqual(words(out.replace(/[#]/g, '')).filter(Boolean), words(body));
  assert.ok(out.includes('🖼 [image: screenshot_final.gif]'), 'the filenamed marker survives');
  assert.ok(out.includes('🖼 [image]'), 'the generic marker survives');
});

test('autoFormatBody: a generic "🖼 [image]" marker is NOT an empty-emphasis artifact (kept)', () => {
  // The empty-emphasis artifact stripper only drops lines whose chars are all
  // * _ and whitespace. The image marker has letters/brackets, so it is kept.
  const body = '🖼 [image]';
  assert.equal(autoFormatBody(body), body);
});

// --- approveOutcome: how the approve POST response is dispatched (T2) -------
// The approve POST has FOUR distinct outcomes the page must render differently
// (D-068/T2). The backend was the SILENT-SUCCESS-APPROVE bug: a no-recipient
// approve flipped the item to "approved" and returned 200 with draftSaved:false,
// which the page rendered as the quiet green "Copied!" — telling the user a draft
// was saved when none was. T1 changes that to a 422 {error:'no_recipient',
// message:...}. approveOutcome turns (httpStatus, body) into a tagged decision so
// the four cases NEVER collapse into one another:
//   - 409                       -> { outcome: 'conflict' } (queue moved; refresh + banner)
//   - 422 & error 'no_recipient'-> { outcome: 'no_recipient', message } (RED banner, item
//                                    stays unapproved/retryable — DISTINCT from 409, no refresh-as-conflict)
//   - any other non-2xx         -> { outcome: 'error', message } (generic failure banner)
//   - 2xx                       -> { outcome: 'ok', draftSaved, etag } (green toast: draftSaved
//                                    true => "Copied! Draft saved to Outlook.", false => "Copied!")
// The no_recipient case MUST be distinct from the 409 conflict (anti
// CONFLATE-NO-RECIPIENT-WITH-409 — a "Conflict... Refreshing" banner sends the
// user to refresh uselessly instead of "add a recipient and retry"). Pure — no
// DOM, no fetch.

test('approveOutcome: 2xx with draftSaved:true -> "ok", draftSaved true, adopts etag', () => {
  const r = approveOutcome(200, { available: true, draftSaved: true, item: {}, etag: 'e7' });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.draftSaved, true);
  assert.equal(r.etag, 'e7');
});

test('approveOutcome: 2xx with draftSaved:false -> "ok", draftSaved false (still a green Copied!, NOT no_recipient)', () => {
  // This is now a NON-recipient reason (e.g. a domain-blocked draft), since
  // no-recipient is a 4xx. The page keeps today's plain "Copied!" here.
  const r = approveOutcome(200, { available: true, draftSaved: false, item: {}, etag: 'e8' });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.draftSaved, false);
  assert.notEqual(r.outcome, 'no_recipient');
});

test('approveOutcome: 409 conflict -> "conflict" (refresh + banner), NEVER no_recipient', () => {
  const r = approveOutcome(409, { error: 'conflict', current: {}, etag: 'e2' });
  assert.equal(r.outcome, 'conflict');
  assert.notEqual(r.outcome, 'no_recipient');
});

test('approveOutcome: 422 no_recipient -> "no_recipient", carries the backend message (RED banner)', () => {
  // The headline SILENT-SUCCESS-APPROVE fix: a no-recipient approve is a 422,
  // NOT a 200/draftSaved:false. It must surface a distinct red-banner outcome
  // with the actionable "add a recipient and retry" message.
  const r = approveOutcome(422, {
    error: 'no_recipient',
    message: 'No recipient address — draft not saved; add a recipient and retry',
  });
  assert.equal(r.outcome, 'no_recipient');
  assert.equal(r.message, 'No recipient address — draft not saved; add a recipient and retry');
  // distinct from conflict so the page does NOT show "Conflict... Refreshing"
  assert.notEqual(r.outcome, 'conflict');
  // distinct from ok so the page does NOT show the green "Copied!" toast
  assert.notEqual(r.outcome, 'ok');
});

test('approveOutcome: no_recipient with a missing message -> a sane default (never "undefined")', () => {
  const r = approveOutcome(422, { error: 'no_recipient' });
  assert.equal(r.outcome, 'no_recipient');
  assert.ok(r.message && r.message.length > 0);
  assert.notEqual(r.message, 'undefined');
});

test('approveOutcome: a 422 that is NOT no_recipient -> generic "error" (not the recipient banner)', () => {
  const r = approveOutcome(422, { error: 'something_else', message: 'other' });
  assert.equal(r.outcome, 'error');
  assert.notEqual(r.outcome, 'no_recipient');
});

test('approveOutcome: a generic non-2xx (500) -> "error" with a readable message', () => {
  const r = approveOutcome(500, { error: 'boom' });
  assert.equal(r.outcome, 'error');
  assert.ok(r.message && typeof r.message === 'string');
});

test('approveOutcome: a null/garbage body never crashes and degrades to "error" on non-2xx', () => {
  assert.equal(approveOutcome(500, null).outcome, 'error');
  assert.equal(approveOutcome(502, undefined).outcome, 'error');
  // a 2xx with a null body still resolves to ok (the happy path tolerates an
  // empty body — draftSaved falls to false, the page shows "Copied!").
  assert.equal(approveOutcome(200, null).outcome, 'ok');
});

// --- approveOutcome: the FIFTH outcome — unresolved_recipients (D-069 part 4) -
// The contacts resolver leaves a genuinely-unresolvable name-only TO recipient
// with email:'' rather than fabricating an address. Saving a partial draft would
// silently DROP those people (the anti-SILENT-DROP intent). T1 makes approve
// fail loud with a NEW 422 code {error:'unresolved_recipients', message, names,
// etag} that is DISTINCT from both the 409 conflict and the 422 no_recipient.
// approveOutcome must map it to a 5th tagged outcome carrying the unresolved
// display names so the page can name exactly who to add manually — never
// collapsing it onto conflict (which would uselessly refresh) or no_recipient
// (which names no one) or the generic error.

test('approveOutcome: 422 unresolved_recipients -> 5th distinct outcome carrying message + names', () => {
  const r = approveOutcome(422, {
    error: 'unresolved_recipients',
    message: "Couldn't resolve these recipients: Monnig, Benjamin; Wang, Xiaoguang(Ken)",
    names: ['Monnig, Benjamin', 'Wang, Xiaoguang(Ken)'],
    etag: 'e5',
  });
  assert.equal(r.outcome, 'unresolved_recipients');
  assert.equal(r.message, "Couldn't resolve these recipients: Monnig, Benjamin; Wang, Xiaoguang(Ken)");
  assert.deepEqual(r.names, ['Monnig, Benjamin', 'Wang, Xiaoguang(Ken)']);
  assert.equal(r.etag, 'e5');
  assert.equal(r.draftSaved, false);
  // DISTINCT from every other outcome — must not be swallowed by any of them.
  assert.notEqual(r.outcome, 'conflict');
  assert.notEqual(r.outcome, 'no_recipient');
  assert.notEqual(r.outcome, 'error');
  assert.notEqual(r.outcome, 'ok');
});

test('approveOutcome: unresolved_recipients with a missing message -> sane default that NAMES the people (never "undefined")', () => {
  const r = approveOutcome(422, { error: 'unresolved_recipients', names: ['Cao, Xulai'] });
  assert.equal(r.outcome, 'unresolved_recipients');
  assert.ok(r.message && r.message.length > 0);
  assert.notEqual(r.message, 'undefined');
  // the default names the unresolved recipient(s) so the user knows who to add
  assert.ok(r.message.includes('Cao, Xulai'));
  assert.deepEqual(r.names, ['Cao, Xulai']);
});

test('approveOutcome: unresolved_recipients with a non-array names -> names defaults to []', () => {
  const r = approveOutcome(422, { error: 'unresolved_recipients', message: 'm', names: 'not-an-array' });
  assert.equal(r.outcome, 'unresolved_recipients');
  assert.deepEqual(r.names, []);
});

test('approveOutcome: the FIVE outcomes stay DISTINCT — no_recipient is NOT mistaken for unresolved_recipients', () => {
  // A 422 no_recipient (fully-empty To) must STILL be 'no_recipient', not the
  // new code — they hinge on b.error, not the status.
  const noRec = approveOutcome(422, { error: 'no_recipient', message: 'add a recipient and retry' });
  assert.equal(noRec.outcome, 'no_recipient');
  assert.notEqual(noRec.outcome, 'unresolved_recipients');

  // A 409 is still conflict, never the new code.
  const conflict = approveOutcome(409, { error: 'conflict', etag: 'e2' });
  assert.equal(conflict.outcome, 'conflict');
  assert.notEqual(conflict.outcome, 'unresolved_recipients');

  // A 422 unresolved_recipients is its own outcome.
  const unres = approveOutcome(422, { error: 'unresolved_recipients', message: 'm', names: ['A'] });
  assert.equal(unres.outcome, 'unresolved_recipients');

  // A different 422 falls through to generic error, NOT the new code.
  const other = approveOutcome(422, { error: 'something_else' });
  assert.equal(other.outcome, 'error');
  assert.notEqual(other.outcome, 'unresolved_recipients');

  // The happy path stays ok.
  assert.equal(approveOutcome(200, { draftSaved: true }).outcome, 'ok');
});

// --- approveOutcome: D-071 markReadFailed / markReadMessage on the 2xx branch -
// The subfolder (OWA) mark-read fix routes a folder item's ORIGINAL message
// through the off-quota Graph mark_email_read path AFTER the durable draft-save.
// The draft-save is the PRIMARY action and always wins, so when the mark-read
// can't complete (Graph id unresolved, or mark_email_read reports failure) the
// approve STILL succeeds (2xx) but the body carries markReadFailed:true (+ a short
// markReadMessage). The page must then show a NON-blocking amber notice ("Draft
// saved — couldn't mark the original read.") instead of a plain green "Copied!" —
// the fix for the silent-green complaint (green shown while the original stayed
// unread in Outlook). These fields ride ONLY on the 2xx 'ok' branch; on
// conflict/no_recipient/unresolved_recipients/error they are meaningless (no
// draft saved, no mark-read attempted). markReadFailed DEFAULTS to false (strict
// `=== true`) so an inbox approve or an OLDER backend that omits the field NEVER
// trips the notice (anti FALSE-ALARM — AC-5). The EXACT field names are pinned
// here so a backend rename goes RED. Escape is the render boundary's concern (T4),
// not this pure reader's.

test('approveOutcome: 2xx + markReadFailed:true + message -> ok, markReadFailed true, exact message', () => {
  const r = approveOutcome(200, {
    available: true,
    draftSaved: true,
    item: {},
    etag: 'e7',
    markReadFailed: true,
    markReadMessage: "Draft saved — couldn't mark the original read.",
  });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.draftSaved, true);
  assert.equal(r.markReadFailed, true);
  assert.equal(r.markReadMessage, "Draft saved — couldn't mark the original read.");
  assert.equal(r.etag, 'e7');
});

test('approveOutcome: 2xx with markReadFailed ABSENT -> markReadFailed false (anti false-alarm, AC-5)', () => {
  // An inbox approve OR an older backend that predates D-071 omits the field
  // entirely — it must never trip the notice (false defaults).
  const r = approveOutcome(200, { available: true, draftSaved: true, item: {}, etag: 'e8' });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.markReadFailed, false);
  assert.equal(r.markReadMessage, '');
});

test('approveOutcome: 2xx with markReadFailed:false -> false (mark-read succeeded, no notice)', () => {
  const r = approveOutcome(200, {
    available: true, draftSaved: true, etag: 'e9', markReadFailed: false, markReadMessage: '',
  });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.markReadFailed, false);
  assert.equal(r.markReadMessage, '');
});

test('approveOutcome: markReadFailed coerces strictly — only literal true trips it', () => {
  // A truthy-but-not-true value (1, "true", {}) must NOT trip the notice — the
  // backend contract is a real boolean true.
  for (const bad of [1, 'true', {}, 'yes']) {
    const r = approveOutcome(200, { draftSaved: true, markReadFailed: bad });
    assert.equal(r.markReadFailed, false, `markReadFailed must be false for ${JSON.stringify(bad)}`);
  }
});

test('approveOutcome: markReadMessage is trimmed; blank/non-string -> "" (never "undefined")', () => {
  assert.equal(approveOutcome(200, { markReadFailed: true, markReadMessage: '  hi  ' }).markReadMessage, 'hi');
  assert.equal(approveOutcome(200, { markReadFailed: true, markReadMessage: '   ' }).markReadMessage, '');
  assert.equal(approveOutcome(200, { markReadFailed: true, markReadMessage: 42 }).markReadMessage, '');
  assert.equal(approveOutcome(200, { markReadFailed: true }).markReadMessage, '');
  assert.notEqual(approveOutcome(200, { markReadFailed: true }).markReadMessage, 'undefined');
});

test('approveOutcome: markReadFailed is IGNORED on the failed branches (meaningless on a failed approve)', () => {
  // A 409 / 422 / 500 never carries a draft-saved mark-read decision — the field
  // must not leak a `true` onto a failure outcome (it would mislead the UI).
  const conflict = approveOutcome(409, { error: 'conflict', etag: 'e2', markReadFailed: true });
  assert.equal(conflict.outcome, 'conflict');
  assert.notEqual(conflict.markReadFailed, true);

  const noRec = approveOutcome(422, { error: 'no_recipient', markReadFailed: true });
  assert.equal(noRec.outcome, 'no_recipient');
  assert.notEqual(noRec.markReadFailed, true);

  const unres = approveOutcome(422, { error: 'unresolved_recipients', names: ['A'], markReadFailed: true });
  assert.equal(unres.outcome, 'unresolved_recipients');
  assert.notEqual(unres.markReadFailed, true);

  const err = approveOutcome(500, { error: 'boom', markReadFailed: true });
  assert.equal(err.outcome, 'error');
  assert.notEqual(err.markReadFailed, true);
});

// --- dismissOutcome: the dismiss path's tagged outcome (T3, D-071) ----------
// Dismiss now ALSO routes a subfolder item's original through the Graph
// mark_email_read path, so it shares the SAME mark-read failure surface as
// approve. dismissOutcome mirrors approveOutcome's shape so the dismiss notice is
// derived the SAME testable way — { outcome:'conflict'|'error'|'ok', etag,
// markReadFailed, markReadMessage }. As with approve, markReadFailed/Message ride
// ONLY the 2xx branch and DEFAULT to false (strict `=== true`) so an inbox dismiss
// or an older backend never trips the notice (AC-5). Dismiss has no draftSaved.

test('dismissOutcome: 2xx + markReadFailed:true + message -> ok, markReadFailed true, exact message', () => {
  const r = dismissOutcome(200, {
    ok: true,
    removed: true,
    markReadFailed: true,
    markReadMessage: "Dismissed — couldn't mark the original read.",
  });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.markReadFailed, true);
  assert.equal(r.markReadMessage, "Dismissed — couldn't mark the original read.");
});

test('dismissOutcome: 2xx with markReadFailed ABSENT -> markReadFailed false (anti false-alarm, AC-5)', () => {
  const r = dismissOutcome(200, { ok: true, removed: true });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.markReadFailed, false);
  assert.equal(r.markReadMessage, '');
});

test('dismissOutcome: 2xx with markReadFailed:false -> false (mark-read succeeded, no notice)', () => {
  const r = dismissOutcome(200, { ok: true, markReadFailed: false, markReadMessage: '' });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.markReadFailed, false);
  assert.equal(r.markReadMessage, '');
});

test('dismissOutcome: 409 conflict -> "conflict", markReadFailed ignored (false), adopts etag', () => {
  const r = dismissOutcome(409, { error: 'conflict', etag: 'e2', current: {}, markReadFailed: true });
  assert.equal(r.outcome, 'conflict');
  assert.notEqual(r.markReadFailed, true);
  assert.equal(r.etag, 'e2');
});

test('dismissOutcome: non-2xx (500) -> "error" with a readable message, markReadFailed ignored', () => {
  const r = dismissOutcome(500, { error: 'boom', markReadFailed: true });
  assert.equal(r.outcome, 'error');
  assert.ok(r.message && typeof r.message === 'string');
  assert.notEqual(r.message, 'undefined');
  assert.notEqual(r.markReadFailed, true);
});

test('dismissOutcome: markReadFailed coerces strictly — only literal true trips it', () => {
  for (const bad of [1, 'true', {}, 'yes']) {
    const r = dismissOutcome(200, { ok: true, markReadFailed: bad });
    assert.equal(r.markReadFailed, false, `markReadFailed must be false for ${JSON.stringify(bad)}`);
  }
});

test('dismissOutcome: markReadMessage is trimmed; blank/non-string -> "" (never "undefined")', () => {
  assert.equal(dismissOutcome(200, { markReadFailed: true, markReadMessage: '  hi  ' }).markReadMessage, 'hi');
  assert.equal(dismissOutcome(200, { markReadFailed: true, markReadMessage: '   ' }).markReadMessage, '');
  assert.equal(dismissOutcome(200, { markReadFailed: true, markReadMessage: 42 }).markReadMessage, '');
  assert.notEqual(dismissOutcome(200, { markReadFailed: true }).markReadMessage, 'undefined');
});

test('dismissOutcome: a null/garbage body never crashes and degrades to "error" on non-2xx', () => {
  assert.equal(dismissOutcome(500, null).outcome, 'error');
  assert.equal(dismissOutcome(502, undefined).outcome, 'error');
  // a 2xx with a null body still resolves to ok (markReadFailed defaults false).
  const ok = dismissOutcome(200, null);
  assert.equal(ok.outcome, 'ok');
  assert.equal(ok.markReadFailed, false);
});

// --- approveOutcome: APPROVE FAIL-LOUD RED-banner routing (D-072 clause 4, T2) -
// VERIFY + REGRESSION-PIN. The backend already returns a 422 BEFORE flipping
// status or saving a draft: {error:'no_recipient'} when reply-all resolves to a
// fully-empty To (server/routes/email.py ~5684), and {error:'unresolved_recipients',
// names:[...]} when the To partially resolved but one or more ORIGINAL name-only
// recipients couldn't be mapped to an address (~5639). EmailPage.approve()
// dispatches approveOutcome(res.status, json) and routes:
//   no_recipient / unresolved_recipients / error -> a RED setError(...) banner
//        (item stays unapproved, NO refresh, NEVER the green "Copied!")
//   409                                          -> the DISTINCT "Conflict... Refreshing" banner + refresh()
//   2xx draftSaved                               -> the green "Copied!" toast
// These tests pin the four outcomes so a FUTURE change that collapses
// no_recipient / unresolved_recipients onto the 409 conflict (anti
// CONFLATE-WITH-409) or onto the ok toast (anti SILENT-GREEN-APPROVE) goes RED
// here — even though the page's setError vs. toast pixels stay browser-unverified.

test('AC-10 #1: httpStatus 422 + error no_recipient -> {outcome:no_recipient, draftSaved:false, non-empty message}', () => {
  const r = approveOutcome(422, {
    error: 'no_recipient',
    message: 'No recipient address — draft not saved; add a recipient and retry',
  });
  assert.equal(r.outcome, 'no_recipient');
  assert.equal(r.draftSaved, false);
  assert.ok(r.message && r.message.length > 0, 'message must be non-empty');
  assert.notEqual(r.message, 'undefined');
  // routes to the RED setError banner — NOT the 409 conflict, NOT the green toast
  assert.notEqual(r.outcome, 'conflict');
  assert.notEqual(r.outcome, 'ok');
});

test('AC-10 #2: httpStatus 422 + error unresolved_recipients + names[] -> {outcome:unresolved_recipients, names, message NAMING the people}', () => {
  const names = ['Wang, Yibo', 'Liu, Yang (Jonathan)'];
  const r = approveOutcome(422, { error: 'unresolved_recipients', names });
  assert.equal(r.outcome, 'unresolved_recipients');
  assert.deepEqual(r.names, names);
  assert.equal(r.draftSaved, false);
  // the message must NAME every unresolved person so the banner is actionable
  assert.ok(r.message.includes('Wang, Yibo'), 'message names the first unresolved recipient');
  assert.ok(r.message.includes('Liu, Yang (Jonathan)'), 'message names the second unresolved recipient');
  assert.notEqual(r.message, 'undefined');
  // DISTINCT from conflict and ok (and from no_recipient, which names no one)
  assert.notEqual(r.outcome, 'conflict');
  assert.notEqual(r.outcome, 'ok');
  assert.notEqual(r.outcome, 'no_recipient');
});

test('AC-10 #3: httpStatus 409 -> {outcome:conflict} — DISTINCT from the two 422 RED outcomes (anti CONFLATE-WITH-409)', () => {
  const r = approveOutcome(409, { error: 'conflict', etag: 'e2', current: {} });
  assert.equal(r.outcome, 'conflict');
  // The 409 "Conflict... Refreshing" path must NEVER be reached by the two 422
  // fail-loud codes — refreshing wouldn't fix a missing/unresolved recipient.
  assert.notEqual(r.outcome, 'no_recipient');
  assert.notEqual(r.outcome, 'unresolved_recipients');
  assert.notEqual(r.outcome, 'ok');
});

test('AC-10 #4: a 2xx with draftSaved -> {outcome:ok} (the green "Copied!" path) — provably NOT the RED cases', () => {
  const r = approveOutcome(200, { available: true, draftSaved: true, item: {}, etag: 'e7' });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.draftSaved, true);
  // the happy path stays the green toast and is NEVER one of the RED fail-loud
  // outcomes (so the RED cases are provably distinct from the green toast).
  assert.notEqual(r.outcome, 'no_recipient');
  assert.notEqual(r.outcome, 'unresolved_recipients');
  assert.notEqual(r.outcome, 'conflict');
  assert.notEqual(r.outcome, 'error');
});

console.log(`\nall green: ${passed} tests passed`);
