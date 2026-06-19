// Single source of truth for how Slack queue items are COUNTED and GROUPED for
// review. Kept in lockstep with SlackPage's sections and the NavBar bubble so the
// "N to review" line and the sidebar count can never drift.
//
// MODEL (D-027): an item's lifecycle `status` is one of
//   needs-classify → needs-draft → needs-review (→ edited) → sent | dismissed
// and a SEPARATE advisory `classification` label (needs-reply | fyi | actionable)
// is recorded by the classify worker. The classification NEVER gates work — every
// classified item is drafted and carries full thread context — it only decides
// which review GROUP the item shows in. So an 'fyi' item still has a draft you can
// send with one click; it just sits in the FYI group because a reply is unlikely.

// Counts toward "N to review" and the nav bubble: everything that still needs your
// attention — i.e. anything NOT terminal. 'sent' and 'dismissed' are the only
// terminal (non-counting) states. So needs-classify (still classifying), fyi
// (classified, you may still want to act), needs-draft, needs-review and edited
// ALL count — and a missing/empty status (older items) counts too. This is an
// explicit denylist of the two terminal states so a new active status can never
// silently fall out of the count.
export function isActionable(item) {
  const s = item && item.status;
  return s !== 'sent' && s !== 'dismissed';
}

export function countActionable(items) {
  return Array.isArray(items) ? items.filter(isActionable).length : 0;
}

// Which review GROUP an actionable item belongs to (D-027). Three buckets, all
// counted, all carrying context + a draft:
//   'classifying' — still being triaged by the classify worker (status
//                   needs-classify); its draft/context arrive when triage resolves.
//   'fyi'         — classified as "no reply likely needed"; drafted anyway so you
//                   can send with one click, but grouped apart so you can clear it
//                   fast (Dismiss all).
//   'reply'       — needs-reply / actionable (or not-yet-classified-but-drafted):
//                   the messages that most likely want a real reply.
// Terminal items (sent/dismissed) are not grouped here — they live in their own
// collapsible sections. Returns null for a terminal item.
export function reviewGroup(item) {
  if (!isActionable(item)) return null;
  if (item && item.status === 'needs-classify') return 'classifying';
  if (item && item.classification === 'fyi') return 'fyi';
  return 'reply';
}
