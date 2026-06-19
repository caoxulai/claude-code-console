// Single source of truth for "which Slack queue items are actionable" — i.e.
// the items SlackPage's header counts as "N to review". An item is actionable
// when it still needs human attention: status is needs-draft / needs-review /
// edited, or the status is missing/empty (older items predate the field). This
// is the exact inverse of the terminal states (sent / dismissed). Keep this in
// lockstep with SlackPage's `needsReviewItems` filter so the nav-bubble count
// and the page's "N to review" line can never drift.

export function isActionable(item) {
  const s = item && item.status;
  return s === 'needs-draft' || s === 'needs-review' || s === 'edited' || !s;
}

export function countActionable(items) {
  return Array.isArray(items) ? items.filter(isActionable).length : 0;
}
