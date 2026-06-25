// Pure decision helpers for EmailPage, extracted so the page AND its Node test
// exercise the SAME code (no jsdom/vitest needed; matches reconcileOutcome.js).
// Each function is a pure transform of its inputs — no DOM, no fetch, no state —
// so the load-bearing data decisions behind the new UI (thread-history timeline,
// Polish, the muted-conversations section, auto-sizing) are unit-checkable.

// Read an item's thread history defensively. The scan worker stores an ordered
// array of structured turns ({sender, timestamp, body}) on `threadHistory`; older
// items predate the field. Always return an array so the timeline renders N cards
// for N turns, 1 for 1, and a graceful placeholder for empty/missing — never
// crashing and never rendering the literal 'undefined'.
export function threadTurns(item) {
  return Array.isArray(item && item.threadHistory) ? item.threadHistory : [];
}

// Decide what Thread Context shows by default vs. behind "Show full email".
// Reuses threadTurns(item): when there are structured turns we show the LATEST
// one by default (the newest turn — the array is oldest→newest, so the last
// element) and offer to expand the full oldest→newest timeline; `count` is the
// REAL turn count and drives the "Show full email (N messages)" label (NOT
// item.messageCount). When there's no structured history we fall back to the
// full concatenated body (then the snippet); `fallbackBody` is ALWAYS a string
// so the render never emits the literal 'undefined' and never crashes.
export function latestThreadView(item) {
  const turns = threadTurns(item);
  if (turns.length > 0) {
    return { latest: turns[turns.length - 1], count: turns.length, turns, fallbackBody: '' };
  }
  return {
    latest: null,
    count: 0,
    turns: [],
    fallbackBody: (item && (item.emailBody || item.snippet)) || '',
  };
}

// Decide what the Thread Summary section's two AI-derived sub-parts show. The
// "Summary" paragraph reuses the existing AI-produced threadContext; the "What
// they need from you" line reuses a separate AI-produced threadAsk. BOTH are
// optional: an older item, an FYI with no real ask, or a generation that didn't
// emit one leaves a field empty. We trim and report has* so the render shows a
// muted placeholder (never the literal 'undefined', never a fabricated ask).
// Non-string values are coerced via String() so a stray number can't surface as
// 'undefined'/'null'. Pure — no DOM, no fetch.
export function threadSummaryParts(item) {
  const it = item || {};
  const summary = it.threadContext == null ? '' : String(it.threadContext).trim();
  const ask = it.threadAsk == null ? '' : String(it.threadAsk).trim();
  return { summary, ask, hasSummary: summary !== '', hasAsk: ask !== '' };
}

// Build the email-list "From" cell text. The base name is the latest reply's
// sender (the scan worker backfills item.sender to the newest turn's sender —
// see email.py _extract_thread_history), preserving the existing
// `sender || 'unknown'` fallback. When the item carries a senderList (an
// ordered, deduped list of unique participants) with more than one name, append
// ` +N` where N is the number of ADDITIONAL unique senders (length - 1): a
// 2-sender thread reads "+1", a 3-sender "+2". A single-sender, missing, empty,
// or non-array senderList (older items predate the field) gets NO suffix —
// never "+0", never a bare "+". Pure — no DOM, no fetch.
export function fromColumnLabel(item) {
  const it = item || {};
  const base = it.sender || 'unknown';
  const list = it.senderList;
  if (Array.isArray(list) && list.length > 1) {
    return `${base} +${list.length - 1}`;
  }
  return base;
}

// Resolve a mute key to a human-friendly label. The email mute key is the raw
// conversationId (no _threadTs suffix — see email.py _mute_key_for), so we look
// up any queue item on the same conversation and borrow its subject (then sender),
// falling back to the raw key when nothing matches (an aged-out or
// muted-elsewhere conversation we no longer hold an item for).
export function mutedLabel(key, items) {
  const list = Array.isArray(items) ? items : [];
  const match = list.find(it => it && it.conversationId === key);
  if (match) {
    const subject = (match.subject || '').trim();
    if (subject) return subject;
    const sender = (match.sender || '').trim();
    if (sender) return sender;
  }
  return key;
}

