import { Fragment, useEffect, useRef, useState } from 'react';
import {
  FiRefreshCw, FiSend, FiRotateCw, FiTrash2, FiSave, FiChevronDown, FiChevronRight,
  FiCheckCircle, FiAlertCircle, FiClock, FiXCircle,
} from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
import { isActionable } from '../lib/slackQueue';

// Length (seconds) of the client-side cancellable window shown after Approve &
// Send is confirmed, before the per-item approve call actually fires. Undo
// during this window cancels the send entirely (no approve POST is made).
const UNDO_WINDOW_S = 9;

// How often the read-only backstop poll re-reads GET /api/slack/queue. The cron
// is the sole scanner/producer now; this poll never scans or POSTs — it is a
// cheap GET that re-reads the warm queue so a cron write surfaces promptly even
// if the slack_changed broadcast is missed. Suppressed while editing/sending and
// while the tab is hidden (see the effect below). The primary repaint path is
// still the slack_changed broadcast; this is the backstop.
const QUEUE_POLL_INTERVAL_MS = 25000;

// How often the "Updated Xs/Xm ago" scan-freshness label re-renders so it stays
// current WITHOUT a fetch. Just ticks a counter to force a re-render; no network
// call. (Drives the scan-freshness label — see `lastScanAt` — not page-fetch time.)
const SCAN_LABEL_TICK_MS = 20000;

