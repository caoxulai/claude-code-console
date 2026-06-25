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
  mutedLabel,
  sortedMutedKeys,
  polishSource,
  autoSizeHeight,
  displayTimestamp,
  deleteOutcome,
} from './emailDetail.js';

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

console.log(`\nall green: ${passed} tests passed`);
