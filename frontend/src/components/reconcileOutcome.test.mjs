// Plain-Node branch-table test for the extracted reconcile decision logic.
// Run: `node frontend/src/components/reconcileOutcome.test.mjs`
// No npm dependency, no jsdom/vitest. This is the interaction-path verification
// the prior pass lacked (it only grepped the built bundle for strings): here we
// exercise the EXACT function the component imports for every branch.
import assert from 'node:assert/strict';
import { decideReconcileOutcome, entryKey } from './reconcileOutcome.js';

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

const loaded = [
  { date: '2026-06-01', text: 'alpha note' },
  { date: '2026-06-02', text: 'beta note' },
];
const loadedKeys = new Set(loaded.map(entryKey));

test('200 → ok + clearEditor, no banner', () => {
  const r = decideReconcileOutcome({
    httpStatus: 200,
    body: { ok: true, etag: 'e2' },
    loadedKeys,
    freshEntries: [],
  });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.clearEditor, true);
  assert.equal(r.arrivedCount, 0);
  assert.equal(r.nextEtag, 'e2');
});

test('200 with no etag in body → ok, nextEtag null', () => {
  const r = decideReconcileOutcome({ httpStatus: 200, body: {}, loadedKeys });
  assert.equal(r.outcome, 'ok');
  assert.equal(r.clearEditor, true);
  assert.equal(r.nextEtag, null);
});

test('409 with new arrivals → conflict, banner N>0, adopt body.etag, keep editor', () => {
  const fresh = [
    ...loaded,
    { date: '2026-06-03', text: 'gamma arrived' },
    { date: '2026-06-04', text: 'delta arrived' },
  ];
  const r = decideReconcileOutcome({
    httpStatus: 409,
    body: { error: 'conflict', etag: 'fresh-etag' },
    loadedKeys,
    freshEntries: fresh,
  });
  assert.equal(r.outcome, 'conflict');
  assert.equal(r.clearEditor, false);
  assert.equal(r.arrivedCount, 2);
  assert.equal(r.nextEtag, 'fresh-etag');
});

test('409 with arrivals=0 → conflict, generic banner (count 0), keep editor', () => {
  // The file changed (e.g. an entry was edited/removed) but no brand-new keys
  // appeared. Still a conflict; the banner shows the generic message.
  const r = decideReconcileOutcome({
    httpStatus: 409,
    body: { error: 'conflict', etag: 'fresh-etag' },
    loadedKeys,
    freshEntries: loaded, // same keys → 0 new
  });
  assert.equal(r.outcome, 'conflict');
  assert.equal(r.clearEditor, false);
  assert.equal(r.arrivedCount, 0);
  assert.equal(r.nextEtag, 'fresh-etag');
});

test('409 with no etag in body → conflict, nextEtag null (do not clobber existing)', () => {
  const r = decideReconcileOutcome({
    httpStatus: 409,
    body: {},
    loadedKeys,
    freshEntries: loaded,
  });
  assert.equal(r.outcome, 'conflict');
  assert.equal(r.nextEtag, null);
  assert.equal(r.clearEditor, false);
});

test('network/throw (httpStatus null) → error, keep editor, re-enable', () => {
  const r = decideReconcileOutcome({ httpStatus: null, body: null, loadedKeys });
  assert.equal(r.outcome, 'error');
  assert.equal(r.clearEditor, false);
  assert.equal(r.arrivedCount, 0);
  assert.equal(r.nextEtag, null);
});

test('non-409 server error (500) → error, keep editor', () => {
  const r = decideReconcileOutcome({ httpStatus: 500, body: {}, loadedKeys });
  assert.equal(r.outcome, 'error');
  assert.equal(r.clearEditor, false);
});

test('clearEditor is true ONLY for ok', () => {
  for (const status of [409, 500, 400, null, 0, undefined]) {
    const r = decideReconcileOutcome({ httpStatus: status, body: {}, loadedKeys, freshEntries: loaded });
    assert.equal(r.clearEditor, false, `status ${status} must not clear the editor`);
  }
  assert.equal(decideReconcileOutcome({ httpStatus: 200, body: {}, loadedKeys }).clearEditor, true);
});

test('entryKey is position-independent and trims text', () => {
  assert.equal(entryKey({ date: '2026-06-01', text: '  x  ' }), '2026-06-01|x');
  assert.equal(entryKey({ text: 'y' }), '|y');
});

console.log(`\nall green: ${passed} tests passed`);
