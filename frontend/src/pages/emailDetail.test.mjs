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