// Coerce a raw `lastScanAt` value from the backend to a finite number or null.
// The backend reports epoch millis (Number) when a scan has completed, or
// null/absent (never scanned). A non-finite/non-number value degrades to null so
// the freshness label hides gracefully instead of feeding junk into relativeTime.
function finiteOrNull(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

// Relative-time label for a timestamp. The backend stores `ts` as epoch
// millis (Number); thread turns may carry an ISO string. `new Date(...)`
// handles both. Defensive: a missing/invalid value reads as '--'.
function relativeTime(ts) {
  if (ts === null || ts === undefined || ts === '') return '--';
  const t = new Date(ts).getTime();
  if (!Number.isFinite(t)) return '--';
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

function truncate(str, len) {
  if (!str) return '';
  return str.length > len ? str.slice(0, len) + '...' : str;
}

// Clear a setTimeout handle held in a ref and null it out, if present. Mirrors
// the per-send `clearTimersFor` helper for the page's single-shot hint timers
// (scanHintRef / scanNoticeRef) so the "clear and forget" idiom lives in one place.
function clearRef(ref) {
  if (ref.current) {
    clearTimeout(ref.current);
    ref.current = null;
  }
}

function stripSlackXml(text) {
  if (!text) return '';
  return text.replace(/<slack-user-content[^>]*>([\s\S]*?)<\/slack-user-content>/g, '$1')
    .replace(//g, '').trim();
}

// A compact word-level diff between the originally-generated draft and the
// user's current edit. Returns an array of {value, type} tokens where type is
// 'same' | 'added' | 'removed'. Used read-only in the detail panel so the user
// can see exactly what they changed before approving — the same edit becomes
// the backend's preference-learning signal. Pure; tolerates empty inputs.
//
// LCS over whitespace-split tokens. Inputs are short drafts (capped server
// side) so the O(n*m) table is cheap.
function wordDiff(before, after) {
  const a = (before || '').split(/(\s+)/).filter(Boolean);
  const b = (after || '').split(/(\s+)/).filter(Boolean);
  const n = a.length, m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  const push = (value, type) => {
    const last = out[out.length - 1];
    if (last && last.type === type) last.value += value;
    else out.push({ value, type });
  };
  while (i < n && j < m) {
    if (a[i] === b[j]) { push(a[i], 'same'); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { push(a[i], 'removed'); i++; }
    else { push(b[j], 'added'); j++; }
  }
  while (i < n) { push(a[i], 'removed'); i++; }
  while (j < m) { push(b[j], 'added'); j++; }
  return out;
}

// A human label for where an item came from.
//   - DMs read as "DM · <sender>".
//   - A channel/@mention reads as "#<slug> · from <sender>" so a group source
//     is legible (which channel AND who posted) instead of a bare slug — e.g.
//     "#chinese-in-sea · from Yuanfeng Jia".
// NOTE: this is a PURE-FRONTEND humanization of the fields the item already
// carries (channel slug + sender). The cron does NOT produce a channelName
// field today; if a future item carries `channelName` we prefer it, but we do
// NOT fabricate a display name that could misrepresent the source — absent a
// real name we keep the verbatim slug (with a leading '#') and just append the
// poster. The slug-with-'#' form is the safest non-fabricating choice.
function sourceLabel(item) {
  if (item.channelType === 'dm') return 'DM';
  // Explicit group_dm channelType (from list_dms isGroup=true). Handles both
  // mpdm- slugs and any future non-mpdm group DM naming.
  if (item.channelType === 'group_dm') {
    const ch = item.channel || '';
    if (ch.startsWith('mpdm-')) {
      const members = ch.replace(/^mpdm-/, '').replace(/-\d+$/, '')
        .split('--').filter(n => n !== 'xulaicao');
      return `Group · ${members.length ? members.join(', ') : 'group'}`;
    }
    return 'Group DM';
  }
  // Legacy fallback: older items (channelType 'mention') with an mpdm- slug
  // still render as group — kept for backward compatibility.
  const ch = item.channel || '';
  if (ch.startsWith('mpdm-')) {
    const members = ch.replace(/^mpdm-/, '').replace(/-\d+$/, '')
      .split('--').filter(n => n !== 'xulaicao');
    return `Group · ${members.length ? members.join(', ') : 'group'}`;
  }
  if (ch) {
    const name = item.channelName
      ? `#${String(item.channelName).replace(/^#/, '')}`
      : (ch.startsWith('#') ? ch : `#${ch}`);
    return `${name} · @`;
  }
  return '';
}

// Status badge styling. App.css owns .badge-ok (green) / .badge-warn (amber);
// 'sent' reuses badge-ok and 'edited' reuses badge-warn (an edited-but-not-sent
// draft is the preference-learning signal, so amber draws the eye). The
// 'needs-review' tone has no App.css class — styled inline here with the muted
// info-blue palette (matching .badge-user) so it's distinct but not bright and
// comfortable in dark mode, mirroring CronsPage's FIRED_BADGE_STYLE approach.
const NEEDS_REVIEW_BADGE_STYLE = { background: '#1a2a3a', color: '#7ab8e6' };

// A 'needs-draft' item has been listed but no reply has been generated yet
// (Phase A list-only fetch). Styled with a dim neutral/grey palette — distinct
// from the info-blue 'needs review' tone, deliberately quieter (it's a pending,
// not-yet-actionable state) and comfortable in dark mode. No App.css class so
// the App.css diff stays at zero.
const NEEDS_DRAFT_BADGE_STYLE = { background: '#262b31', color: '#9aa3ad' };

// A 'dismissed' item was soft-dismissed (never sent, but preserved for the
// Dismissed section and Undo). Styled with a quiet, dimmer neutral palette than
// needs-draft so it reads as archived/inactive. No App.css class — keeps the
// App.css diff at zero.
const DISMISSED_BADGE_STYLE = { background: '#23262b', color: '#7c8088' };

// Inline animation for the worker-owned "drafting…" indicator. Reuses the
// `pulse` @keyframes ALREADY defined in App.css (opacity/scale breathe) by name
// only — referencing an existing keyframe from an inline `animation` string adds
// ZERO App.css diff (App.css is in-flight WIP / off-limits). Applied to the
// FiClock icon so the row reads as actively being generated by the backend
// worker, not stalled. (The user-triggered drafting indicator stays static.)
const PULSE_ICON_STYLE = { animation: 'pulse 1.4s ease-in-out infinite' };

function statusBadge(status) {
  if (status === 'sent') return { label: 'sent', className: 'badge badge-ok', style: undefined };
  if (status === 'edited') return { label: 'edited', className: 'badge badge-warn', style: undefined };
  if (status === 'needs-draft') return { label: 'needs draft', className: 'badge', style: NEEDS_DRAFT_BADGE_STYLE };
  if (status === 'dismissed') return { label: 'dismissed', className: 'badge', style: DISMISSED_BADGE_STYLE };
  // default / 'needs-review'
  return { label: 'needs review', className: 'badge', style: NEEDS_REVIEW_BADGE_STYLE };
}

export default function SlackPage() {
  const [items, setItems] = useState([]);
  const [etag, setEtag] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [notAvailable, setNotAvailable] = useState(false);
  // When a Slack SCAN last completed (epoch millis), as reported by the backend
  // in GET /api/slack/queue's `lastScanAt` field. This is the freshness signal
  // the "Updated Xs/Xm ago" header label renders from — NOT when the page last
  // fetched. The backstop poll re-reads the queue every ~25s but `lastScanAt`
  // only advances when a real scan completes (periodic worker or manual
  // Refresh), so the label no longer resets to "just now" on a passive re-read.
  // The backend persists it in slack_threads.json so it survives a restart; it
  // is null/absent on a fresh install (never scanned) — the label hides then.
  const [lastScanAt, setLastScanAt] = useState(null);
  // A bare counter bumped on an interval purely to force a re-render so the
  // relative "Updated …" label stays current without a fetch. Value is unused.
  const [, setTick] = useState(0);
  // Transient "a scan is running" hint. POST /refresh wakes the in-process scan
  // worker and returns IMMEDIATELY ({scanning:true}); it does NOT block on the
  // MCP scan. The warmed queue arrives later via the slack_changed broadcast
  // (which the worker fires on EVERY completed scan, including a no-op that found
  // nothing new) → useLiveUpdates clears this hint. We show a brief "Scanning…"
  // label that also auto-clears on a timer so the button never spins for minutes.
  const [scanning, setScanning] = useState(false);
  const scanHintRef = useRef(null);
  // A brief, NON-error muted notice surfaced when a manual Refresh was a no-op
  // because a scan (the periodic worker OR an earlier manual Refresh) is already
  // in progress. The backend returns {scanning:false, reason:"A scan is already
  // in progress."}; we show that server-scrubbed plain-text reason transiently
  // (auto-clears) instead of the misleading "Scanning…" state — it is NOT routed
  // through the red error banner. Rendered only via React {…} interpolation.
  const [scanNotice, setScanNotice] = useState(null);
  const scanNoticeRef = useRef(null);

  const [expandedId, setExpandedId] = useState(null);
  // Whether the per-row "Recent history (3d)" block is expanded. Collapsed by
  // default so the long history never bloats the opened row; reset on every
  // collapse/row-switch in toggleExpand so a freshly-opened row starts folded.
  const [historyOpen, setHistoryOpen] = useState(false);
  // The Sent and Dismissed sections are collapsible and COLLAPSED BY DEFAULT so
  // they never crowd the actionable Needs-review list. Same chevron toggle the
  // "Recent history (3d)" block uses. (Empty sections render nothing regardless.)
  const [sentOpen, setSentOpen] = useState(false);
  const [dismissedOpen, setDismissedOpen] = useState(false);
  // The item whose draft is being actively edited, plus its working text. While
  // an edit is in flight we suppress live-update refetches so we don't clobber
  // the textarea (mirrors CronsPage's `if (!editingId && !showNew)` guard).
  const [editingId, setEditingId] = useState(null);
  const [draftText, setDraftText] = useState('');
  const [busyId, setBusyId] = useState(null); // item mid approve/regenerate/dismiss

  // Ids whose draft is currently being generated (on-demand on expand, or via
  // the explicit Regenerate button). Drives the per-row "drafting…" indicator
  // and the header progress summary. Held as a Set so several can be in flight
  // at once. (A legacy queue item or a regenerate-in-flight can still transiently
  // present needs-draft / drafting state even though the cron now produces fully
  // drafted items, so this state is retained.)
  const [draftingIds, setDraftingIds] = useState(() => new Set());
  // Ids whose most recent draft attempt failed/was unavailable. Surfaces a
  // "draft failed — retry" affordance per row; never a fabricated draft. Cleared
  // when a retry starts.
  const [draftFailedIds, setDraftFailedIds] = useState(() => new Set());
  // Ids whose most recent SEND attempt failed. Map<id, reason string>. Surfaces
  // a per-row "Send failed: <reason> — retry" affordance so the user sees WHY a
  // send didn't go through (instead of a silent no-op). Cleared when a new send
  // attempt starts for that id; only populated on non-OK / available:false / 4xx.
  const [sendFailedIds, setSendFailedIds] = useState(() => new Map());
  // Mirror of draftingIds for synchronous reads (state updates are async, so a
  // ref avoids double-dispatching the same id on the on-demand/regenerate path).
  const draftingRef = useRef(draftingIds);
  draftingRef.current = draftingIds;

  // Seam-health (GET /api/slack/health): whether the delegation subprocess is
  // actually wired. Drives the header badge and gates the per-item on-demand /
  // regenerate draft path (no point spawning subprocesses that will just fail).
  const [health, setHealth] = useState(null);

  // PER-ITEM pending sends — a map keyed by item id:
  //   { [id]: { secondsLeft, capturedText, capturedBaseline } }
  // A pending send is a confirmed-but-not-yet-fired send; the actual approve
  // POST only fires when THAT id's countdown elapses, and per-row Undo cancels
  // only that id. Multiple sends can be pending at once. Switching/leaving a row
  // does NOT cancel a pending send (the per-row Undo is the ONLY cancel) — that
  // fixes the "switch-away-cancels" bug. `capturedText` is the text to send (so
  // a later concurrent edit can't change what goes out); `capturedBaseline` is
  // the item's stored draft at confirm time, forwarded so the backend can match
  // the send by id+content and not 409 on a concurrent queue refresh that didn't
  // touch THIS item.
  const [pendingSends, setPendingSends] = useState({});
  // The per-id timer/interval handles, held in a ref so the unmount cleanup can
  // clear EVERY one (no send after the component is gone):
  //   timersRef.current = { [id]: { timer, interval } }
  const timersRef = useRef({});
  // Synchronous mirror of pendingSends so handlers/cleanup can read the current
  // set without waiting for a state flush.
  const pendingSendsRef = useRef(pendingSends);
  pendingSendsRef.current = pendingSends;
  const anyPending = Object.keys(pendingSends).length > 0;

  const fetchHealth = async () => {
    try {
      const res = await fetch('/api/slack/health');
      if (!res.ok) { setHealth({ ready: false, detail: 'Health check unavailable.' }); return; }
      const json = await res.json();
      setHealth({
        claudeOnPath: json.claudeOnPath === true,
        mcpConfigured: json.mcpConfigured,
        ready: json.ready === true,
        detail: typeof json.detail === 'string' ? json.detail : '',
      });
    } catch {
      setHealth({ ready: false, detail: 'Health check unavailable.' });
    }
  };

  const refresh = async () => {
    try {
      const res = await fetch('/api/slack/queue');
      const json = await res.json();
      const list = Array.isArray(json.items) ? json.items : [];
      setItems(list);
      setEtag(json.etag ?? null);
      // The backend signals an unwired/unreachable Slack fetch with
      // `available: false` — surface that explicitly rather than as "no items".
      setNotAvailable(json.available === false);
      setError(null);
      // SCAN freshness (NOT page-fetch time): the backend reports when a Slack
      // scan last completed via `lastScanAt` (epoch millis, persisted across
      // restarts). Drives the "Updated …" header label. A finite number only;
      // null/undefined (fresh install / never scanned) leaves it null so the
      // label degrades gracefully (hidden) instead of crashing relativeTime.
      setLastScanAt(finiteOrNull(json.lastScanAt));
    } catch {
      setError('Failed to load the Slack queue.');
    }
  };

  useEffect(() => { refresh(); fetchHealth(); }, []);

  // Live refresh, but never while a draft is open in the editor — that would
  // discard the in-progress edit.
  useLiveUpdates(['slack_changed', 'slack_deleted'], () => {
    // A broadcast means a scan completed (the worker fires slack_changed on
    // EVERY completed scan now — including a no-op that found nothing new), so
    // the transient "Scanning…" hint clears here even when zero new messages
    // arrived. refresh() below also re-reads `lastScanAt`, advancing the
    // "Updated …" label to the just-finished scan.
    clearRef(scanHintRef);
    setScanning(false);
    if (!editingId) refresh();
  });

  // Clear ONLY one id's timers (fire timer + countdown interval), without
  // touching the rendered map. Internal helper used by both cancel and fire.
  const clearTimersFor = (id) => {
    const handles = timersRef.current[id];
    if (handles) {
      if (handles.timer) clearTimeout(handles.timer);
      if (handles.interval) clearInterval(handles.interval);
      delete timersRef.current[id];
    }
  };

  // Cancel the pending (confirmed-but-not-yet-fired) send for ONE id: clears
  // that id's fire timer + countdown interval so NO approve POST is made, and
  // removes ONLY that id from the map. Per-row Undo calls this. Safe to call
  // when nothing is pending for the id.
  const cancelPendingSend = (id) => {
    clearTimersFor(id);
    setPendingSends(prev => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };

  // Clear EVERY outstanding send-timer on unmount so no confirmed-but-undone (or
  // simply navigated-away-from) send fires after the component is gone. Iterates
  // the whole per-id timer map.
  useEffect(() => () => {
    Object.values(timersRef.current).forEach(({ timer, interval }) => {
      if (timer) clearTimeout(timer);
      if (interval) clearInterval(interval);
    });
    timersRef.current = {};
    clearRef(scanHintRef);
    clearRef(scanNoticeRef);
  }, []);

  // DECISION: proactive drafting is owned by a BACKEND asyncio worker now — it
  // polls slack_threads.json, picks up skeleton `needs-draft` items (and
  // `needs-review` items flagged `needsRedraft`), drafts them in parallel via
  // capped claude --print subprocesses, then flips them to `needs-review` with a
  // draft. The frontend therefore does NO proactive drafting:
  //   - The old in-process "needs-draft drain" useEffect was already removed.
  //   - The on-expand auto-draft (toggleExpand used to POST /{id}/refresh for an
  //     undrafted row) is ALSO removed — with the worker on every needs-draft
  //     item, auto-drafting on expand spawned a SECOND redundant subprocess
  //     racing the worker. Opening such a row now just shows a passive
  //     "drafting… (auto)" indicator; the worker's result arrives via the
  //     slack_changed broadcast.
  // The per-item Generate/Retry button (POST /{id}/refresh — unchanged on the
  // backend) is kept as a MANUAL escape hatch so a user can still force a draft
  // if the worker is wedged. draftingIds/draftFailedIds track only those
  // user-triggered attempts; the needs-draft badge + the passive worker
  // indicator cover the worker-owned state.

  // Read-only backstop poll of GET /api/slack/queue. The scan worker / cron
  // writes the queue file, so the primary repaint is the slack_changed broadcast
  // (see useLiveUpdates above); this poll is the BACKSTOP that catches a missed
  // broadcast so a fresh scan write surfaces within QUEUE_POLL_INTERVAL_MS without
  // a manual Refresh. It calls ONLY refresh() (a cheap GET that updates only on
  // success; it reads `lastScanAt` but does NOT itself advance scan freshness —
  // only a real scan does) — it NEVER POSTs /refresh, NEVER triggers a scan, and
  // NEVER calls generateDraft. SUPPRESSED while a draft is
  // open in the editor (editingId) or ANY send is pending (anyPending) — the
  // same suppression the slack_changed handler uses — so an in-progress edit or
  // any undo window is never clobbered. PAUSED while the tab is hidden
  // (document.hidden), and a visibilitychange listener re-evaluates so polling
  // effectively stops when hidden and resumes on show.
  useEffect(() => {
    if (editingId || anyPending) return undefined;
    const tick = () => {
      if (document.hidden) return;        // paused while tab is hidden
      if (editingId || anyPending) return; // never clobber an edit / pending send
      refresh();                          // read-only GET /queue
    };
    const id = setInterval(tick, QUEUE_POLL_INTERVAL_MS);
    // Re-evaluate promptly when the tab becomes visible again (the interval
    // simply no-ops while hidden; this gives an immediate refresh on show).
    const onVisible = () => { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingId, anyPending]);

  // Tick the "Updated Xs/Xm ago" label so it stays current WITHOUT a fetch.
  // Pure re-render trigger — no network. Cleared on unmount.
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), SCAN_LABEL_TICK_MS);
    return () => clearInterval(id);
  }, []);

  // Manual Refresh triggers a REAL Slack scan and returns immediately. POST
  // /refresh wakes the in-process scan worker (the same `_scan_once` work the
  // periodic worker runs) and returns NON-BLOCKING — it never awaits the MCP scan.
  // The worker takes a shared in-progress guard, so the response tells us which
  // happened:
  //   - {scanning:true}  — a scan was just started. Show the brief "Scanning…"
  //     hint; the warmed queue (and the cleared hint) arrive when the scan
  //     completes via the slack_changed broadcast → useLiveUpdates → refresh().
  //     The worker broadcasts on EVERY completed scan, including a no-op that
  //     found nothing new, so the hint always clears.
  //   - {scanning:false, reason} — a scan (the periodic worker OR an earlier
  //     manual Refresh) is ALREADY running, so this Refresh was a no-op. We do
  //     NOT show the misleading "Scanning…" state; instead we surface the
  //     server-scrubbed plain-text reason as a brief, NON-error muted notice
  //     (auto-clears). Manual and system scans never overlap.
  // Either way we re-read the warm queue instantly below so the user sees the
  // current persisted state right away. Seam-unavailability still surfaces via
  // the GET /queue available:false path and the health badge, re-checked here.
  const refreshQueue = async () => {
    setRefreshing(true);
    setError(null);
    setScanNotice(null);
    clearRef(scanNoticeRef);
    let startedScan = false;
    let alreadyRunningReason = null;
    try {
      const res = await fetch('/api/slack/queue/refresh', { method: 'POST' });
      // A scan-already-running answer is NOT an HTTP error — the backend returns
      // 200/202 with {scanning:false, reason}. Only a genuine non-2xx is an error.
      if (res.status < 200 || res.status >= 300) {
        setError('Failed to request a Slack scan.');
        return;
      }
      const json = await res.json().catch(() => null);
      if (json && json.scanning === false) {
        // A scan is already in progress — this Refresh did nothing destructive.
        alreadyRunningReason = (typeof json.reason === 'string' && json.reason)
          ? json.reason : 'A scan is already in progress.';
      } else {
        // {scanning:true} (or a backend that omits the flag) — a scan started.
        startedScan = true;
      }
    } catch {
      setError('Failed to request a Slack scan.');
      return;
    } finally {
      setRefreshing(false);
      fetchHealth();
      // Re-read the current warm queue right away (cheap, instant) so the user
      // sees the latest persisted state immediately; the live scan repaints it
      // again when it lands.
      refresh();
    }
    if (alreadyRunningReason) {
      // Brief, non-error muted notice — never the "Scanning…" state, never the
      // red error banner. Plain text, rendered via React {…} interpolation.
      setScanNotice(alreadyRunningReason);
      clearRef(scanNoticeRef);
      scanNoticeRef.current = setTimeout(() => { setScanNotice(null); scanNoticeRef.current = null; }, 4000);
      return;
    }
    if (!startedScan) return;
    // Brief "Scanning…" hint. The real repaint AND the primary hint-clear come
    // from the slack_changed broadcast when the scan completes (even a no-op
    // scan broadcasts); this timeout is only a safety net so a missed broadcast
    // can't leave the hint stuck. Generous so a slower MCP scan still reads as
    // "Scanning…" until it lands.
    setScanning(true);
    clearRef(scanHintRef);
    scanHintRef.current = setTimeout(() => { setScanning(false); scanHintRef.current = null; }, 30000);
  };

  // Expand/collapse a row. Opening a row seeds the draft editor with the item's
  // current draft so editing is inline; collapsing discards the working buffer.
  const toggleExpand = (item) => {
    // Switching to another row or collapsing the current one must NOT cancel an
    // in-flight pending send (that was the old "switch-away-cancels" bug). A
    // pending send keeps its OWN per-id countdown and fires on its own timer; the
    // per-row Undo button is the ONLY way to cancel a send. We deliberately do
    // NOT call cancelPendingSend() here.
    if (expandedId === item.id) {
      setExpandedId(null);
      setEditingId(null);
      setDraftText('');
      setHistoryOpen(false);
    } else {
      setExpandedId(item.id);
      setEditingId(null);
      setDraftText(item.draft || '');
      setHistoryOpen(false);
      setError(null);
      // NO auto-draft on expand. The backend worker already drafts every
      // needs-draft item, so POSTing /{id}/refresh here would spawn a SECOND
      // redundant subprocess racing the worker. Opening an undrafted row just
      // shows a passive "drafting… (auto)" indicator (see renderDetailPanel);
      // the worker's draft lands via the slack_changed broadcast. The explicit
      // Generate/Retry button in the detail panel remains the manual escape
      // hatch if the worker is wedged.
    }
  };

  // Persist an edited draft. etag-guarded; a 409 means the queue changed under
  // us — surface a conflict banner and refetch so the user re-reviews.
  const saveDraft = async (id) => {
    try {
      const res = await fetch(`/api/slack/queue/${encodeURIComponent(id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft: draftText, etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing…');
        setEditingId(null);
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to save the draft.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag ?? null);
      setEditingId(null);
      setError(null);
      refresh();
    } catch {
      setError('Failed to save the draft.');
    }
  };

  // POST one approve. Forwards `draft` (the captured text to send) AND
  // `expectedDraft` (the item's stored draft at confirm time) so the backend can
  // match the send by id+CONTENT rather than the whole-file etag — a concurrent
  // */3 cron / 5s watcher refresh that did NOT touch THIS item must not 409 the
  // send. We still forward `etag` so the backend can adopt it on the happy path.
  // Returns a structured outcome: {status, json}. NEVER fires before the undo
  // window has elapsed (it is only ever called from fireSend's elapsed timer).
  const postApprove = async (id, text, baseline) => {
    const res = await fetch(`/api/slack/queue/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // `draft` is the text to send; `expectedDraft`/`baseText` is the item's
      // own draft at confirm time (the id+content baseline the backend matches).
      body: JSON.stringify({ draft: text, expectedDraft: baseline, baseText: baseline, etag }),
    });
    let json = null;
    try { json = await res.json(); } catch { json = null; }
    return { status: res.status, json };
  };

  // The actual outward-facing send. Fired ONLY when THIS id's undo window
  // elapses — never directly from a click. Clears ONLY this id's timers + map
  // entry. The backend's already-sent → 409 guard remains the single source of
  // truth against double-sends. `text` is captured at confirm time so a
  // concurrent edit can't change what goes out; `baseline` is the item's stored
  // draft at confirm time (forwarded so the backend matches by id+content).
  //
  // STALE-ETAG RECOVERY: the */3 cron and the 5s watcher refresh the queue (and
  // its etag) underneath the ~9s undo window, so a legitimate unedited send could
  // 409 on a now-stale whole-file etag. On a 409 we AUTO-RETRY ONCE: re-read the
  // queue and, if THIS item's own draft is unchanged (the send was lost to an
  // unrelated refresh, not a real edit), retry the send. We only surface a
  // conflict banner if the item itself actually changed (or the retry still 409s).
  const fireSend = async (id, text, baseline) => {
    clearTimersFor(id);
    setPendingSends(prev => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setBusyId(id);
    setError(null);
    // Clear any prior send-failure for this id so a retry starts fresh.
    setSendFailedIds(prev => {
      if (!prev.has(id)) return prev;
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
    try {
      let { status, json } = await postApprove(id, text, baseline);
      if (status === 409) {
        // Re-read the queue to learn THIS item's current draft. If the item's own
        // content is unchanged, the 409 was a stale whole-file etag from an
        // unrelated concurrent refresh — adopt the fresh etag and retry once.
        let fresh = null;
        try {
          const r = await fetch('/api/slack/queue');
          const fj = await r.json();
          const list = Array.isArray(fj.items) ? fj.items : [];
          setItems(list);
          setEtag(fj.etag ?? null);
          setNotAvailable(fj.available === false);
          setLastScanAt(finiteOrNull(fj.lastScanAt));
          fresh = list.find(it => it.id === id) || null;
        } catch {
          fresh = null;
        }
        if (fresh && fresh.status === 'sent') {
          // Already sent (double-send guard / a prior fire landed). Not an error.
          setEditingId(null);
          refresh();
          return;
        }
        const freshDraft = fresh ? (fresh.draft || '') : null;
        const unchanged = fresh !== null && freshDraft === baseline;
        if (unchanged) {
          // Item itself unchanged — the send was lost to an unrelated refresh.
          // Retry ONCE with the now-current etag (set above) and the same text.
          const retry = await postApprove(id, text, baseline);
          status = retry.status;
          json = retry.json;
        } else {
          // The item ACTUALLY changed underneath us (an edit / regenerate /
          // someone sent it) — do not silently resend stale text; surface it.
          setError('Conflict: this item changed before the send. Re-review and approve again.');
          return;
        }
      }
      if (status === 409) {
        // Still conflicting after the single retry — surface it.
        setError('Conflict: the queue was modified elsewhere before the send. Re-review and approve again.');
        refresh();
        return;
      }
      if (status < 200 || status >= 300) {
        const reason = (json && json.reason) || 'Send failed';
        setSendFailedIds(prev => new Map(prev).set(id, reason));
        setError('Failed to send the reply.');
        return;
      }
      if (json && json.etag) setEtag(json.etag);
      setEditingId(null);
      refresh();
    } catch {
      setSendFailedIds(prev => new Map(prev).set(id, 'Network error — check connection'));
      setError('Failed to send the reply.');
    } finally {
      setBusyId(null);
    }
  };

  // Approve & send. Sending a Slack message is outward-facing and hard to
  // reverse, so it is gated behind an explicit per-item confirm AND a brief
  // cancellable undo window — there is NO bulk/auto-send path. After confirm we
  // do NOT POST immediately; we start a PER-ITEM countdown and only fire when it
  // elapses. Per-row Undo clears that id's timer so no send is made. Multiple
  // sends can be pending at once (a second click on row B while row A counts down
  // starts B too — we do NOT early-return). The final (possibly-edited) draft
  // text is captured now and sent; the item's stored draft at confirm time is
  // captured as the backend's id+content match baseline.
  const approveAndSend = (id) => {
    // Capture what goes out. If THIS row is being edited, send the working
    // buffer; otherwise send the item's stored draft. The baseline is the item's
    // stored draft (the backend's id+content match key) regardless of edits.
    const item = items.find(it => it.id === id);
    const baseline = item ? (item.draft || '') : '';
    const text = (editingId === id) ? draftText : baseline;
    setError(null);
    // Clear any prior timers for this id (e.g. a fresh re-confirm) before arming.
    clearTimersFor(id);
    setPendingSends(prev => ({ ...prev, [id]: { secondsLeft: UNDO_WINDOW_S, capturedText: text, capturedBaseline: baseline } }));
    const interval = setInterval(() => {
      setPendingSends(prev => {
        const cur = prev[id];
        if (!cur) return prev;
        return { ...prev, [id]: { ...cur, secondsLeft: Math.max(0, cur.secondsLeft - 1) } };
      });
    }, 1000);
    const timer = setTimeout(() => fireSend(id, text, baseline), UNDO_WINDOW_S * 1000);
    timersRef.current[id] = { timer, interval };
  };

  // Generate (or regenerate) a single item's draft via the per-item seam call
  // POST /api/slack/queue/{id}/refresh — the SAME path used for both the
  // on-demand draft (expanding a legacy needs-draft row) and the explicit
  // Regenerate button. These are USER-TRIGGERED and remain after the drain was
  // removed. The backend flips needs-draft → needs-review on success and is
  // fail-loud per item (502 / available:false). One short request per item;
  // never a giant call, never /approve — this can NOT send.
  //
  // Per-item fail-loud: on failure the row is left in needs-draft and added to
  // draftFailedIds so a "draft failed — retry" affordance shows; we never
  // fabricate a draft. `quiet` suppresses the error banner (the row's own retry
  // affordance is the signal); the explicit Regenerate button passes quiet=false
  // so the user sees why their click did nothing.
  const generateDraft = async (id, { quiet = false } = {}) => {
    if (draftingRef.current.has(id)) return; // already in flight
    setDraftingIds(prev => { const n = new Set(prev); n.add(id); return n; });
    setDraftFailedIds(prev => {
      if (!prev.has(id)) return prev;
      const n = new Set(prev); n.delete(id); return n;
    });
    if (!quiet) setError(null);
    let ok = false;
    try {
      const res = await fetch(`/api/slack/queue/${encodeURIComponent(id)}/refresh`, { method: 'POST' });
      if (res.ok) {
        const json = await res.json().catch(() => null);
        // The seam returns HTTP 200 with {available:false} when the per-item
        // draft delegation was unavailable — treat that as a failure, not a
        // silent no-op, so the retry affordance appears.
        ok = !(json && json.available === false);
        if (!ok && !quiet) {
          setError(`Draft unavailable: ${(json && json.reason) || 'no reason given'}`);
        }
      } else if (!quiet) {
        setError('Failed to generate the draft.');
      }
    } catch {
      if (!quiet) setError('Failed to generate the draft.');
    } finally {
      setDraftingIds(prev => { const n = new Set(prev); n.delete(id); return n; });
      if (!ok) setDraftFailedIds(prev => { const n = new Set(prev); n.add(id); return n; });
    }
    if (ok) {
      // The backend broadcasts slack_changed → useLiveUpdates → refresh(), but
      // refetch directly too so the flip to needs-review is immediate even if the
      // ws message is delayed.
      await refresh();
    }
  };

  // The explicit Regenerate button: same per-item path, but it also clears any
  // in-progress edit (the user asked for a fresh draft) and surfaces failures
  // loudly.
  const regenerate = async (id) => {
    setEditingId(null);
    await generateDraft(id, { quiet: false });
  };

  // Dismiss SOFT-dismisses the item: the backend sets status='dismissed' and
  // PRESERVES the item (it is no longer a hard delete) and broadcasts
  // 'slack_changed', so the row moves into the collapsible Dismissed section and
  // can be restored with Undo. The DELETE verb + body are unchanged on the wire
  // (the backend's dismiss handler reinterprets it as a soft state flip).
  const dismiss = async (id) => {
    cancelPendingSend(id); // a pending send for this row is no longer valid
    setBusyId(id);
    try {
      const res = await fetch(`/api/slack/queue/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing…');
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to dismiss the item.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag ?? null);
      if (expandedId === id) { setExpandedId(null); setEditingId(null); }
      setError(null);
      refresh();
    } catch {
      setError('Failed to dismiss the item.');
    } finally {
      setBusyId(null);
    }
  };

  // Undo a soft-dismiss: POST /api/slack/queue/{id}/undismiss restores a
  // 'dismissed' item back to needs-review (or 'edited' if its draft differs from
  // the generated baseline — the backend decides). Etag-guarded like the other
  // mutators; on success the item moves back into the Needs-review section.
  const undismiss = async (id) => {
    setBusyId(id);
    try {
      const res = await fetch(`/api/slack/queue/${encodeURIComponent(id)}/undismiss`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing…');
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to restore the item.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag ?? null);
      setError(null);
      refresh();
    } catch {
      setError('Failed to restore the item.');
    } finally {
      setBusyId(null);
    }
  };

  // --- Detail panel: full thread context + editable draft + per-item actions -
  const renderDetailPanel = (item) => {
    const sectionStyle = { marginBottom: 'var(--space-md)' };
    const labelStyle = { fontSize: '0.72em', fontWeight: 600, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '0.3em' };
    // Link-style toggle, matching CronsPage's collapsible-section affordance.
    const toggleStyle = {
      display: 'inline-flex', alignItems: 'center', gap: '0.35em',
      cursor: 'pointer', color: 'var(--accent)', fontSize: '0.8em', background: 'none',
      border: 'none', padding: 0,
    };
    const isBusy = busyId === item.id;
    const isSent = item.status === 'sent';
    // A soft-dismissed item: preserved history, never sent. Read-only like sent
    // (show its draft) but with an Undo affordance to restore it. Treated as a
    // terminal/inactive row — no editing, no draft generation, no send.
    const isDismissed = item.status === 'dismissed';
    const isReadOnly = isSent || isDismissed;
    // `drafting` = a USER-triggered Generate/Retry is in flight (id in
    // draftingIds). `workerDrafting` = the BACKEND worker is presumably on it —
    // an undrafted needs-draft skeleton with no user-triggered attempt running.
    // The two render differently: a static "drafting…" for the explicit click,
    // a pulsing "drafting… (auto)" for the worker (it lands via slack_changed).
    const drafting = draftingIds.has(item.id);
    const draftFailed = draftFailedIds.has(item.id);
    const hasDraft = (item.draft || '').trim() !== '';
    // A row that's listed-but-not-yet-drafted with no draft on hand. Its draft
    // area shows a drafting indicator / Generate button instead of an editable
    // box, and Approve & Send is withheld (can't approve empty text).
    const undrafted = !isReadOnly && !hasDraft;
    const workerDrafting = undrafted && item.status === 'needs-draft' && !drafting && !draftFailed;
    // A needs-review item the cron flagged for re-draft (new messages arrived in
    // an existing conversation): the backend worker is about to re-generate the
    // reply addressing all unanswered messages. Read-only & non-blocking — the
    // existing draft stays editable/approvable; we just surface a subtle
    // "updating…" hint. Tolerate the field being absent on older items.
    const updating = !isReadOnly && hasDraft && item.status === 'needs-review' && item.needsRedraft === true;
    const editing = editingId === item.id;
    const dirty = editing && draftText !== (item.draft || '');
    // The pending (confirmed-but-not-yet-fired) send for THIS row, if any —
    // looked up from the per-id map.
    const pending = pendingSends[item.id] || null;

    // The text the user is currently looking at (working buffer once editing,
    // otherwise the stored draft) vs the originally-generated baseline. When
    // they differ we show a read-only diff — this is exactly the edit the
    // backend distills into a style/topic preference signal on approve.
    const currentText = editing ? draftText : (item.draft || '');
    const generated = item.generatedDraft || '';
    const showDiff = !isReadOnly && generated.trim() !== '' && generated.trim() !== currentText.trim();
    const diffTokens = showDiff ? wordDiff(generated, currentText) : null;

    // Thread context: the backend stores `threadContext` as a single scrubbed
    // string. We also tolerate a structured array of {sender, text, ts} turns
    // for forward-compat. All text is EXTERNAL user content — rendered via
    // React's default {…} interpolation, which escapes it (never
    // dangerouslySetInnerHTML).
    const thread = Array.isArray(item.threadContext) ? item.threadContext : [];
    const threadText = typeof item.threadContext === 'string' ? item.threadContext : '';

    // Last-3-days chat history: an ordered (oldest→newest) array of
    // {ts (epoch millis), author, text}. Distinct from the short threadContext
    // summary above — this is the full recent conversation, shown only on an
    // explicit expand so it never bloats the row. Read defensively: older items
    // predate the field and must render with no history block (never crash).
    // All text is EXTERNAL user content — rendered via React's {…} interpolation
    // (escaped), never dangerouslySetInnerHTML.
    const history = Array.isArray(item.history3d) ? item.history3d : [];

    return (
      <div style={{ padding: 'var(--space-md)', background: 'var(--surface2)', borderRadius: 'var(--radius)' }}>
        {/* --- Thread context ------------------------------------------------ */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Thread context</div>
          {thread.length === 0 && !threadText ? (
            <div style={{ color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
              No thread context was captured for this item.
            </div>
          ) : thread.length > 0 ? (
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              {thread.map((turn, i) => (
                <div
                  key={i}
                  style={{
                    padding: '0.6em 0.9em',
                    borderBottom: i < thread.length - 1 ? '1px solid var(--border)' : 'none',
                    fontSize: '0.85em',
                  }}
                >
                  <div style={{ display: 'flex', gap: '0.6em', alignItems: 'baseline', flexWrap: 'wrap' }}>
                    <span style={{ fontWeight: 600, color: 'var(--text)' }}>{turn.sender || 'unknown'}</span>
                    {turn.ts && <span style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTime(turn.ts)}</span>}
                  </div>
                  <div style={{ marginTop: '0.25em', color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {stripSlackXml(turn.text)}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: 'var(--text)', fontSize: '0.85em', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {threadText}
            </div>
          )}
        </div>

        {/* --- Recent history (3d) ------------------------------------------- */}
        {/* The full last-3-days conversation, distinct from the short summary
            above. Collapsed by default (opt-in expand) so it never bloats the
            row. Rendered only when there is history to show; older items
            without the field simply omit this block. */}
        {history.length > 0 && (
          <div style={sectionStyle}>
            <button
              type="button"
              style={toggleStyle}
              onClick={() => setHistoryOpen(v => !v)}
            >
              {historyOpen ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
              {historyOpen ? 'Hide recent history (3d)' : 'Show recent history (3d)'}
            </button>
            {historyOpen && (
              <div className="card" style={{ padding: 0, overflow: 'hidden', marginTop: '0.4em' }}>
                {history.map((msg, i) => (
                  <div
                    key={i}
                    style={{
                      padding: '0.6em 0.9em',
                      borderBottom: i < history.length - 1 ? '1px solid var(--border)' : 'none',
                      fontSize: '0.85em',
                    }}
                  >
                    <div style={{ display: 'flex', gap: '0.6em', alignItems: 'baseline', flexWrap: 'wrap' }}>
                      <span style={{ fontWeight: 600, color: 'var(--text)' }}>{msg.author || 'unknown'}</span>
                      {msg.ts && <span style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTime(msg.ts)}</span>}
                    </div>
                    <div style={{ marginTop: '0.25em', color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {stripSlackXml(msg.text)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* --- Draft reply --------------------------------------------------- */}
        <div style={sectionStyle}>
          <div style={labelStyle}>
            {isSent ? 'Sent reply' : isDismissed ? 'Dismissed draft' : 'Draft reply'}
          </div>
          {/* New messages arrived — the worker is regenerating this reply to
              cover them. Non-blocking: the current draft below stays usable. */}
          {updating && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', color: 'var(--muted)', fontSize: '0.8em', fontStyle: 'italic', marginBottom: '0.4em' }}>
              <FiClock size={12} style={PULSE_ICON_STYLE} /> New messages arrived — updating this draft…
            </div>
          )}
          {isSent ? (
            // A sent item is read-only history — show the final text that went
            // out (defensively: finalText, falling back to draft), never an
            // editable box that could re-send, plus when it was sent.
            <div>
              <div style={{ color: 'var(--text)', fontSize: '0.9em', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.5 }}>
                {item.finalText || item.draft || '(no text recorded)'}
              </div>
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', marginTop: '0.5em', color: 'var(--muted)', fontSize: '0.8em' }}>
                <FiCheckCircle size={12} /> Sent {relativeTime(item.sentAt ?? item.ts)}
              </div>
            </div>
          ) : isDismissed ? (
            // A soft-dismissed item is read-only history — show the draft that
            // was never sent. Restore it via Undo (in the Actions section below).
            <div style={{ color: 'var(--text)', fontSize: '0.9em', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.5 }}>
              {item.draft || '(no draft recorded)'}
            </div>
          ) : undrafted ? (
            // Listed but not yet drafted. Three sub-states, NEVER a fabricated
            // draft — the box only fills when a real draft lands:
            //   1. workerDrafting — the backend worker is generating it: a
            //      passive pulsing "drafting… (auto)" indicator + a small manual
            //      "Generate now" escape hatch (in case the worker is wedged).
            //   2. drafting — a USER-triggered Generate/Retry is in flight: a
            //      static "drafting…" indicator.
            //   3. otherwise — a Generate-draft button (with a retry note if a
            //      prior manual attempt failed).
            workerDrafting ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45em' }}>
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
                  <FiClock size={13} style={PULSE_ICON_STYLE} /> drafting… <span style={{ opacity: 0.8 }}>(auto)</span>
                </div>
                <div style={{ color: 'var(--muted)', fontSize: '0.78em' }}>
                  A reply is being generated in the background — it will appear here shortly.
                </div>
                <button
                  className="btn"
                  disabled={isBusy}
                  onClick={() => generateDraft(item.id, { quiet: false })}
                  title="Force a draft now (the background worker is normally already on this)"
                  style={{ alignSelf: 'flex-start', fontSize: '0.85em', padding: '0.2em 0.5em' }}
                >
                  <FiRotateCw size={12} /> Generate now
                </button>
              </div>
            ) : drafting ? (
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
                <FiClock size={13} /> drafting…
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5em' }}>
                {draftFailed && (
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.82em' }}>
                    <FiAlertCircle size={13} /> The draft could not be generated. No reply was fabricated — retry below.
                  </div>
                )}
                <button
                  className="btn"
                  disabled={isBusy}
                  onClick={() => generateDraft(item.id, { quiet: false })}
                  title="Generate a draft reply for this item"
                  style={{ alignSelf: 'flex-start' }}
                >
                  <FiRotateCw size={13} /> {draftFailed ? 'Retry draft' : 'Generate draft'}
                </button>
              </div>
            )
          ) : (
            <textarea
              className="form-textarea"
              value={editing ? draftText : (item.draft || '')}
              onChange={e => { setEditingId(item.id); setDraftText(e.target.value); }}
              placeholder="The drafted reply will appear here. Edit it before approving — your edits teach the style preferences."
              style={{ width: '100%' }}
            />
          )}
        </div>

        {/* --- Changes since the generated draft (read-only diff) ------------ */}
        {showDiff && (
          <div style={sectionStyle}>
            <div style={labelStyle}>Changes since the generated draft</div>
            <div
              className="card"
              style={{
                padding: '0.7em 0.9em', fontSize: '0.85em', lineHeight: 1.6,
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text)',
              }}
            >
              {diffTokens.map((tok, i) => {
                if (tok.type === 'same') return <span key={i}>{tok.value}</span>;
                if (tok.type === 'removed') {
                  return (
                    <span
                      key={i}
                      style={{ color: 'var(--muted)', textDecoration: 'line-through', textDecorationColor: 'var(--muted)' }}
                    >
                      {tok.value}
                    </span>
                  );
                }
                return <span key={i} style={{ color: 'var(--accent)', fontWeight: 600 }}>{tok.value}</span>;
              })}
            </div>
            <div style={{ display: 'flex', gap: '1em', marginTop: '0.4em', fontSize: '0.75em', color: 'var(--muted)' }}>
              <span><span style={{ textDecoration: 'line-through' }}>generated</span> removed</span>
              <span><span style={{ color: 'var(--accent)' }}>your edit</span> added</span>
            </div>
          </div>
        )}

        {/* --- Actions ------------------------------------------------------- */}
        {/* A soft-dismissed row exposes ONLY Undo (restore to needs-review). It
            was never sent, so there is nothing to re-send or re-edit here. */}
        {isDismissed && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn" disabled={isBusy} onClick={() => undismiss(item.id)} title="Restore this item to the review queue">
              <FiRotateCw size={13} /> Undo dismiss
            </button>
          </div>
        )}
        {/* An undrafted row exposes only Dismiss — there's no draft to approve,
            save or diff yet, and Approve & Send must never offer to send empty
            text. The Generate-draft button lives in the Draft-reply section. */}
        {undrafted && !isReadOnly && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn btn-danger" disabled={isBusy} onClick={() => dismiss(item.id)} title="Dismiss without replying">
              <FiTrash2 size={13} /> Dismiss
            </button>
          </div>
        )}
        {!isReadOnly && !undrafted && (
          pending ? (
            // Pending undo window — the send is queued but has NOT left yet.
            // Undo cancels ONLY this row's send (no approve POST). This is the
            // only way to cancel a confirmed-but-not-yet-fired send.
            <div style={{ display: 'flex', gap: '0.6em', flexWrap: 'wrap', alignItems: 'center' }}>
              <span
                className="badge"
                style={{ ...NEEDS_REVIEW_BADGE_STYLE, display: 'inline-flex', alignItems: 'center', gap: '0.35em' }}
              >
                <FiClock size={12} /> Sending in {pending.secondsLeft}s…
              </span>
              <button className="btn" onClick={() => cancelPendingSend(item.id)} title="Cancel — do not send this reply">
                <FiXCircle size={13} /> Undo ({pending.secondsLeft}s)
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
              {dirty && (
                <button className="btn" disabled={isBusy} onClick={() => saveDraft(item.id)} title="Save the edited draft">
                  <FiSave size={13} /> Save edit
                </button>
              )}
              <button
                className="btn btn-primary"
                disabled={isBusy}
                onClick={() => approveAndSend(item.id)}
                title="Approve and send this reply on Slack (with a brief undo window)"
              >
                <FiSend size={13} /> Approve &amp; Send
              </button>
              <button className="btn" disabled={isBusy} onClick={() => regenerate(item.id)} title="Regenerate this draft">
                <FiRotateCw size={13} /> Regenerate
              </button>
              <button className="btn btn-danger" disabled={isBusy} onClick={() => dismiss(item.id)} title="Dismiss without replying">
                <FiTrash2 size={13} /> Dismiss
              </button>
              {dirty && (
                <span style={{ color: 'var(--muted)', fontSize: '0.8em' }}>
                  Unsaved edits — Approve &amp; Send will use the edited text.
                </span>
              )}
            </div>
          )
        )}

        {/* Per-row send-failed affordance — shown when the most recent send for
            THIS item failed (non-OK / available:false / network error). Mirrors
            the draftFailed pattern: inline icon + reason + retry button. Cleared
            on the next send attempt for this id. All reason text is external —
            rendered via React {…} interpolation (never dangerouslySetInnerHTML). */}
        {sendFailedIds.has(item.id) && (
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.82em', marginTop: 'var(--space-sm)' }}>
            <FiAlertCircle size={13} />
            <span>Send failed: {sendFailedIds.get(item.id)}</span>
            <button
              className="btn"
              style={{ fontSize: '0.85em', padding: '0.2em 0.5em' }}
              onClick={() => approveAndSend(item.id)}
              title="Retry sending this reply"
            >
              Retry
            </button>
          </div>
        )}
      </div>
    );
  };

  // Render one section's rows as a <table>. Shared by all three sections so the
  // row markup (expand chevron, source label, snippet, status badge + drafting
  // hints, and the expanded detail panel) stays identical everywhere.
  const renderTable = (list) => (
    <table className="data-table">
      <thead>
        <tr>
          <th>Source</th>
          <th>From</th>
          <th>Message</th>
          <th style={{ textAlign: 'center' }}>When</th>
          <th style={{ textAlign: 'center' }}>Status</th>
          <th style={{ textAlign: 'center' }}>Action</th>
        </tr>
      </thead>
      <tbody>
        {list.map(item => {
          const badge = statusBadge(item.status);
          const open = expandedId === item.id;
          const rowHasDraft = (item.draft || '').trim() !== '';
          const rowWorkerDrafting = item.status === 'needs-draft' && !rowHasDraft
            && !draftingIds.has(item.id) && !draftFailedIds.has(item.id);
          const rowUpdating = item.status === 'needs-review' && rowHasDraft && item.needsRedraft === true;
          return (
            <Fragment key={item.id}>
              <tr onClick={() => toggleExpand(item)} style={{ cursor: 'pointer' }}>
                <td>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
                    <span style={{ color: 'var(--muted)' }}>
                      {open ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                    </span>
                    <span style={{ color: 'var(--muted)' }}>{sourceLabel(item)}</span>
                  </span>
                </td>
                <td>{item.sender || 'unknown'}</td>
                <td title={stripSlackXml(item.snippet)}>{truncate(stripSlackXml(item.snippet), 80)}</td>
                <td style={{ color: 'var(--muted)', fontSize: '0.85em', textAlign: 'center' }}>{relativeTime(item.ts)}</td>
                <td style={{ textAlign: 'center' }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', flexWrap: 'wrap', justifyContent: 'center' }}>
                    <span className={badge.className} style={badge.style}>{badge.label}</span>
                    {pendingSends[item.id] && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em' }} title="A send is queued — open the row to undo.">
                        <FiClock size={11} /> sending in {pendingSends[item.id].secondsLeft}s…
                      </span>
                    )}
                    {draftingIds.has(item.id) && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }}>
                        <FiClock size={11} /> drafting…
                      </span>
                    )}
                    {rowWorkerDrafting && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }} title="A reply is being generated in the background — it will appear shortly.">
                        <FiClock size={11} style={PULSE_ICON_STYLE} /> drafting… (auto)
                      </span>
                    )}
                    {rowUpdating && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }} title="New messages arrived — the draft is being updated in the background.">
                        <FiClock size={11} style={PULSE_ICON_STYLE} /> updating…
                      </span>
                    )}
                    {!draftingIds.has(item.id) && draftFailedIds.has(item.id) && item.status === 'needs-draft' && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em' }} title="The draft could not be generated — open the row to retry.">
                        <FiAlertCircle size={11} /> draft failed
                      </span>
                    )}
                    {sendFailedIds.has(item.id) && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em' }} title={sendFailedIds.get(item.id)}>
                        <FiAlertCircle size={11} /> send failed
                      </span>
                    )}
                  </span>
                </td>
                <td style={{ textAlign: 'center' }}>
                  {item.status !== 'sent' && item.status !== 'dismissed' && (
                    <button
                      className="btn"
                      style={{ fontSize: '0.72em', padding: '0.15em 0.4em', lineHeight: 1 }}
                      title="Dismiss"
                      onClick={(e) => { e.stopPropagation(); dismiss(item.id); }}
                    >
                      <FiTrash2 size={11} />
                    </button>
                  )}
                  {item.status === 'dismissed' && (
                    <button
                      className="btn"
                      style={{ fontSize: '0.72em', padding: '0.15em 0.4em', lineHeight: 1 }}
                      title="Undo dismiss"
                      onClick={(e) => { e.stopPropagation(); undismiss(item.id); }}
                    >
                      <FiRotateCw size={11} />
                    </button>
                  )}
                </td>
              </tr>
              {open && (
                <tr>
                  <td colSpan={6} style={{ padding: 0, background: 'var(--surface2)' }}>
                    {renderDetailPanel(item)}
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );

  // A collapsible section header (chevron + label + count), reusing the same
  // link-styled toggle the "Recent history (3d)" block uses. `open`/`onToggle`
  // drive the disclosure; the section renders nothing when its list is empty.
  const collapsibleStyle = {
    display: 'inline-flex', alignItems: 'center', gap: '0.4em',
    cursor: 'pointer', color: 'var(--accent)', fontSize: '0.85em', background: 'none',
    border: 'none', padding: 0, fontWeight: 600,
  };

  // Partition the queue purely client-side. Needs-review = the actionable rows
  // (needs-draft / needs-review / edited). Sent / Dismissed are terminal states
  // shown in their own collapsible sections.
  const needsReviewItems = items.filter(isActionable);
  const sentItems = items.filter(it => it.status === 'sent');
  const dismissedItems = items.filter(it => it.status === 'dismissed');

  // Sub-partition actionable items by channel type.
  const dmItems = needsReviewItems.filter(it => it.channelType === 'dm');
  const groupDmItems = needsReviewItems.filter(it => it.channelType === 'group_dm');
  const channelItems = needsReviewItems.filter(it => it.channelType !== 'dm' && it.channelType !== 'group_dm');

  // Dismiss all items in a list (sequential, best-effort per item).
  const dismissAll = async (list) => {
    for (const item of list) {
      if (item.status === 'sent') continue;
      try {
        const res = await fetch(`/api/slack/queue/${encodeURIComponent(item.id)}`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ etag }),
        });
        if (res.ok) {
          const json = await res.json();
          setEtag(json.etag ?? null);
        }
      } catch { /* best-effort */ }
    }
    refresh();
  };

  return (
    <div>
      <div className="page-header">
        <h2>Slack</h2>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.8em' }}>
          {/* "Updated …" indicator — when a Slack SCAN last completed (from the
              backend's `lastScanAt`), NOT when the page last fetched. The ~25s
              backstop poll re-reads the queue without scanning, so this label
              does NOT reset to "just now" on a passive re-read; it advances only
              when a real scan (periodic worker or manual Refresh) finishes. Ticks
              on its own so the relative text stays current without a fetch.
              GRACEFUL DEGRADATION: hidden entirely until the first scan has been
              recorded (lastScanAt null on a fresh install) — never crashes
              relativeTime. Subtle muted text. */}
          {lastScanAt !== null && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', color: 'var(--muted)', fontSize: '0.82em' }}>
              <FiClock size={12} /> Updated {relativeTime(lastScanAt)}
            </span>
          )}
          <button className="btn btn-primary" onClick={refreshQueue} disabled={refreshing}>
            <FiRefreshCw size={14} /> {scanning ? 'Scanning…' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Brief, NON-error notice when a Refresh was a no-op because a scan
          (the periodic worker or an earlier manual Refresh) is already running.
          Muted (NOT the red error banner) and auto-clearing. The reason is
          server-scrubbed plain text, rendered via React {…} interpolation. */}
      {scanNotice && (
        <div
          style={{
            display: 'inline-flex', alignItems: 'center', gap: '0.4em',
            color: 'var(--muted)', fontSize: '0.85em', marginBottom: 'var(--space-md)',
          }}
        >
          <FiClock size={13} /> {scanNotice}
        </div>
      )}

      {/* Seam-health badge: makes an available:false refresh explainable rather
          than a silent empty list. `detail` is server-scrubbed text (never
          credentials) and rendered as plain DOM text. */}
      {health && (
        health.ready ? (
          <div
            className="badge badge-ok"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', marginBottom: 'var(--space-md)' }}
          >
            <FiCheckCircle size={13} /> Slack delegation ready
          </div>
        ) : (
          <div
            className="badge badge-warn"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', marginBottom: 'var(--space-md)', maxWidth: '100%' }}
            title={health.detail || ''}
          >
            <FiAlertCircle size={13} /> not wired{health.detail ? `: ${health.detail}` : ''}
          </div>
        )
      )}

      {/* Drafting-progress summary so the page never looks dead. SCOPED to the
          Needs-review (actionable) items so the counts stay sensible now that
          Sent/Dismissed live in their own sections. With the backend worker
          owning proactive drafting, an undrafted needs-draft skeleton is being
          drafted (by the worker, or by a user-triggered Generate) UNLESS its
          last attempt failed — so: 'drafting' = worker-owned + user-triggered
          in flight; 'need draft' = only those whose draft FAILED and no attempt
          is running (a genuine manual-nudge backlog). The 'N sending…' hint
          reflects any in-flight per-item pending sends. Plain React text. */}
      {(needsReviewItems.length > 0 || anyPending) && (() => {
        const needDraft = needsReviewItems.filter(it => it.status === 'needs-draft' && !(it.draft || '').trim());
        // Genuinely stuck: a manual attempt failed and none is running.
        const failed = needDraft.filter(it => draftFailedIds.has(it.id) && !draftingIds.has(it.id)).length;
        // Everything else undrafted is being worked (worker or user-triggered).
        const drafting = needDraft.length - failed;
        const drafted = needsReviewItems.length - needDraft.length;
        const sendingCount = Object.keys(pendingSends).length;
        return (
          <div style={{ color: 'var(--muted)', fontSize: '0.85em', marginBottom: 'var(--space-md)' }}>
            {needsReviewItems.length} to review · {drafted} drafted
            {drafting > 0 ? ` · ${drafting} drafting…` : ''}
            {failed > 0 ? ` · ${failed} need draft` : ''}
            {sendingCount > 0 ? ` · ${sendingCount} sending…` : ''}
          </div>
        );
      })()}

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {/* --- Needs review (actionable) ------------------------------------- */}
      {needsReviewItems.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            {notAvailable ? (
              <>
                <h3>Slack fetch not available</h3>
                <p>
                  The Slack queue could not be fetched (the Slack integration is not wired up
                  or returned nothing). Try Refresh — no items are shown until real data arrives.
                </p>
              </>
            ) : (
              <>
                <h3>No items to review</h3>
                <p>There are no unread DMs or @mentions needing a reply. Use Refresh to check again.</p>
              </>
            )}
          </div>
        </div>
      ) : (
        <>
          {dmItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>DMs ({dmItems.length})</span>
                <button className="btn" style={{ fontSize: '0.78em', padding: '0.2em 0.5em' }} onClick={() => dismissAll(dmItems)}>
                  <FiTrash2 size={11} /> Dismiss all
                </button>
              </div>
              <div className="card">{renderTable(dmItems)}</div>
            </div>
          )}
          {groupDmItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>Group DMs ({groupDmItems.length})</span>
                <button className="btn" style={{ fontSize: '0.78em', padding: '0.2em 0.5em' }} onClick={() => dismissAll(groupDmItems)}>
                  <FiTrash2 size={11} /> Dismiss all
                </button>
              </div>
              <div className="card">{renderTable(groupDmItems)}</div>
            </div>
          )}
          {channelItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>Channels ({channelItems.length})</span>
                <button className="btn" style={{ fontSize: '0.78em', padding: '0.2em 0.5em' }} onClick={() => dismissAll(channelItems)}>
                  <FiTrash2 size={11} /> Dismiss all
                </button>
              </div>
              <div className="card">{renderTable(channelItems)}</div>
            </div>
          )}
        </>
      )}

      {/* --- Sent (collapsible, collapsed by default) ---------------------- */}
      {/* Empty section renders nothing — never a blank/broken block. */}
      {sentItems.length > 0 && (
        <div style={{ marginTop: 'var(--space-md)' }}>
          <button type="button" style={collapsibleStyle} onClick={() => setSentOpen(v => !v)}>
            {sentOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
            Sent ({sentItems.length})
          </button>
          {sentOpen && (
            <div className="card" style={{ marginTop: '0.5em' }}>
              {renderTable(sentItems)}
            </div>
          )}
        </div>
      )}

      {/* --- Dismissed (collapsible, collapsed by default) ----------------- */}
      {dismissedItems.length > 0 && (
        <div style={{ marginTop: 'var(--space-md)' }}>
          <button type="button" style={collapsibleStyle} onClick={() => setDismissedOpen(v => !v)}>
            {dismissedOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
            Dismissed ({dismissedItems.length})
          </button>
          {dismissedOpen && (
            <div className="card" style={{ marginTop: '0.5em' }}>
              {renderTable(dismissedItems)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
