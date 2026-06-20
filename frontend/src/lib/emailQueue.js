// Single source of truth for how Email queue items are COUNTED and GROUPED for
// review. Kept in lockstep with EmailPage's sections and the NavBar bubble so the
// "N to review" line and the sidebar count can never drift.
//
// MODEL: an item's lifecycle `status` is one of
//   needs-classify → needs-draft → needs-review (→ edited) → approved | dismissed
// and a SEPARATE advisory `classification` label (needs-reply | fyi | actionable)
// is recorded by the classify worker. The classification NEVER gates work — every
// classified item is drafted and carries full email context — it only decides
// which review GROUP the item shows in. So an 'fyi' item still has a draft you can
// approve with one click; it just sits in the FYI group because a reply is unlikely.

// Counts toward "N to review" and the nav bubble: everything that still needs your
// attention — i.e. anything NOT terminal. 'approved' and 'dismissed' are the only
// terminal (non-counting) states. So needs-classify (still classifying), fyi
// (classified, you may still want to act), needs-draft, needs-review and edited
// ALL count — and a missing/empty status (older items) counts too. This is an
// explicit denylist of the two terminal states so a new active status can never
// silently fall out of the count.
//
// LOCKSTEP CONTRACT (D-035): this MUST stay in sync with server/routes/email.py
// `_count_actionable` / `_TERMINAL_STATUSES` (the GET /api/email/queue/count backend
// the NavBar bubble reads). The terminal denylist here and the one there are two
// halves of one contract — an editor of either side must update the other, or the
// NavBar badge and the page's "N to review" count drift apart.
export function isActionable(item) {
  const s = item && item.status;
  return s !== 'approved' && s !== 'dismissed';
}

export function countActionable(items) {
  return Array.isArray(items) ? items.filter(isActionable).length : 0;
}

// Which review GROUP an actionable item belongs to. Three buckets, all
// counted, all carrying context + a draft:
//   'classifying' — still being triaged by the classify worker (status
//                   needs-classify); its draft/context arrive when triage resolves.
//   'fyi'         — classified as "no reply likely needed"; drafted anyway so you
//                   can approve with one click, but grouped apart so you can clear
//                   it fast (Dismiss all).
//   'reply'       — needs-reply / actionable (or not-yet-classified-but-drafted):
//                   the messages that most likely want a real reply.
// Terminal items (approved/dismissed) are not grouped here — they live in their own
// collapsible sections. Returns null for a terminal item.
export function reviewGroup(item) {
  if (!isActionable(item)) return null;
  if (item && item.status === 'needs-classify') return 'classifying';
  if (item && item.classification === 'fyi') return 'fyi';
  return 'reply';
}
