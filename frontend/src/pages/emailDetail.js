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

// Shrink-then-grow a textarea to its content: height = min(scrollHeight + 2, cap).
// The +2 avoids a 1px scrollbar flicker; the cap keeps a pathologically long
// draft from eating the panel (it scrolls past the cap). Extracted from the DOM
// side-effect so the math is testable.
export function autoSizeHeight(scrollHeight, cap) {
  return Math.min((scrollHeight || 0) + 2, cap);
}
