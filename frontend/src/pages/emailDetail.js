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
    // FEATURE #1: for a SINGLE-turn item the turn card IS the whole email, so
    // the card's body source is the FULL item.emailBody (32k cap) — NOT the
    // 4096-capped threadHistory[0].body that cut the CRIS Flash mid-sentence
    // (the SINGLE-MESSAGE-STILL-CAPPED trap). Fall back to the turn body then
    // the snippet so an item lacking emailBody still renders. Multi-turn
    // threads keep each per-turn body verbatim (who-said-what preserved) — we
    // do NOT swap in the full concatenation there.
    if (turns.length === 1) {
      const only = turns[0];
      const fullBody = (item && item.emailBody) || only.body || (item && item.snippet) || '';
      const latest = { ...only, body: fullBody };
      return { latest, count: 1, turns: [latest], fallbackBody: '' };
    }
    return { latest: turns[turns.length - 1], count: turns.length, turns, fallbackBody: '' };
  }
  return {
    latest: null,
    count: 0,
    turns: [],
    fallbackBody: (item && (item.emailBody || item.snippet)) || '',
  };
}

// The user's own address. SINGLE named const, cross-referenced with the backend
// server/routes/email.py `_MY_EMAIL = "user@example.com"` (the minus-me /
// always-CC-me default). If one side changes, change the other.
export const MY_EMAIL = 'user@example.com';

// --- autoFormatBody: conservative, idempotent, content-preserving formatter --
// FEATURE #1 (light auto-formatting, NO LLM). Make flat newsletter prose
// scannable with cheap, deterministic heuristics applied BEFORE the markdown
// render, while PRESERVING paragraph breaks, real markdown lists, GFM tables and
// clickable links. The heuristics are intentionally conservative — they must
// never mangle a normal paragraph, never drop or reorder content, and be
// idempotent: f(f(x)) === f(x), and every word of the original survives one pass
// in the same order.
//
// Two heuristics, both line-based on a blank-line-delimited block model:
//   (1) HEADING: promote a bare line to a markdown subheading ("### ") ONLY when
//       it is SHORT (<= HEADING_MAX_LEN chars, single line) AND it is FOLLOWED
//       BY A BLANK LINE AND it is either (a) a question (ends in '?') or (b) a
//       Title-Case header-like line (every significant word capitalized, no
//       terminal sentence punctuation). The followed-by-blank-line + short bars
//       are load-bearing: they keep a normal sentence that happens to end in '?'
//       or start a paragraph from being mangled (the OVER-EAGER-HEADINGS trap).
//   (2) LIST: leave existing markdown list lines ('- ', '* ', '1. ') intact.
// Both are idempotent — an already-heading line ('### …') is skipped, an
// already-markdown list line is left as-is.
const HEADING_MAX_LEN = 60;

function _isAlreadyMarkdownHeading(line) {
  return /^#{1,6}\s+/.test(line);
}

function _isTableLine(line) {
  // A GFM table row / divider — never reinterpret these as prose/heading.
  return /^\s*\|/.test(line) || /^\s*\|?\s*:?-{3,}/.test(line);
}

function _isListLine(line) {
  // Existing markdown list markers (unordered or ordered) — leave intact.
  return /^\s*([-*+]\s+|\d+[.)]\s+)/.test(line);
}

function _isTitleCaseHeaderLine(line) {
  // Header-like: a short line of mostly Title-Case words with NO terminal
  // sentence punctuation. Conservative: requires >= 2 significant words all
  // starting uppercase (ignoring short connectors like "the"/"of"/"and") and no
  // trailing . , ; : so a normal capitalized sentence is never promoted.
  const trimmed = line.trim();
  if (!trimmed) return false;
  if (/[.,;:]$/.test(trimmed)) return false;
  if (trimmed.endsWith('?')) return false; // questions take the dedicated branch
  const tokens = trimmed.split(/\s+/);
  if (tokens.length < 2 || tokens.length > 8) return false;
  const minor = new Set(['the', 'of', 'and', 'or', 'a', 'an', 'to', 'for', 'in', 'on', 'with', 'at', 'by']);
  let significant = 0;
  for (const tok of tokens) {
    const word = tok.replace(/[^A-Za-z0-9]/g, '');
    if (!word) return false; // punctuation-only token -> not a clean header
    if (minor.has(word.toLowerCase())) continue;
    significant += 1;
    if (!/^[A-Z0-9]/.test(word)) return false; // a significant word not capitalized -> prose
  }
  return significant >= 2;
}

function _isShortQuestionLine(line) {
  const trimmed = line.trim();
  return trimmed.endsWith('?') && trimmed.length <= HEADING_MAX_LEN && !trimmed.includes('\n');
}