// Order the keys of the muted map newest-muted first (values are epoch seconds).
export function sortedMutedKeys(muted) {
  if (!muted || typeof muted !== 'object') return [];
  return Object.keys(muted).sort((a, b) => (muted[b] || 0) - (muted[a] || 0));
}

// Decide which text Polish operates on, plus the empty guard. Polish runs on
// whatever the user is looking at: their unsaved edit if this row is being
// edited, otherwise the stored draft. Returns { ok:false, text:'' } when there is
// nothing to polish (so the caller surfaces the banner and never POSTs an empty
// string). Pure — never reads email, never sends.
export function polishSource(id, editingId, draftText, item) {
  const text = (editingId === id) ? (draftText || '') : ((item && item.draft) || '');
  if (!text.trim()) return { ok: false, text: '' };
  return { ok: true, text };
}

// Decide the per-turn timestamp slot text (D-053 clause 7). A turn from the
// Graph message carries an epoch/ISO timestamp that resolves to a relative-time
// string ("3h ago"); a turn PARSED from a quoted-reply header carries a human
// date string verbatim ("Wednesday, June 24, 2026 at 10:23") that `new Date()`
// cannot parse. Returns:
//   - '' for null/undefined/empty (so renderTurnCard renders no timestamp slot)
//   - the relative-time string when the value parses to a finite Date (same
//     just-now / Nm / Nh / Nd math as EmailPage's relativeTime, kept in sync here)
//   - the VERBATIM trimmed string when it is a non-empty string Date can't parse
//     — NEVER the broken-looking '--' placeholder for a real human date (AC-8)
// Pure — no DOM, no fetch. The page maps an '' result to "render nothing".
export function displayTimestamp(ts) {
  if (ts === null || ts === undefined) return '';
  const raw = typeof ts === 'string' ? ts.trim() : ts;
  if (raw === '') return '';
  const t = new Date(raw).getTime();
  if (Number.isFinite(t)) {
    const diff = Date.now() - t;
    if (diff < 0) return 'just now';
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }
  // Unparseable but real (a quoted-header human date) — show it verbatim,
  // never '--'. A non-string unparseable value (e.g. NaN) has no useful text.
  return typeof ts === 'string' ? raw : '';
}

// Decide whether a delete POST actually deleted the email (Fix task 6 frontend
// half). The delete is fire-and-forget at the HTTP envelope — the backend can
// answer 200/ok:true while the Outlook MOVE later 429s on the exhausted GRASP
// quota — so "handled at the HTTP layer" is NOT "the email was deleted" (the
// verify-effect-not-caller principle). The row flips to "deleted" ONLY when the
// response BODY confirms `deleted === true`. Everything else keeps the row
// VISIBLE in its prior state:
//   - HTTP 409          -> { outcome: 'conflict' }  (queue moved elsewhere; re-read + banner)
//   - non-2xx           -> { outcome: 'failed' }     (show the reason inline)
//   - 2xx & deleted:true-> { outcome: 'deleted' }    (collapse/move to Deleted; adopt etag)
//   - 2xx & !deleted    -> { outcome: 'failed' }     (SWALLOWED-200: move 429'd / old backend)
// The `reason` is the body's error text when present, else a readable default,
// so the page surfaces WHY without ever rendering 'undefined'. Pure — no DOM.
export function deleteOutcome(httpStatus, body) {
  const b = (body && typeof body === 'object') ? body : {};
  const etag = typeof b.etag === 'string' ? b.etag : null;
  if (httpStatus === 409) {
    return { outcome: 'conflict', etag, reason: '' };
  }
  const ok2xx = httpStatus >= 200 && httpStatus < 300;
  if (ok2xx && b.deleted === true) {
    return { outcome: 'deleted', etag, reason: '' };
  }
  const reason = (typeof b.error === 'string' && b.error.trim())
    ? b.error.trim()
    : "Couldn't delete — Outlook rate-limited (GRASP quota), retrying later.";
  return { outcome: 'failed', etag, reason };
}

// Shrink-then-grow a textarea to its content: height = min(scrollHeight + 2, cap).
// The +2 avoids a 1px scrollbar flicker; the cap keeps a pathologically long
// draft from eating the panel (it scrolls past the cap). Extracted from the DOM
// side-effect so the math is testable.
export function autoSizeHeight(scrollHeight, cap) {
  return Math.min((scrollHeight || 0) + 2, cap);
}
