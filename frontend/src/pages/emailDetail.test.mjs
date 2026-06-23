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
  mutedLabel,
  sortedMutedKeys,
  polishSource,
  autoSizeHeight,
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

console.log(`\nall green: ${passed} tests passed`);