export function autoFormatBody(text) {
  if (typeof text !== 'string' || text === '') return '';
  // Split into blank-line-delimited blocks WITHOUT collapsing the blank lines —
  // we rebuild with the exact original separators so paragraph breaks survive.
  const lines = text.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();
    // The next non-removed line tells us whether this line is "followed by a
    // blank line" (the required heading guard). A line is followed-by-blank when
    // the immediately next line is blank OR it is the last line.
    const nextLine = i + 1 < lines.length ? lines[i + 1] : '';
    const followedByBlank = i + 1 >= lines.length || nextLine.trim() === '';

    const isHeadingCandidate =
      trimmed.length > 0 &&
      trimmed.length <= HEADING_MAX_LEN &&
      followedByBlank &&
      !_isAlreadyMarkdownHeading(trimmed) &&
      !_isListLine(line) &&
      !_isTableLine(line) &&
      (_isShortQuestionLine(line) || _isTitleCaseHeaderLine(line));

    if (isHeadingCandidate) {
      // Only the leading whitespace of the line is dropped (insignificant); the
      // text content is preserved verbatim after the "### " marker.
      out.push(`### ${trimmed}`);
    } else {
      out.push(line);
    }
  }
  return out.join('\n');
}

// --- replyAllRecipients: reply-all default To/CC resolution -----------------
// FEATURE #3 (UI default). When the draft editor opens, compute the reply-all
// recipients from the item the backend captured:
//   To = original sender (item.senderEmail) + the original `to` recipients,
//        with the user's own address (MY_EMAIL) removed (never reply to self).
//   CC = the original `cc` recipients with MY_EMAIL removed, then ALWAYS add
//        MY_EMAIL (always-CC-me, even when the original CC was empty).
// Both lists are de-duped case-insensitively by email. If the item carries no
// captured to/cc (an older item, or the data was absent) it degrades to
// To=[sender], CC=[me] — NEVER fabricating another address. Recipient entries
// may be {name,email} dicts OR bare email strings; blank or shape-invalid
// entries are skipped (no fabrication). Pure — no DOM, no fetch.
function _emailOf(entry) {
  if (entry && typeof entry === 'object') return String(entry.email || '').trim();
  if (typeof entry === 'string') return entry.trim();
  return '';
}

function _looksLikeEmail(addr) {
  // Basic shape check only — local@domain.tld-ish. Conservative: drops obvious
  // non-addresses ("not-an-email") without trying to validate exhaustively.
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(addr);
}

function _dedupeByEmailCI(emails) {
  const seen = new Set();
  const out = [];
  for (const e of emails) {
    const addr = (e || '').trim();
    if (!addr || !_looksLikeEmail(addr)) continue;
    const key = addr.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(addr);
  }
  return out;
}

export function replyAllRecipients(item) {
  const it = item || {};
  const myKey = MY_EMAIL.toLowerCase();
  const sender = String(it.senderEmail || '').trim();
  const origTo = Array.isArray(it.toRecipients) ? it.toRecipients : [];
  const origCc = Array.isArray(it.ccRecipients) ? it.ccRecipients : [];

  // To = sender + original To, minus me. De-dupe CI so a sender who is also in
  // the original To list isn't doubled.
  const toRaw = [sender, ...origTo.map(_emailOf)].filter(
    addr => addr && addr.toLowerCase() !== myKey,
  );
  const to = _dedupeByEmailCI(toRaw);

  // CC = original CC minus me, then always add me last.
  const ccRaw = origCc.map(_emailOf).filter(addr => addr && addr.toLowerCase() !== myKey);
  const cc = _dedupeByEmailCI([...ccRaw, MY_EMAIL]);

  return { to, cc };
}

// Decide which recipients the reply-all editor opens with (FEATURE #3, AC-12).
// When the backend flipped item.recipientsEdited (save_draft persisted the user's
// edited To/CC as plain email-string lists — see email.py save_draft), seed
// DIRECTLY from those persisted lists so a page refresh keeps the user's exact
// edit verbatim (we must NOT re-run replyAllRecipients, which would re-add the
// sender / re-add me and clobber an intentional removal — the RECIPIENTS-NOT-
// PERSISTED trap). Otherwise (a fresh item, only the captured {name,email}
// lists) compute the reply-all default. Either way the result is validated +
// de-duped case-insensitively so a persisted list is never trusted blindly.
// Pure — no DOM, no fetch.
export function seedRecipients(item) {
  const it = item || {};
  if (it.recipientsEdited === true) {
    const to = _dedupeByEmailCI((Array.isArray(it.toRecipients) ? it.toRecipients : []).map(_emailOf));
    const cc = _dedupeByEmailCI((Array.isArray(it.ccRecipients) ? it.ccRecipients : []).map(_emailOf));
    return { to, cc };
  }
  return replyAllRecipients(it);
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
