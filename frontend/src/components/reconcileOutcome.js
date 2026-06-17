// Pure decision logic for a reconcile POST's outcome, extracted so the panel
// AND its Node test exercise the SAME code (no jsdom/vitest needed). Given the
// raw HTTP result and the entry sets, decide what the UI must do:
//
//   - outcome 'ok'        → the write applied; clear any open editor, drop banners.
//   - outcome 'conflict'  → 409, the file moved under us; adopt the fresh etag,
//                           show the re-review banner (arrivedCount), KEEP the
//                           editor open so the user's typed text survives.
//   - outcome 'error'     → a non-2xx (other than 409) or a thrown/network error;
//                           KEEP the editor open, re-enable the button, show the
//                           "could not reach the server" notice.
//
// The editor-clear rule is intentionally part of the returned shape so the
// branch table (clearEditor only when outcome === 'ok') is unit-checkable.

// Content-based key so an entry set can be diffed across reloads regardless of
// position (an agent may insert an entry mid-file, not only at the tail). Kept
// here so the component and the test share one definition.
export const entryKey = (e) => `${e?.date || ''}|${(e?.text || '').trim()}`;

// `loadedKeys` is a Set (or array) of the entry keys the panel held when the
// user clicked; `freshEntries` is the reloaded entry list after a 409.
export function decideReconcileOutcome({ httpStatus, body, loadedKeys, freshEntries } = {}) {
  const keys = loadedKeys instanceof Set ? loadedKeys : new Set(loadedKeys || []);
  const fresh = Array.isArray(freshEntries) ? freshEntries : [];

  if (httpStatus === 409) {
    const nextEtag = body && body.etag ? body.etag : null;
    const arrivedCount = fresh.filter((e) => !keys.has(entryKey(e))).length;
    return { outcome: 'conflict', nextEtag, arrivedCount, clearEditor: false };
  }
  if (typeof httpStatus === 'number' && httpStatus >= 200 && httpStatus < 300) {
    return { outcome: 'ok', nextEtag: body && body.etag ? body.etag : null, arrivedCount: 0, clearEditor: true };
  }
  // Any other status, OR a thrown/network error (caller passes httpStatus null).
  return { outcome: 'error', nextEtag: null, arrivedCount: 0, clearEditor: false };
}
