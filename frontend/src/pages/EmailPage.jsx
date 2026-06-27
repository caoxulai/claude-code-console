import { Fragment, useEffect, useRef, useState } from 'react';
import {
  FiRefreshCw, FiRotateCw, FiTrash2, FiTrash, FiSave, FiChevronDown, FiChevronRight,
  FiCheckCircle, FiAlertCircle, FiClock, FiBellOff, FiPause, FiPlay,
  FiClipboard, FiFeather, FiPlus, FiX,
} from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
import { ErrorBoundary } from '../components/ErrorBoundary';
// Single source of truth for COUNTING/GROUPING (kept in lockstep with the NavBar
// bubble and server/routes/email.py — see D-035). The page no longer re-defines
// these inline so the page sections and the bubble can never drift.
import { isActionable, reviewGroup, countActionable } from '../lib/emailQueue';
// Pure decision helpers shared with emailDetail.test.mjs (no jsdom/vitest).
import { latestThreadView, threadSummaryParts, fromColumnLabel, sourceFolderLabel, mutedLabel, sortedMutedKeys, polishSource, autoSizeHeight, displayTimestamp, deleteOutcome, autoFormatBody, seedRecipients } from './emailDetail';

// --- Constants ---

const QUEUE_POLL_INTERVAL_MS = 25000;
const SCAN_LABEL_TICK_MS = 20000;

// --- Utility functions ---

function finiteOrNull(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

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

function clearRef(ref) {
  if (ref.current) {
    clearTimeout(ref.current);
    ref.current = null;
  }
}

// Word-level diff (LCS). Returns array of {value, type} tokens.
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

// --- Badge styles (inline, no App.css changes) ---

const NEEDS_REVIEW_BADGE_STYLE = { background: '#1a2a3a', color: '#7ab8e6' };
const NEEDS_DRAFT_BADGE_STYLE = { background: '#262b31', color: '#9aa3ad' };
const DISMISSED_BADGE_STYLE = { background: '#23262b', color: '#7c8088' };
const DELETED_BADGE_STYLE = { background: '#2b1a1a', color: '#e67a7a' };
const FYI_BADGE_STYLE = { background: '#26262e', color: '#9a9ab0' };
const NEEDS_CLASSIFY_BADGE_STYLE = { background: '#2b2730', color: '#a39aad' };
const APPROVED_BADGE_STYLE = { background: '#1a2e1a', color: '#7ae67a' };
const PULSE_ICON_STYLE = { animation: 'pulse 1.4s ease-in-out infinite' };
// Source-folder badge (D-058 §5): a small neutral metadata chip on each row that
// names where the item was ingested from ("Inbox", "1 GSD", ...). Neutral
// surface/border/muted-text tokens so it reads as quiet provenance metadata, not
// a status the user must act on (it sits next to the status badge but must not
// compete with it). Sized down so the From cell stays scannable.
const SOURCE_FOLDER_BADGE_STYLE = {
  display: 'inline-flex', alignItems: 'center',
  background: 'var(--surface2)', color: 'var(--muted)',
  border: '1px solid var(--border)', borderRadius: 'var(--radius)',
  fontSize: '0.72em', padding: '0.05em 0.4em', whiteSpace: 'nowrap',
  fontWeight: 500, lineHeight: 1.4,
};

function statusBadge(item) {
  const status = typeof item === 'string' ? item : (item && item.status);
  const classification = typeof item === 'string' ? '' : (item && item.classification);
  if (status === 'approved') return { label: 'approved', className: 'badge', style: APPROVED_BADGE_STYLE };
  if (status === 'edited') return { label: 'edited', className: 'badge badge-warn', style: undefined };
  if (status === 'dismissed') return { label: 'dismissed', className: 'badge', style: DISMISSED_BADGE_STYLE };
  if (status === 'deleted') return { label: 'deleted', className: 'badge', style: DELETED_BADGE_STYLE };
  if (status === 'needs-classify') return { label: 'classifying', className: 'badge', style: NEEDS_CLASSIFY_BADGE_STYLE };
  if (classification === 'fyi') return { label: 'fyi', className: 'badge', style: FYI_BADGE_STYLE };
  if (status === 'needs-draft') return { label: 'needs draft', className: 'badge', style: NEEDS_DRAFT_BADGE_STYLE };
  return { label: 'needs review', className: 'badge', style: NEEDS_REVIEW_BADGE_STYLE };
}

export default function EmailPage() {
  const [items, setItems] = useState([]);
  const [etag, setEtag] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [notAvailable, setNotAvailable] = useState(false);
  const [lastScanAt, setLastScanAt] = useState(null);
  const [paused, setPaused] = useState(false);
  const [pausing, setPausing] = useState(false);
  // GRASP quota / Microsoft Graph rate-limit state, read from GET /api/email/queue.
  // graphThrottled gates an honest page-level banner instead of a falsely-calm
  // "No items to review"; it CLEARS as soon as the backend reports it false again
  // (anti-STICKY-THROTTLE), so the banner never lingers past recovery.
  const [graphThrottled, setGraphThrottled] = useState(false);
  const [lastGraphError, setLastGraphError] = useState(null);
  // Per-item delete failures: id -> human reason. A delete that the backend could
  // not confirm (429/quota or any error) records its reason here so the row stays
  // VISIBLE with an inline error instead of vanishing as if deleted. Cleared when
  // the next delete attempt starts or the item confirms deleted.
  const [deleteFailed, setDeleteFailed] = useState(() => new Map());
  const [, setTick] = useState(0);
  const [scanning, setScanning] = useState(false);
  const scanHintRef = useRef(null);
  const [scanNotice, setScanNotice] = useState(null);
  const scanNoticeRef = useRef(null);

  const [expandedId, setExpandedId] = useState(null);
  const [emailBodyOpen, setEmailBodyOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [draftText, setDraftText] = useState('');
  const [busyId, setBusyId] = useState(null);
  const [polishingId, setPolishingId] = useState(null); // item mid fluency-polish

  // Editable reply-all recipients for the currently-expanded row (Feature #3).
  // Seeded when a row expands: from the item's PERSISTED toRecipients/ccRecipients
  // (if a prior save recorded them as a plain email list) else from the
  // replyAllRecipients(item) default (sender + original To minus me; original CC
  // minus me plus me; de-duped). Held as plain email-string arrays so the chips,
  // the save-draft PUT, and Approve all share one shape. recipientsDirty tracks an
  // unsaved recipient edit so the Save/Approve flow persists it. recipientInput is
  // the per-field "add" text box value.
  const [recipientsTo, setRecipientsTo] = useState([]);
  const [recipientsCc, setRecipientsCc] = useState([]);
  const [recipientsDirty, setRecipientsDirty] = useState(false);
  const [toInput, setToInput] = useState('');
  const [ccInput, setCcInput] = useState('');

  // The draft <textarea>, auto-sized to its content so a short reply isn't framed
  // by a tall empty box. autoSizeDraft() shrinks-then-grows to scrollHeight
  // (capped); it runs on edit (in the textarea's onChange) AND on programmatic
  // text changes (row expand / Polish) via the effect below.
  const draftRef = useRef(null);
  const autoSizeDraft = () => {
    const ta = draftRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = autoSizeHeight(ta.scrollHeight, 320) + 'px';
  };

  // Muted-conversations management. GET /api/email/muted returns
  // { muteKey: unixSeconds }; shown in its own collapsible section so the user can
  // review and unmute. Best-effort — a failed fetch just leaves the section empty.
  const [muted, setMuted] = useState({});
  const [unmutingKey, setUnmutingKey] = useState(null);
  const [mutedOpen, setMutedOpen] = useState(false);

  const [draftingIds, setDraftingIds] = useState(() => new Set());
  const [draftFailedIds, setDraftFailedIds] = useState(() => new Set());
  const draftingRef = useRef(draftingIds);
  draftingRef.current = draftingIds;

  const [health, setHealth] = useState(null);

  // Approve confirmation message per item (transient)
  const [approveMsg, setApproveMsg] = useState({});
  const approveMsgRef = useRef({});

  // Transient "Copied" flag for the subject copy button (per item), so the user
  // can paste the subject into Outlook search. Clears after a short timer.
  const [subjectCopied, setSubjectCopied] = useState(null);
  const subjectCopiedRef = useRef(null);
  const copySubject = async (id, subject) => {
    try {
      await navigator.clipboard.writeText(subject || '');
    } catch {
      // Clipboard can fail in a non-secure context; nothing else to do.
    }
    setSubjectCopied(id);
    if (subjectCopiedRef.current) clearTimeout(subjectCopiedRef.current);
    subjectCopiedRef.current = setTimeout(() => setSubjectCopied(null), 1500);
  };

  // Collapsible sections
  const [approvedOpen, setApprovedOpen] = useState(false);
  const [dismissedOpen, setDismissedOpen] = useState(false);
  const [deletedOpen, setDeletedOpen] = useState(false);
  const [fyiOpen, setFyiOpen] = useState(true);

  // Style memory section
  const [styleOpen, setStyleOpen] = useState(false);
  const [styleContent, setStyleContent] = useState('');
  const [styleEtag, setStyleEtag] = useState(null);
  const [styleLoaded, setStyleLoaded] = useState(false);
  const [styleSaving, setStyleSaving] = useState(false);
  const [styleFeedback, setStyleFeedback] = useState(null);
  const styleFeedbackRef = useRef(null);

  const fetchStyle = async () => {
    try {
      const res = await fetch('/api/email/style');
      if (!res.ok) return;
      const json = await res.json();
      setStyleContent(typeof json.content === 'string' ? json.content : '');
      setStyleEtag(json.etag ?? null);
      setStyleLoaded(true);
    } catch {
      // Degrade silently — the section just won't show content
    }
  };

  const saveStyle = async () => {
    setStyleSaving(true);
    setStyleFeedback(null);
    clearRef(styleFeedbackRef);
    try {
      const res = await fetch('/api/email/style', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: styleContent, etag: styleEtag }),
      });
      if (res.status === 409) {
        const json = await res.json().catch(() => null);
        if (json && typeof json.current === 'string') {
          setStyleContent(json.current);
          setStyleEtag(json.etag ?? null);
        }
        setStyleFeedback({ type: 'error', text: 'Conflict — content was updated elsewhere. Showing current version.' });
        return;
      }
      if (!res.ok) {
        setStyleFeedback({ type: 'error', text: 'Failed to save style preferences.' });
        return;
      }
      const json = await res.json();
      setStyleEtag(json.etag ?? null);
      if (typeof json.content === 'string') setStyleContent(json.content);
      setStyleFeedback({ type: 'ok', text: 'Saved.' });
      styleFeedbackRef.current = setTimeout(() => { setStyleFeedback(null); styleFeedbackRef.current = null; }, 3000);
    } catch {
      setStyleFeedback({ type: 'error', text: 'Failed to save style preferences.' });
    } finally {
      setStyleSaving(false);
    }
  };

  const fetchHealth = async () => {
    try {
      const res = await fetch('/api/email/health');
      if (!res.ok) { setHealth({ ready: false, detail: 'Health check unavailable.' }); return; }
      const json = await res.json();
      // Graph (manager-outlook-mcp) read path needs Node 24. The backend reports
      // it separately as graphNodeReady/graphNodeReason; fold graphNodeReady into
      // overall readiness so a missing Node 24 shows the actionable "not wired"
      // banner (with the install reason) instead of a green badge over an empty
      // queue that looks like "inbox is empty".
      const ready = json.ready === true && json.graphNodeReady !== false;
      setHealth({
        ready,
        detail: typeof json.graphNodeReason === 'string' ? json.graphNodeReason : '',
      });
    } catch {
      setHealth({ ready: false, detail: 'Health check unavailable.' });
    }
  };

  const refresh = async () => {
    try {
      const res = await fetch('/api/email/queue');
      const json = await res.json();
      const list = Array.isArray(json.items) ? json.items : [];
      setItems(list);
      setEtag(json.etag ?? null);
      setNotAvailable(json.available === false);
      setError(null);
      setLastScanAt(finiteOrNull(json.lastScanAt));
      setPaused(json.paused === true);
      // Read the GRASP-quota / Graph-throttle flag every refresh so the banner
      // CLEARS the moment the backend reports recovery (graphThrottled false) —
      // never sticky. lastGraphError is a human string for the banner detail.
      setGraphThrottled(json.graphThrottled === true);
      setLastGraphError(typeof json.lastGraphError === 'string' && json.lastGraphError.trim()
        ? json.lastGraphError.trim() : null);
    } catch {
      setError('Failed to load the email queue.');
    }
  };

  // Load the muted-conversations map for the management section. Best-effort: a
  // failure just leaves the section empty rather than blocking the page.
  const refreshMuted = async () => {
    try {
      const res = await fetch('/api/email/muted');
      const json = await res.json().catch(() => null);
      setMuted(json && json.muted && typeof json.muted === 'object' ? json.muted : {});
    } catch {
      /* best-effort — leave whatever we had */
    }
  };

  useEffect(() => { refresh(); fetchHealth(); fetchStyle(); refreshMuted(); }, []);

  useLiveUpdates(['email_changed'], () => {
    clearRef(scanHintRef);
    setScanning(false);
    if (!editingId) refresh();
    // A scan-driven mute/unmute or a 30-day prune can change the muted map; keep
    // the management section current. Cheap GET; safe to run even while editing.
    refreshMuted();
  });

  // Cleanup on unmount
  useEffect(() => () => {
    clearRef(scanHintRef);
    clearRef(scanNoticeRef);
    clearRef(styleFeedbackRef);
    Object.values(approveMsgRef.current).forEach(t => clearTimeout(t));
  }, []);

  // Backstop poll
  useEffect(() => {
    if (editingId) return undefined;
    const tick = () => {
      if (document.hidden) return;
      if (editingId) return;
      refresh();
    };
    const id = setInterval(tick, QUEUE_POLL_INTERVAL_MS);
    const onVisible = () => { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingId]);

  // Tick the scan-freshness label
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), SCAN_LABEL_TICK_MS);
    return () => clearInterval(id);
  }, []);

  // Auto-size the draft textarea whenever the row it belongs to opens or its
  // displayed text changes by a NON-typing path — expanding a row, or Polish
  // replacing the text. (Typing is sized inline in the textarea's onChange.) The
  // textarea only exists while a row is expanded, so this no-ops otherwise.
  useEffect(() => {
    autoSizeDraft();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandedId, editingId, draftText, items]);

  // Manual refresh
  const refreshQueue = async () => {
    setRefreshing(true);
    setError(null);
    setScanNotice(null);
    clearRef(scanNoticeRef);
    let startedScan = false;
    let alreadyRunningReason = null;
    try {
      const res = await fetch('/api/email/queue/refresh', { method: 'POST' });
      if (res.status < 200 || res.status >= 300) {
        setError('Failed to request an email scan.');
        return;
      }
      const json = await res.json().catch(() => null);
      if (json && json.scanning === false) {
        alreadyRunningReason = (typeof json.reason === 'string' && json.reason)
          ? json.reason : 'A scan is already in progress.';
      } else {
        startedScan = true;
      }
    } catch {
      setError('Failed to request an email scan.');
      return;
    } finally {
      setRefreshing(false);
      fetchHealth();
      refresh();
    }
    if (alreadyRunningReason) {
      setScanNotice(alreadyRunningReason);
      clearRef(scanNoticeRef);
      scanNoticeRef.current = setTimeout(() => { setScanNotice(null); scanNoticeRef.current = null; }, 4000);
      return;
    }
    if (!startedScan) return;
    setScanning(true);
    clearRef(scanHintRef);
    scanHintRef.current = setTimeout(() => { setScanning(false); scanHintRef.current = null; }, 30000);
  };

  // Toggle pause
  const togglePaused = async () => {
    const next = !paused;
    setPausing(true);
    setError(null);
    try {
      const res = await fetch('/api/email/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: next, etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
        refresh();
        return;
      }
      if (!res.ok) {
        setError(next ? 'Failed to pause email scanning.' : 'Failed to resume email scanning.');
        return;
      }
      const json = await res.json().catch(() => null);
      if (json && json.etag) setEtag(json.etag);
      setPaused(json && typeof json.paused === 'boolean' ? json.paused : next);
    } catch {
      setError(next ? 'Failed to pause email scanning.' : 'Failed to resume email scanning.');
    } finally {
      setPausing(false);
    }
  };

  // Seed the reply-all editor from an item: the user's persisted edit if the
  // backend flipped recipientsEdited, else the computed reply-all default. Shared
  // by toggleExpand and the on-demand FYI generate path so they stay in sync.
  const seedRecipientEditor = (item) => {
    const seeded = seedRecipients(item);
    setRecipientsTo(seeded.to);
    setRecipientsCc(seeded.cc);
    setRecipientsDirty(false);
    setToInput('');
    setCcInput('');
  };

  // Expand/collapse a row
  const toggleExpand = (item) => {
    if (expandedId === item.id) {
      setExpandedId(null);
      setEditingId(null);
      setDraftText('');
      setEmailBodyOpen(false);
      setRecipientsTo([]);
      setRecipientsCc([]);
      setRecipientsDirty(false);
      setToInput('');
      setCcInput('');
    } else {
      setExpandedId(item.id);
      setEditingId(null);
      setDraftText(item.draft || '');
      setEmailBodyOpen(false);
      setError(null);
      seedRecipientEditor(item);
    }
  };

  // Add a recipient to a reply-all list (To or CC). Basic shape validation +
  // case-insensitive de-dupe — never fabricate, never double an existing address.
  const RECIPIENT_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const addRecipient = (field) => {
    const setter = field === 'cc' ? setRecipientsCc : setRecipientsTo;
    const list = field === 'cc' ? recipientsCc : recipientsTo;
    const raw = (field === 'cc' ? ccInput : toInput).trim();
    if (!raw) return;
    if (!RECIPIENT_RE.test(raw)) {
      setError(`"${raw}" doesn't look like an email address.`);
      return;
    }
    if (list.some(e => e.toLowerCase() === raw.toLowerCase())) {
      // Already present (case-insensitive) — just clear the box.
      (field === 'cc' ? setCcInput : setToInput)('');
      return;
    }
    setter([...list, raw]);
    setRecipientsDirty(true);
    (field === 'cc' ? setCcInput : setToInput)('');
    setError(null);
  };

  const removeRecipient = (field, email) => {
    const setter = field === 'cc' ? setRecipientsCc : setRecipientsTo;
    const list = field === 'cc' ? recipientsCc : recipientsTo;
    setter(list.filter(e => e.toLowerCase() !== email.toLowerCase()));
    setRecipientsDirty(true);
  };

  // Save edited draft. When the user changed the reply-all recipients we send the
  // resolved To/CC email arrays alongside the draft so they persist (the backend
  // flips recipientsEdited and uses them on approve — D-056 #3). A draft-only save
  // omits the recipient keys so it never wipes captured/edited recipients.
  const saveDraft = async (id) => {
    try {
      const payload = { draft: draftText, etag };
      if (recipientsDirty) {
        payload.toRecipients = recipientsTo;
        payload.ccRecipients = recipientsCc;
      }
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
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
      setRecipientsDirty(false);
      setError(null);
      refresh();
    } catch {
      setError('Failed to save the draft.');
    }
  };

  // Approve flow: save draft if edited, then POST approve, copy to clipboard
  const approve = async (id) => {
    setBusyId(id);
    setError(null);
    try {
      const item = items.find(it => it.id === id);
      const text = (editingId === id) ? draftText : (item ? (item.draft || '') : '');

      // If the draft OR the reply-all recipients were edited, persist first so
      // approve reads the latest values (the backend resolves To/CC from the
      // persisted item — D-056 #3). We send the recipient arrays only when the
      // user actually changed them, so a draft-only approve never rewrites them.
      const draftDirty = editingId === id && draftText !== (item ? (item.draft || '') : '');
      if (draftDirty || recipientsDirty) {
        const savePayload = { draft: text, etag };
        if (recipientsDirty) {
          savePayload.toRecipients = recipientsTo;
          savePayload.ccRecipients = recipientsCc;
        }
        const saveRes = await fetch(`/api/email/queue/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(savePayload),
        });
        if (saveRes.status === 409) {
          setError('Conflict: the queue was modified elsewhere. Refreshing...');
          setEditingId(null);
          refresh();
          return;
        }
        if (!saveRes.ok) {
          setError('Failed to save the edited draft before approving.');
          return;
        }
        const saveJson = await saveRes.json();
        setEtag(saveJson.etag ?? null);
        setRecipientsDirty(false);
      }

      // POST approve
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft: text, etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to approve the draft.');
        return;
      }
      const json = await res.json();
      if (json && json.etag) setEtag(json.etag);

      // Copy to clipboard
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Clipboard may fail in non-secure contexts; the draft is still saved
      }

      // Show confirmation message
      const draftSaved = json && json.draftSaved === true;
      const msg = draftSaved ? 'Copied! Draft saved to Outlook.' : 'Copied!';
      setApproveMsg(prev => ({ ...prev, [id]: msg }));
      // Clear after a timer
      if (approveMsgRef.current[id]) clearTimeout(approveMsgRef.current[id]);
      approveMsgRef.current[id] = setTimeout(() => {
        setApproveMsg(prev => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        delete approveMsgRef.current[id];
      }, 5000);

      setEditingId(null);
      refresh();
    } catch {
      setError('Failed to approve the draft.');
    } finally {
      setBusyId(null);
    }
  };

  // Regenerate draft
  const generateDraft = async (id, { quiet = false } = {}) => {
    if (draftingRef.current.has(id)) return;
    setDraftingIds(prev => { const n = new Set(prev); n.add(id); return n; });
    setDraftFailedIds(prev => {
      if (!prev.has(id)) return prev;
      const n = new Set(prev); n.delete(id); return n;
    });
    if (!quiet) setError(null);
    let ok = false;
    try {
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}/refresh`, { method: 'POST' });
      if (res.ok) {
        const json = await res.json().catch(() => null);
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
    if (ok) await refresh();
  };

  const regenerate = async (id) => {
    setEditingId(null);
    await generateDraft(id, { quiet: false });
  };

  // "Polish": take the text CURRENTLY in this row's box, ask the stateless,
  // MCP-free /api/email/polish seam to improve its fluency while keeping the
  // user's own voice and language, and drop the result back into the box as an
  // UNSAVED edit (we set editingId + draftText, never touching the stored draft
  // or sending anything). It runs on whatever the user is looking at — their own
  // edits if editing, otherwise the stored draft. Fail-loud: on any failure we
  // surface the banner and leave the text exactly as it was (never fabricate).
  const polishDraft = async (id) => {
    const item = items.find(it => it.id === id);
    if (!item) return;
    const { ok, text } = polishSource(id, editingId, draftText, item);
    if (!ok) {
      setError('Nothing to polish yet — write or generate a draft first.');
      return;
    }
    setPolishingId(id);
    setError(null);
    try {
      const res = await fetch('/api/email/polish', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      const json = await res.json().catch(() => null);
      if (!res.ok || !json || json.available === false) {
        setError(`Couldn't polish the draft: ${(json && json.reason) || 'unavailable'}`);
        return;
      }
      // Land the polished text as an editable, unsaved edit so the existing diff
      // vs. the generated baseline + Save + Approve flow handle it unchanged.
      setEditingId(id);
      setDraftText(json.draft || '');
    } catch {
      setError("Couldn't polish the draft.");
    } finally {
      setPolishingId(null);
    }
  };

  // Dismiss
  const dismiss = async (id) => {
    setBusyId(id);
    try {
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
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

  // Delete from Outlook (moves to Deleted Items — distinct from dismiss).
  //
  // HONEST delete (Fix task 6): the Outlook move is fire-and-forget at the HTTP
  // envelope — the backend can answer 200/ok:true while the move later 429s on
  // the exhausted GRASP quota. So we DO NOT key "deleted" on res.ok / non-409;
  // deleteOutcome (emailDetail.js) keys it on the response BODY's `deleted` flag
  // (the verify-effect-not-caller principle). On a confirmed delete we collapse
  // the row (it re-renders in the Deleted section). On failure we leave the row
  // VISIBLE in its prior state and attach an inline per-item error so the user
  // sees WHY (e.g. GRASP quota) instead of the row silently vanishing.
  const deleteEmail = async (id) => {
    setBusyId(id);
    // Clear any prior delete error for this item before retrying.
    setDeleteFailed(prev => {
      if (!prev.has(id)) return prev;
      const n = new Map(prev); n.delete(id); return n;
    });
    try {
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      const json = await res.json().catch(() => null);
      const { outcome, etag: nextEtag, reason } = deleteOutcome(res.status, json);
      if (nextEtag) setEtag(nextEtag);

      if (outcome === 'conflict') {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
        refresh();
        return;
      }
      if (outcome === 'deleted') {
        // Confirmed: the email actually moved to Deleted Items. Collapse the row
        // (it reappears under the Deleted section) and clear any stale error.
        // (deleteFailed for this id was already cleared at the top of the call.)
        if (expandedId === id) { setExpandedId(null); setEditingId(null); }
        setError(null);
        refresh();
        return;
      }
      // outcome === 'failed': the move did not confirm (429/quota or an error).
      // Keep the row exactly where it is — do NOT collapse the panel as if it
      // were deleted — and surface the reason inline next to the Delete button.
      setDeleteFailed(prev => new Map(prev).set(id, reason));
      // Re-read so the row reflects its true (still-actionable) state. The
      // backend leaves the item in its prior status, so the row won't flicker
      // to "deleted" and reappear — it simply stays put with the inline error.
      refresh();
    } catch {
      setDeleteFailed(prev => new Map(prev).set(
        id, "Couldn't delete — the request did not complete. The email was NOT removed."));
    } finally {
      setBusyId(null);
    }
  };

  // Undismiss
  const undismiss = async (id) => {
    setBusyId(id);
    try {
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}/undismiss`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
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

  // Mute conversation
  const mute = async (id) => {
    setBusyId(id);
    try {
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}/mute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (res.status === 409) {
        setError('Conflict: the queue was modified elsewhere. Refreshing...');
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to mute the conversation.');
        return;
      }
      const json = await res.json().catch(() => null);
      if (json && json.etag) setEtag(json.etag);
      if (expandedId === id) { setExpandedId(null); setEditingId(null); }
      setError(null);
      refresh();
      refreshMuted(); // keep the Muted-conversations section in sync
    } catch {
      setError('Failed to mute the conversation.');
    } finally {
      setBusyId(null);
    }
  };

  // Unmute one conversation: DELETE /api/email/muted/{key}. Un-muting only stops
  // future scans from skipping the conversation — it does NOT resurrect any
  // dismissed item (a genuinely new message earns a fresh skeleton). 409 →
  // re-read the fresh etag & retry once, mirroring mute (a scan can bump the
  // shared file's etag between our load and the click).
  const unmute = async (key) => {
    setUnmutingKey(key);
    const del = (withEtag) =>
      fetch(`/api/email/muted/${encodeURIComponent(key)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag: withEtag }),
      });
    try {
      let res = await del(etag);
      if (res.status === 409) {
        const fresh = await fetch('/api/email/queue').then(r => r.json()).catch(() => null);
        if (fresh && fresh.etag) {
          setEtag(fresh.etag);
          res = await del(fresh.etag);
        }
      }
      const json = await res.json().catch(() => null);
      if (json && json.etag) setEtag(json.etag);
      if (!res.ok) {
        // 404 → the key was already gone (aged out / unmuted elsewhere); just
        // resync the list rather than alarm the user.
        if (res.status !== 404) {
          setError(`Couldn't unmute that conversation (HTTP ${res.status}).`);
        }
      } else {
        setError(null);
      }
      refreshMuted();
    } catch {
      setError("Couldn't unmute that conversation — the request did not complete.");
    } finally {
      setUnmutingKey(null);
    }
  };

  // Dismiss every item in a declared group (sequential soft-DELETE per item,
  // best-effort). Skips terminal items, adopts the fresh etag after each, and
  // refreshes once at the end. Each dismiss is a recoverable soft state flip
  // (Undo from the Dismissed section). NEVER sends.
  //
  // Etag handling: each successful write bumps the queue's etag, so we thread a
  // LOCAL `cur` through the loop and advance it from every response. setEtag()
  // alone is a next-render update invisible to the running loop, so relying on
  // it would send a stale etag for items #2..N and the backend
  // (`expected_etag or current_etag`) would 409 them instead of falling back to
  // current — silently leaving most of the group undismissed. On a 409 (etag
  // raced by something outside this loop) we re-read the fresh queue etag and
  // retry that one item, so EVERY actionable item ends up dismissed.
  const dismissAll = async (list) => {
    let cur = etag;
    for (const item of list) {
      if (item.status === 'approved' || item.status === 'dismissed') continue;
      const del = (withEtag) =>
        fetch(`/api/email/queue/${encodeURIComponent(item.id)}`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ etag: withEtag }),
        });
      try {
        let res = await del(cur);
        if (res.status === 409) {
          const fresh = await fetch('/api/email/queue').then(r => r.json()).catch(() => null);
          if (fresh && fresh.etag) {
            cur = fresh.etag;
            res = await del(cur);
          }
        }
        if (res.ok) {
          const json = await res.json().catch(() => null);
          if (json && json.etag) cur = json.etag;
        }
      } catch { /* best-effort */ }
    }
    setEtag(cur ?? null);
    refresh();
  };

  // "Delete all (N)" for a group header — moves EVERY item in the group to
  // Outlook's Deleted Items (the real, honest delete; distinct from the
  // recoverable soft-dismiss above). Irreversible from this app, so it confirms
  // first. Mirrors deleteEmail's verify-effect-not-caller contract per item:
  // success is keyed on the response BODY's `deleted` flag (not res.ok), and a
  // per-item failure (429/GRASP quota or error) is surfaced inline on that row
  // (setDeleteFailed) and the item is LEFT in place — never silently vanished.
  // Etag is chained item-to-item like dismissAll, with a fresh re-read on 409.
  const deleteAll = async (list) => {
    // Skip only items already in Outlook's Deleted Items (re-deleting is a no-op).
    // Dismissed and approved items are still real Outlook messages, so "Delete all"
    // on those archive sections genuinely moves them to Deleted Items — the backend
    // delete_item has no status guard, it just resolves the message id and moves it.
    const targets = list.filter(it => it.status !== 'deleted');
    if (!targets.length) return;
    if (!window.confirm(
      `Move ${targets.length} email${targets.length === 1 ? '' : 's'} to Outlook's Deleted Items? ` +
      `This can't be undone here.`)) return;
    let cur = etag;
    for (const item of targets) {
      // Clear any prior delete error for this item before retrying it.
      setDeleteFailed(prev => {
        if (!prev.has(item.id)) return prev;
        const n = new Map(prev); n.delete(item.id); return n;
      });
      const del = (withEtag) =>
        fetch(`/api/email/queue/${encodeURIComponent(item.id)}/delete`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ etag: withEtag }),
        });
      try {
        let res = await del(cur);
        let json = await res.json().catch(() => null);
        let { outcome, etag: nextEtag, reason } = deleteOutcome(res.status, json);
        if (outcome === 'conflict') {
          // Etag raced by something outside this loop — re-read fresh and retry once.
          const fresh = await fetch('/api/email/queue').then(r => r.json()).catch(() => null);
          if (fresh && fresh.etag) {
            cur = fresh.etag;
            res = await del(cur);
            json = await res.json().catch(() => null);
            ({ outcome, etag: nextEtag, reason } = deleteOutcome(res.status, json));
          }
        }
        if (nextEtag) cur = nextEtag;
        if (outcome !== 'deleted' && outcome !== 'conflict') {
          // Confirmed failure (e.g. GRASP quota) — keep the row, attach the reason.
          setDeleteFailed(prev => new Map(prev).set(item.id, reason));
        }
      } catch {
        setDeleteFailed(prev => new Map(prev).set(
          item.id, "Couldn't delete — the request did not complete. The email was NOT removed."));
      }
    }
    setEtag(cur ?? null);
    refresh();
  };

  // --- Detail panel ---
  const renderDetailPanel = (item) => {
    const sectionStyle = { marginBottom: 'var(--space-md)' };
    const labelStyle = { fontSize: '0.72em', fontWeight: 600, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '0.3em' };
    const toggleStyle = {
      display: 'inline-flex', alignItems: 'center', gap: '0.35em',
      cursor: 'pointer', color: 'var(--accent)', fontSize: '0.8em', background: 'none',
      border: 'none', padding: 0,
    };
    const isBusy = busyId === item.id;
    // A confirmed-failed delete (429/quota or any error) keeps the row visible
    // and attaches the reason here; prefer the live per-item state, falling back
    // to the item's persisted lastActionError so a reload still shows WHY.
    const deleteError = deleteFailed.has(item.id)
      ? deleteFailed.get(item.id)
      : (typeof item.lastActionError === 'string' && item.lastActionError.trim()
        ? item.lastActionError.trim() : null);
    const isApproved = item.status === 'approved';
    const isDismissed = item.status === 'dismissed';
    const isDeleted = item.status === 'deleted';
    const isClassifying = item.status === 'needs-classify';
    const isFyiClassified = item.classification === 'fyi';
    const isReadOnly = isApproved || isDismissed || isDeleted || isClassifying;
    const drafting = draftingIds.has(item.id);
    const draftFailed = draftFailedIds.has(item.id);
    const hasDraft = (item.draft || '').trim() !== '';
    const undrafted = !isReadOnly && !hasDraft;
    const workerDrafting = undrafted && item.status === 'needs-draft' && !drafting && !draftFailed;
    const updating = !isReadOnly && hasDraft && item.status === 'needs-review' && item.needsRedraft === true;
    const editing = editingId === item.id;
    const dirty = editing && draftText !== (item.draft || '');

    const currentText = editing ? draftText : (item.draft || '');
    const generated = item.generatedDraft || '';
    const showDiff = !isReadOnly && generated.trim() !== '' && generated.trim() !== currentText.trim();
    const diffTokens = showDiff ? wordDiff(generated, currentText) : null;

    // Thread Summary: two AI-derived sub-parts (summary = threadContext, ask =
    // threadAsk). Both optional; a missing field renders a muted placeholder,
    // never the literal 'undefined' and never a fabricated ask (the decision is
    // a pure transform unit-tested in emailDetail.test.mjs).
    const summary = threadSummaryParts(item);

    // Thread Context: an ordered array of {sender, timestamp, body} turns
    // captured by the scan worker. latestThreadView picks the LATEST turn to
    // show by default and exposes the REAL turn count for the "Show full email
    // (N messages)" expander; with no structured history it falls back to the
    // full concatenated body/snippet. Read defensively (older items predate the
    // field) so it never crashes and never renders 'undefined'. All text is
    // EXTERNAL email content, rendered via React's {…} interpolation (escaped),
    // never dangerouslySetInnerHTML.
    const view = latestThreadView(item);

    // Shared card markup for one thread turn (sender + relative timestamp +
    // body) — used for the single latest-message card AND each card in the
    // expanded full timeline so who-said-what is preserved verbatim.
    // Render an email body as markdown via the canonical app stack (the same
    // ReactMarkdown + remarkGfm/rehypeHighlight + markdown-body used by chat,
    // Sessions, Memory) so **bold**, [links](…) and - bullets render as real
    // formatting instead of literal markdown text. ErrorBoundary keeps a
    // malformed-markdown throw from blanking the whole detail panel.
    const renderBody = (text) => (
      <ErrorBoundary label="Couldn't render this email body.">
        <div className="markdown-body" style={{ color: 'var(--text)', lineHeight: 1.5, wordBreak: 'break-word' }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {text || ''}
          </ReactMarkdown>
        </div>
      </ErrorBoundary>
    );

    const renderTurnCard = (turn, key) => {
      // displayTimestamp (emailDetail.js) returns a relative-time string for an
      // epoch/ISO timestamp, the VERBATIM human date for a quoted-header date
      // string that `new Date()` can't parse ("Wednesday, June 24, 2026 at
      // 10:23" — NEVER the broken "--"), and '' when there's nothing to show.
      // Gating the slot on the resolved string keeps an empty/unusable
      // timestamp from rendering an empty span. Untrusted email text -> React
      // {…} interpolation only.
      const tsLabel = displayTimestamp(turn.timestamp);
      return (
      <div
        key={key}
        className="card"
        style={{
          padding: '0.6em 0.9em',
          borderLeft: '3px solid var(--accent)',
          fontSize: '0.85em',
        }}
      >
        <div style={{ display: 'flex', gap: '0.6em', alignItems: 'baseline', flexWrap: 'wrap' }}>
          <span style={{ fontWeight: 600, color: 'var(--text)' }}>{turn.sender || 'unknown'}</span>
          {tsLabel && (
            <span style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{tsLabel}</span>
          )}
        </div>
        {/* Optional "To:" line — who this turn was sent to. Rendered ONLY when
            the turn carries a non-empty recipients STRING (the backend joins
            display names into a human-readable string — see email.py); skipped
            entirely otherwise so we never show an empty "To:" or a placeholder.
            All text via React {…} interpolation, never dangerouslySetInnerHTML. */}
        {typeof turn.recipients === 'string' && turn.recipients.trim() && (
          <div style={{ color: 'var(--muted)', fontSize: '0.85em', marginTop: '0.15em', wordBreak: 'break-word' }}>
            To: {turn.recipients}
          </div>
        )}
        {/* The body is run through autoFormatBody first (FEATURE #1): a pure,
            conservative, idempotent, content-preserving transform that promotes
            short question/Title-Case header lines (followed by a blank line) into
            markdown subheadings and leaves real lists/tables/links/paragraphs
            intact — making flat newsletter prose scannable WITHOUT an LLM. The
            single-turn card's body is the FULL item.emailBody (latestThreadView),
            so the CRIS Flash renders end-to-end, never the 4096 mid-sentence cut.
            Still rendered via ReactMarkdown ({…} escaped), never dangerouslySetInnerHTML. */}
        <div style={{ marginTop: '0.3em' }}>
          {renderBody(autoFormatBody(turn.body))}
        </div>
      </div>
      );
    };

    const subjectText = (item.subject || '').trim();

    return (
      <div style={{ padding: 'var(--space-md)', background: 'var(--surface2)', borderRadius: 'var(--radius)' }}>
        {/* --- Subject header + copy button (paste into Outlook search) ----
            The subject is the most reliable key for finding the thread back in
            Outlook, so we surface it at the top of the panel with a one-click
            copy. */}
        <div style={{ ...sectionStyle, display: 'flex', alignItems: 'baseline', gap: '0.5em', flexWrap: 'wrap' }}>
          <span style={{ fontWeight: 600, color: 'var(--text)', wordBreak: 'break-word', flex: 1, minWidth: 0 }}>
            {subjectText || '(no subject)'}
          </span>
          {subjectText && (
            <button
              type="button"
              onClick={() => copySubject(item.id, subjectText)}
              title="Copy subject to search in Outlook"
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '0.3em',
                cursor: 'pointer', background: 'none', border: '1px solid var(--border)',
                borderRadius: 'var(--radius)', color: 'var(--accent)',
                fontSize: '0.75em', padding: '0.2em 0.5em', whiteSpace: 'nowrap',
              }}
            >
              <FiClipboard size={12} />
              {subjectCopied === item.id ? 'Copied!' : 'Copy subject'}
            </button>
          )}
        </div>

        {/* --- 1. Thread Summary (AI summary + what they need from you) ----
            Two AI-derived sub-parts. Both are optional: a missing/empty field
            (older item, an FYI with no real ask, or a generation that didn't
            emit one) renders a muted placeholder — NEVER 'undefined', never a
            fabricated ask. All text via React {…} interpolation, never
            dangerouslySetInnerHTML. */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Thread summary</div>
          <div style={{ marginBottom: '0.6em' }}>
            <div style={{ ...labelStyle, fontSize: '0.66em', marginBottom: '0.2em' }}>Summary</div>
            {summary.hasSummary ? (
              <div style={{ color: 'var(--text)', fontSize: '0.88em', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {summary.summary}
              </div>
            ) : (
              <div style={{ color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
                No summary available.
              </div>
            )}
          </div>
          <div>
            <div style={{ ...labelStyle, fontSize: '0.66em', marginBottom: '0.2em' }}>What they need from you</div>
            {summary.hasAsk ? (
              <div style={{ color: 'var(--text)', fontSize: '0.88em', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {summary.ask}
              </div>
            ) : (
              <div style={{ color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
                No specific ask detected.
              </div>
            )}
          </div>
        </div>

        {/* --- 2. Thread Context (latest message + expandable full timeline) -
            The newest turn shows by default; "Show full email (N messages)"
            expands the full oldest→newest per-turn cards (reusing renderTurnCard
            so who-said-what is preserved). N is the REAL turn count from
            latestThreadView. With no structured history we fall back to the full
            body/snippet. emailBodyOpen drives this section's expand. */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Thread context</div>
          {view.latest ? (
            renderTurnCard(view.latest, 'latest')
          ) : (
            <div
              className="card"
              style={{
                padding: '0.7em 0.9em', fontSize: '0.85em',
                color: 'var(--text)', maxHeight: '400px', overflow: 'auto',
              }}
            >
              {view.fallbackBody
                ? renderBody(autoFormatBody(view.fallbackBody))
                : <span style={{ color: 'var(--muted)', fontStyle: 'italic' }}>(no email body available)</span>}
            </div>
          )}
          {view.count > 1 && (
            <>
              <button
                type="button"
                style={{ ...toggleStyle, marginTop: '0.5em' }}
                onClick={() => setEmailBodyOpen(v => !v)}
              >
                {emailBodyOpen ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                {emailBodyOpen ? 'Hide full email' : `Show full email (${view.count} messages)`}
              </button>
              {emailBodyOpen && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5em', marginTop: '0.5em' }}>
                  {view.turns.map((turn, i) => renderTurnCard(turn, turn.timestamp || i))}
                </div>
              )}
            </>
          )}
        </div>

        {/* --- 3. Draft reply --- */}
        <div style={sectionStyle}>
          <div style={labelStyle}>
            {isApproved ? 'Approved draft'
              : isDismissed ? 'Dismissed draft'
              : isClassifying ? 'Classification'
              : 'Draft reply'}
          </div>
          {isFyiClassified && !isReadOnly && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', color: 'var(--muted)', fontSize: '0.8em', marginBottom: '0.4em' }}>
              <FiCheckCircle size={12} /> Classified FYI — no reply auto-drafted. Generate one below if you want to reply.
            </div>
          )}
          {updating && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', color: 'var(--muted)', fontSize: '0.8em', fontStyle: 'italic', marginBottom: '0.4em' }}>
              <FiClock size={12} style={PULSE_ICON_STYLE} /> New messages arrived — updating this draft...
            </div>
          )}
          {isApproved ? (
            <div>
              <div style={{ color: 'var(--text)', fontSize: '0.9em', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.5 }}>
                {item.draft || '(no text recorded)'}
              </div>
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', marginTop: '0.5em', color: 'var(--muted)', fontSize: '0.8em' }}>
                <FiCheckCircle size={12} /> Approved {relativeTime(item.approvedAt ?? item.ts)}
              </div>
            </div>
          ) : isDismissed ? (
            <div style={{ color: 'var(--text)', fontSize: '0.9em', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.5 }}>
              {item.draft || '(no draft recorded)'}
            </div>
          ) : isClassifying ? (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
              <FiClock size={13} style={PULSE_ICON_STYLE} /> classifying... <span style={{ opacity: 0.8 }}>(deciding whether a reply is needed)</span>
            </div>
          ) : undrafted ? (
            workerDrafting ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45em' }}>
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
                  <FiClock size={13} style={PULSE_ICON_STYLE} /> drafting... <span style={{ opacity: 0.8 }}>(auto)</span>
                </div>
                <div style={{ color: 'var(--muted)', fontSize: '0.78em' }}>
                  A reply is being generated in the background — it will appear here shortly.
                </div>
                <button
                  className="btn"
                  disabled={isBusy}
                  onClick={() => generateDraft(item.id, { quiet: false })}
                  title="Force a draft now"
                  style={{ alignSelf: 'flex-start', fontSize: '0.85em', padding: '0.2em 0.5em' }}
                >
                  <FiRotateCw size={12} /> Generate now
                </button>
              </div>
            ) : drafting ? (
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
                <FiClock size={13} /> drafting...
              </div>
            ) : (
              // On-demand draft generation (FEATURE #2-UI). An FYI item lands here
              // undrafted (it is NOT auto-drafted — D-056 #2; the backend routes
              // fyi -> needs-review with no draft and _draft_pending skips it). The
              // user generates a reply ON DEMAND with this button, which reuses the
              // existing generateDraft -> POST /queue/{id}/refresh path and works
              // whether the workers are OFF (default) or ON. After it lands the
              // item becomes a normal drafted item (textarea + Polish + Save +
              // Approve + the To/CC editor). A needs-draft item that simply hasn't
              // been auto-drafted yet also gets a manual escape hatch here.
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5em' }}>
                {draftFailed && (
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--muted)', fontSize: '0.82em' }}>
                    <FiAlertCircle size={13} /> The draft could not be generated. No reply was fabricated — retry below.
                  </div>
                )}
                <button
                  className={isFyiClassified ? 'btn btn-primary' : 'btn'}
                  disabled={isBusy}
                  onClick={() => generateDraft(item.id, { quiet: false })}
                  title={isFyiClassified
                    ? 'Generate a reply draft for this FYI item (none is drafted automatically)'
                    : 'Generate a draft reply for this item'}
                  style={{ alignSelf: 'flex-start' }}
                >
                  <FiRotateCw size={13} /> {draftFailed
                    ? 'Retry draft'
                    : (isFyiClassified ? 'Generate reply' : 'Generate draft')}
                </button>
              </div>
            )
          ) : (
            <textarea
              ref={draftRef}
              className="form-textarea"
              value={editing ? draftText : (item.draft || '')}
              onChange={e => { setEditingId(item.id); setDraftText(e.target.value); autoSizeDraft(); }}
              placeholder="The drafted reply will appear here. Edit it before approving — your edits teach the style preferences."
              style={{ width: '100%' }}
            />
          )}
          {/* --- Reply-all To/CC editor (FEATURE #3-UI) ----------------------
              Shown once a draft exists (the recipients only matter for a reply
              you're about to approve). Seeded on row-expand from seedRecipients:
              the user's persisted edit if recipientsEdited, else the reply-all
              default (To = sender + original To minus me; CC = original CC minus
              me plus me, de-duped). The user adds/removes chips; the edit persists
              via the save-draft PUT (recipientsDirty), so a refresh keeps it and
              Approve uses it. All addresses render via React {…} interpolation —
              never dangerouslySetInnerHTML. */}
          {!isReadOnly && !undrafted && (() => {
            const chipRow = (field, list) => (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35em', alignItems: 'center' }}>
                {list.length === 0 && (
                  <span style={{ color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }}>
                    {field === 'cc' ? '(no CC)' : '(no recipients)'}
                  </span>
                )}
                {list.map(addr => (
                  <span
                    key={addr}
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: '0.3em',
                      background: 'var(--surface2)', color: 'var(--text)',
                      border: '1px solid var(--border)', borderRadius: 'var(--radius)',
                      fontSize: '0.78em', padding: '0.12em 0.2em 0.12em 0.5em',
                    }}
                  >
                    {addr}
                    <button
                      type="button"
                      onClick={() => removeRecipient(field, addr)}
                      title={`Remove ${addr}`}
                      aria-label={`Remove ${addr}`}
                      style={{
                        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                        cursor: 'pointer', background: 'none', border: 'none', padding: '0.1em',
                        color: 'var(--muted)', lineHeight: 1, borderRadius: 'var(--radius)',
                      }}
                    >
                      <FiX size={12} />
                    </button>
                  </span>
                ))}
              </div>
            );
            const addBox = (field) => {
              const value = field === 'cc' ? ccInput : toInput;
              const setVal = field === 'cc' ? setCcInput : setToInput;
              return (
                <div style={{ display: 'flex', gap: '0.35em', alignItems: 'center', marginTop: '0.3em' }}>
                  <input
                    type="email"
                    value={value}
                    onChange={e => setVal(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addRecipient(field); } }}
                    placeholder={`Add ${field === 'cc' ? 'CC' : 'To'} address`}
                    style={{
                      flex: 1, minWidth: 0, fontSize: '0.8em', padding: '0.25em 0.5em',
                      background: 'var(--surface)', color: 'var(--text)',
                      border: '1px solid var(--border)', borderRadius: 'var(--radius)',
                    }}
                  />
                  <button
                    type="button"
                    className="btn"
                    onClick={() => addRecipient(field)}
                    disabled={!value.trim()}
                    title={`Add ${field === 'cc' ? 'CC' : 'To'} recipient`}
                    style={{ fontSize: '0.78em', padding: '0.2em 0.5em' }}
                  >
                    <FiPlus size={12} /> Add
                  </button>
                </div>
              );
            };
            return (
              <div style={{ marginTop: '0.6em', display: 'flex', flexDirection: 'column', gap: '0.6em' }}>
                <div>
                  <div style={{ ...labelStyle, fontSize: '0.66em', marginBottom: '0.25em' }}>To</div>
                  {chipRow('to', recipientsTo)}
                  {addBox('to')}
                </div>
                <div>
                  <div style={{ ...labelStyle, fontSize: '0.66em', marginBottom: '0.25em' }}>CC</div>
                  {chipRow('cc', recipientsCc)}
                  {addBox('cc')}
                </div>
                {recipientsDirty && (
                  <span style={{ color: 'var(--muted)', fontSize: '0.78em' }}>
                    Recipients edited — Save or Approve to keep them on the Outlook draft.
                  </span>
                )}
              </div>
            );
          })()}
          {/* --- Draft tools — Polish / Save / Regenerate sit WITH the textarea
              because they shape the draft, distinct from the disposition row
              (Approve / Dismiss / Delete / Mute) below. Matches Slack layout.
              Shown when the draft text OR the reply-all recipients are dirty so a
              recipients-only edit still gets a Save (Polish stays gated on text). */}
          {!isReadOnly && !undrafted && (dirty || recipientsDirty) && (() => {
            const polishText = (editing ? draftText : (item.draft || ''));
            const noText = polishText.trim() === '';
            const isPolishing = polishingId === item.id;
            return (
              <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center', marginTop: '0.5em' }}>
                {dirty && (
                  <button
                    className="btn"
                    disabled={isBusy || isPolishing || noText}
                    onClick={() => polishDraft(item.id)}
                    title="Rewrite the current text to read more fluently while keeping your own wording and language"
                  >
                    <FiFeather size={13} /> {isPolishing ? 'Polishing...' : 'Polish'}
                  </button>
                )}
                <button className="btn" disabled={isBusy} onClick={() => saveDraft(item.id)} title="Save the edited draft and recipients">
                  <FiSave size={13} /> Save edit
                </button>
                <span style={{ color: 'var(--muted)', fontSize: '0.8em' }}>
                  Unsaved edits — Approve will use the edited {dirty && recipientsDirty ? 'text and recipients' : dirty ? 'text' : 'recipients'}.
                </span>
              </div>
            );
          })()}
        </div>

        {/* --- Word diff vs generated --- */}
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
                if (tok.type === 'same') return <span key={tok.type + '-' + i + '-' + tok.value.slice(0, 12)}>{tok.value}</span>;
                if (tok.type === 'removed') {
                  return (
                    <span
                      key={tok.type + '-' + i + '-' + tok.value.slice(0, 12)}
                      style={{ color: 'var(--muted)', textDecoration: 'line-through', textDecorationColor: 'var(--muted)' }}
                    >
                      {tok.value}
                    </span>
                  );
                }
                return <span key={tok.type + '-' + i + '-' + tok.value.slice(0, 12)} style={{ color: 'var(--accent)', fontWeight: 600 }}>{tok.value}</span>;
              })}
            </div>
            <div style={{ display: 'flex', gap: '1em', marginTop: '0.4em', fontSize: '0.75em', color: 'var(--muted)' }}>
              <span><span style={{ textDecoration: 'line-through' }}>generated</span> removed</span>
              <span><span style={{ color: 'var(--accent)' }}>your edit</span> added</span>
            </div>
          </div>
        )}

        {/* --- Disposition actions (matches Slack layout: primary left,
            destructive pushed right via marginLeft:auto) --- */}
        {isDismissed && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn" disabled={isBusy} onClick={() => undismiss(item.id)} title="Restore this item to the review queue">
              <FiRotateCw size={13} /> Undo dismiss
            </button>
          </div>
        )}
        {undrafted && !isReadOnly && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn btn-quiet-danger" disabled={isBusy} onClick={() => dismiss(item.id)} title="Dismiss without replying (marks read in Outlook)">
              <FiTrash2 size={13} /> Dismiss
            </button>
            <button className="btn btn-danger" disabled={isBusy} onClick={() => deleteEmail(item.id)} title="Delete this email from Outlook (moves to Deleted Items)">
              <FiTrash size={13} /> Delete
            </button>
            <button className="btn" disabled={isBusy} onClick={() => mute(item.id)} title="Mute this conversation — dismiss and stop surfacing future messages">
              <FiBellOff size={13} /> Mute
            </button>
          </div>
        )}
        {!isReadOnly && !undrafted && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button
              className="btn btn-primary"
              disabled={isBusy}
              onClick={() => approve(item.id)}
              title="Approve: copies to clipboard and saves as Outlook draft"
            >
              <FiClipboard size={13} /> Approve
            </button>
            <button className="btn btn-quiet-danger" disabled={isBusy} onClick={() => dismiss(item.id)} title="Dismiss without replying (marks read in Outlook)" style={{ marginLeft: 'auto' }}>
              <FiTrash2 size={13} /> Dismiss
            </button>
            <button className="btn btn-danger" disabled={isBusy} onClick={() => deleteEmail(item.id)} title="Delete this email from Outlook (moves to Deleted Items)">
              <FiTrash size={13} /> Delete
            </button>
            <button className="btn" disabled={isBusy} onClick={() => mute(item.id)} title="Mute this conversation — dismiss and stop surfacing future messages">
              <FiBellOff size={13} /> Mute
            </button>
          </div>
        )}

        {/* Inline delete-failure notice (Fix task 6). A delete that the backend
            could NOT confirm (429/quota or any error) keeps this item right here,
            actionable, and shows WHY — never the silent-vanish lie. Uses the same
            --warning/--muted/--border tokens as the rest of the page; no new
            tokens. The reason text is external/log-derived → React {…} only. */}
        {deleteError && (
          <div
            style={{
              display: 'flex', alignItems: 'flex-start', gap: '0.4em',
              color: 'var(--warning)', fontSize: '0.82em', marginTop: 'var(--space-sm)',
              padding: '0.5em 0.7em', border: '1px solid var(--border)',
              borderRadius: 'var(--radius)', background: 'var(--surface)',
            }}
          >
            <FiAlertCircle size={14} style={{ flexShrink: 0, marginTop: '0.1em' }} />
            <span>
              {deleteError}
              <span style={{ color: 'var(--muted)' }}> The email was NOT deleted — it is still in your inbox. Try again later.</span>
            </span>
          </div>
        )}

        {/* Approve confirmation */}
        {approveMsg[item.id] && (
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', color: 'var(--accent)', fontSize: '0.85em', marginTop: 'var(--space-sm)', fontWeight: 600 }}>
            <FiCheckCircle size={14} /> {approveMsg[item.id]}
          </div>
        )}
      </div>
    );
  };

  // --- Table rendering ---
  const renderTable = (list) => (
    <table className="data-table">
      <thead>
        <tr>
          <th>From</th>
          <th>Subject</th>
          <th>Preview</th>
          <th style={{ textAlign: 'center' }}>When</th>
          <th style={{ textAlign: 'center' }}>Status</th>
          <th style={{ textAlign: 'center' }}>Action</th>
        </tr>
      </thead>
      <tbody>
        {list.map(item => {
          const badge = statusBadge(item);
          const open = expandedId === item.id;
          const rowHasDraft = (item.draft || '').trim() !== '';
          const rowWorkerDrafting = item.status === 'needs-draft' && !rowHasDraft
            && !draftingIds.has(item.id) && !draftFailedIds.has(item.id);
          const rowUpdating = item.status === 'needs-review' && rowHasDraft && item.needsRedraft === true;
          return (
            <Fragment key={item.id}>
              <tr onClick={() => toggleExpand(item)} tabIndex={0} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleExpand(item); } }} style={{ cursor: 'pointer' }}>
                <td>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', flexWrap: 'wrap' }}>
                    <span style={{ color: 'var(--muted)' }}>
                      {open ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                    </span>
                    <span>{fromColumnLabel(item)}</span>
                    {/* Source-folder provenance badge (D-058 §5, AC-2): names where
                        the scan ingested this item ("Inbox" for the root path, the
                        subfolder display name like "1 GSD" for folder-sourced items).
                        Rendered VERBATIM from sourceFolderLabel(item), which defaults
                        an older item lacking the field to "Inbox" so no row reads
                        blank — never an invented name, never a raw AAMk... id. */}
                    <span className="badge" style={SOURCE_FOLDER_BADGE_STYLE} title={`Source folder: ${sourceFolderLabel(item)}`}>
                      {sourceFolderLabel(item)}
                    </span>
                  </span>
                </td>
                <td style={{ fontWeight: 500 }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
                    <span>{truncate(item.subject, 60)}</span>
                    {(item.subject || '').trim() && (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); copySubject(item.id, (item.subject || '').trim()); }}
                        title="Copy subject to search in Outlook"
                        style={{
                          display: 'inline-flex', alignItems: 'center', gap: '0.25em',
                          cursor: 'pointer', background: 'none', border: 'none',
                          color: subjectCopied === item.id ? 'var(--success, var(--accent))' : 'var(--muted)',
                          fontSize: '0.78em', padding: '0.1em 0.2em', whiteSpace: 'nowrap', flexShrink: 0,
                        }}
                      >
                        <FiClipboard size={12} />
                        {subjectCopied === item.id && <span>Copied!</span>}
                      </button>
                    )}
                  </span>
                </td>
                <td title={item.snippet}>{truncate(item.snippet, 70)}</td>
                <td style={{ color: 'var(--muted)', fontSize: '0.85em', textAlign: 'center' }}>{relativeTime(item.ts)}</td>
                <td style={{ textAlign: 'center' }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', flexWrap: 'wrap', justifyContent: 'center' }}>
                    <span className={badge.className} style={badge.style}>{badge.label}</span>
                    {draftingIds.has(item.id) && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }}>
                        <FiClock size={11} /> drafting...
                      </span>
                    )}
                    {rowWorkerDrafting && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }} title="A reply is being generated in the background.">
                        <FiClock size={11} style={PULSE_ICON_STYLE} /> drafting... (auto)
                      </span>
                    )}
                    {rowUpdating && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em', fontStyle: 'italic' }} title="New messages arrived — draft updating.">
                        <FiClock size={11} style={PULSE_ICON_STYLE} /> updating...
                      </span>
                    )}
                    {!draftingIds.has(item.id) && draftFailedIds.has(item.id) && item.status === 'needs-draft' && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', color: 'var(--muted)', fontSize: '0.78em' }} title="The draft could not be generated — open the row to retry.">
                        <FiAlertCircle size={11} /> draft failed
                      </span>
                    )}
                  </span>
                </td>
                <td style={{ textAlign: 'center' }}>
                  {item.status !== 'approved' && item.status !== 'dismissed' && (
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

  const collapsibleStyle = {
    display: 'inline-flex', alignItems: 'center', gap: '0.4em',
    cursor: 'pointer', color: 'var(--accent)', fontSize: '0.85em', background: 'none',
    border: 'none', padding: 0, fontWeight: 600,
  };

  // "Dismiss all (N)" for a group header — soft-dismisses every item in that
  // declared group (recoverable via Undo), leaving other groups untouched.
  const dismissAllBtn = (list) => (
    <button
      className="btn"
      style={{ fontSize: '0.78em', padding: '0.2em 0.5em' }}
      onClick={() => dismissAll(list)}
      title="Dismiss every item in this group (recoverable from the Dismissed section)"
    >
      <FiTrash2 size={11} /> Dismiss all ({list.length})
    </button>
  );

  // "Delete all (N)" for a group header — the irreversible counterpart to
  // "Dismiss all": moves every item in the group to Outlook's Deleted Items
  // (confirms first, see deleteAll). Danger-styled so it reads distinctly from
  // the recoverable dismiss sitting beside it.
  const deleteAllBtn = (list) => (
    <button
      className="btn btn-danger"
      style={{ fontSize: '0.78em', padding: '0.2em 0.5em' }}
      onClick={() => deleteAll(list)}
      title="Delete every item in this group from Outlook (moves to Deleted Items — cannot be undone here)"
    >
      <FiTrash size={11} /> Delete all ({list.length})
    </button>
  );

  // Partition items
  const needsReviewItems = items.filter(isActionable);
  const approvedItems = items.filter(it => it.status === 'approved');
  const dismissedItems = items.filter(it => it.status === 'dismissed');
  const deletedItems = items.filter(it => it.status === 'deleted');

  const replyItems = needsReviewItems.filter(it => reviewGroup(it) === 'reply');

  // Sub-group Reply by recipientType: to-only (direct to me), to (me in To shared), cc (me in CC), dl (via distribution list), unknown
  const toOnlyItems = replyItems.filter(it => it.recipientType === 'to-only');
  const toItems = replyItems.filter(it => it.recipientType === 'to');
  const ccItems = replyItems.filter(it => it.recipientType === 'cc');
  const dlItems = replyItems.filter(it => it.recipientType === 'dl');
  const unknownItems = replyItems.filter(it => !it.recipientType || it.recipientType === 'unknown');
  const fyiItems = needsReviewItems.filter(it => reviewGroup(it) === 'fyi');
  const classifyingItems = needsReviewItems.filter(it => reviewGroup(it) === 'classifying');

  // Muted-conversations management: the keys of the muted map, newest-muted
  // first. An email mute key is the raw conversationId, so a label resolves from
  // any item on the same conversation (subject, then sender), falling back to the
  // raw key. Empty map renders nothing.
  const mutedKeys = sortedMutedKeys(muted);

  return (
    <div>
      <div className="page-header">
        <h2>Email</h2>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.8em' }}>
          {lastScanAt !== null && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', color: 'var(--muted)', fontSize: '0.82em' }}>
              <FiClock size={12} /> Updated {relativeTime(lastScanAt)}
            </span>
          )}
          <button
            type="button"
            role="switch"
            aria-checked={!paused}
            onClick={togglePaused}
            disabled={pausing}
            title={paused
              ? 'Paused — Email scanning, drafting and classification are off. Click to resume.'
              : 'Running — Email scanning, drafting and classification are on. Click to pause.'}
            aria-label={paused ? 'Email scanning paused — resume' : 'Email scanning running — pause'}
            style={{
              position: 'relative', display: 'inline-flex', alignItems: 'center',
              width: '104px', height: '34px', flex: 'none', padding: 0,
              borderRadius: '17px', border: 'none',
              background: paused ? 'var(--warning)' : 'var(--accent2)',
              cursor: pausing ? 'default' : 'pointer',
              opacity: pausing ? 0.6 : 1,
              transition: 'background 0.15s ease',
              flexDirection: paused ? 'row-reverse' : 'row',
              color: '#000', fontSize: '0.82em', fontWeight: 700,
            }}
          >
            <span
              aria-hidden="true"
              style={{
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                width: '28px', height: '28px', margin: '0 3px', flex: 'none',
                borderRadius: '50%', background: 'var(--bg)', color: 'var(--text)',
              }}
            >
              {paused ? <FiPause size={13} /> : <FiPlay size={13} />}
            </span>
            <span style={{ flex: 1, textAlign: 'center', letterSpacing: '0.02em' }}>
              {paused ? 'Paused' : 'Running'}
            </span>
          </button>
          <button
            className="btn btn-primary"
            onClick={refreshQueue}
            disabled={refreshing}
            style={{ height: '34px' }}
          >
            <FiRefreshCw size={14} /> {scanning ? 'Scanning...' : 'Refresh'}
          </button>
        </div>
      </div>

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

      {health && (
        health.ready ? (
          <div
            className="badge badge-ok"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em', marginBottom: 'var(--space-md)' }}
          >
            <FiCheckCircle size={13} /> Email delegation ready
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

      {/* GRASP-quota / Graph rate-limit banner (Fix task 5/6). When the backend
          reports graphThrottled we say so honestly instead of letting an empty
          queue read as a falsely-calm "No items to review" / green-ready state.
          It is gated on graphThrottled, so it CLEARS automatically the moment the
          backend reports recovery (anti-STICKY-THROTTLE). lastGraphError carries
          the human detail (status + GRASP code) and is log-derived → React {…}. */}
      {graphThrottled && (
        <div
          className="badge badge-warn"
          style={{
            display: 'flex', alignItems: 'flex-start', gap: '0.4em',
            marginBottom: 'var(--space-md)', maxWidth: '100%', whiteSpace: 'normal',
          }}
        >
          <FiAlertCircle size={14} style={{ flexShrink: 0, marginTop: '0.1em' }} />
          <span>
            Outlook temporarily rate-limited (GRASP quota) — retrying later. Scanning and deletes are backed off until it recovers.
            {lastGraphError ? <span style={{ opacity: 0.85 }}> ({lastGraphError})</span> : null}
          </span>
        </div>
      )}

      {/* Progress summary */}
      {needsReviewItems.length > 0 && (
        <div style={{ color: 'var(--muted)', fontSize: '0.85em', marginBottom: 'var(--space-md)' }}>
          {countActionable(items)} to review
          {replyItems.length > 0 ? ` · ${replyItems.length} reply` : ''}
          {fyiItems.length > 0 ? ` · ${fyiItems.length} fyi` : ''}
          {classifyingItems.length > 0 ? ` · ${classifyingItems.length} classifying...` : ''}
        </div>
      )}

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {/* --- Reply section (always expanded) --- */}
      {replyItems.length === 0 && fyiItems.length === 0 && classifyingItems.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            {notAvailable ? (
              <>
                <h3>Email fetch not available</h3>
                <p>
                  The email queue could not be fetched (the Outlook integration is not wired up
                  or returned nothing). Try Refresh — no items are shown until real data arrives.
                </p>
              </>
            ) : (
              <>
                <h3>No items to review</h3>
                <p>There are no unread emails needing a reply. Use Refresh to check again.</p>
              </>
            )}
          </div>
        </div>
      ) : (
        <>
          {toOnlyItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  To me only ({toOnlyItems.length})
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                  {dismissAllBtn(toOnlyItems)}
                  {deleteAllBtn(toOnlyItems)}
                </div>
              </div>
              <div className="card">{renderTable(toOnlyItems)}</div>
            </div>
          )}
          {toItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Me in To ({toItems.length})
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                  {dismissAllBtn(toItems)}
                  {deleteAllBtn(toItems)}
                </div>
              </div>
              <div className="card">{renderTable(toItems)}</div>
            </div>
          )}
          {ccItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Me in CC ({ccItems.length})
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                  {dismissAllBtn(ccItems)}
                  {deleteAllBtn(ccItems)}
                </div>
              </div>
              <div className="card">{renderTable(ccItems)}</div>
            </div>
          )}
          {dlItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Via DL ({dlItems.length})
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                  {dismissAllBtn(dlItems)}
                  {deleteAllBtn(dlItems)}
                </div>
              </div>
              <div className="card">{renderTable(dlItems)}</div>
            </div>
          )}
          {unknownItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Other ({unknownItems.length})
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                  {dismissAllBtn(unknownItems)}
                  {deleteAllBtn(unknownItems)}
                </div>
              </div>
              <div className="card">{renderTable(unknownItems)}</div>
            </div>
          )}

          {/* FYI section (collapsible) */}
          {fyiItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
                <button type="button" style={collapsibleStyle} aria-expanded={fyiOpen} onClick={() => setFyiOpen(v => !v)}>
                  {fyiOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
                  FYI — no reply needed ({fyiItems.length})
                </button>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                  {dismissAllBtn(fyiItems)}
                  {deleteAllBtn(fyiItems)}
                </div>
              </div>
              {fyiOpen && (
                <div className="card" style={{ marginTop: '0.5em' }}>{renderTable(fyiItems)}</div>
              )}
            </div>
          )}

          {/* Classifying section */}
          {classifyingItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>Classifying... ({classifyingItems.length})</span>
              </div>
              <div className="card">{renderTable(classifyingItems)}</div>
            </div>
          )}
        </>
      )}

      {/* --- Approved (collapsible, collapsed by default) --- */}
      {approvedItems.length > 0 && (
        <div style={{ marginTop: 'var(--space-md)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
            <button type="button" style={collapsibleStyle} aria-expanded={approvedOpen} onClick={() => setApprovedOpen(v => !v)}>
              {approvedOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
              Approved ({approvedItems.length})
            </button>
            {deleteAllBtn(approvedItems)}
          </div>
          {approvedOpen && (
            <div className="card" style={{ marginTop: '0.5em' }}>
              {renderTable(approvedItems)}
            </div>
          )}
        </div>
      )}

      {/* --- Dismissed (collapsible, collapsed by default) --- */}
      {dismissedItems.length > 0 && (
        <div style={{ marginTop: 'var(--space-md)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.4em' }}>
            <button type="button" style={collapsibleStyle} aria-expanded={dismissedOpen} onClick={() => setDismissedOpen(v => !v)}>
              {dismissedOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
              Dismissed ({dismissedItems.length})
            </button>
            {deleteAllBtn(dismissedItems)}
          </div>
          {dismissedOpen && (
            <div className="card" style={{ marginTop: '0.5em' }}>
              {renderTable(dismissedItems)}
            </div>
          )}
        </div>
      )}

      {/* --- Deleted (collapsible, collapsed by default) --- */}
      {deletedItems.length > 0 && (
        <div style={{ marginTop: 'var(--space-md)' }}>
          <button type="button" style={collapsibleStyle} aria-expanded={deletedOpen} onClick={() => setDeletedOpen(v => !v)}>
            {deletedOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
            Deleted ({deletedItems.length})
          </button>
          {deletedOpen && (
            <div className="card" style={{ marginTop: '0.5em' }}>
              {renderTable(deletedItems)}
            </div>
          )}
        </div>
      )}

      {/* --- Muted conversations (collapsible, collapsed by default) --- */}
      {/* The conversations whose future messages the scan skips. Each row shows a
          friendly label (resolved from any queue item on the same conversation)
          and an Unmute button. Empty map renders nothing. */}
      {mutedKeys.length > 0 && (
        <div style={{ marginTop: 'var(--space-md)' }}>
          <button type="button" style={collapsibleStyle} aria-expanded={mutedOpen} onClick={() => setMutedOpen(v => !v)}>
            {mutedOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
            <FiBellOff size={13} /> Muted conversations ({mutedKeys.length})
          </button>
          {mutedOpen && (
            <div className="card" style={{ marginTop: '0.5em' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Conversation</th>
                    <th style={{ textAlign: 'center' }}>Muted</th>
                    <th style={{ textAlign: 'center' }}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {mutedKeys.map(key => (
                    <tr key={key}>
                      <td>
                        <span>{mutedLabel(key, items)}</span>
                        <span style={{ color: 'var(--muted)', fontSize: '0.78em', marginLeft: '0.5em' }}>
                          {key}
                        </span>
                      </td>
                      <td style={{ color: 'var(--muted)', fontSize: '0.85em', textAlign: 'center' }}>
                        {relativeTime(muted[key] ? muted[key] * 1000 : null)}
                      </td>
                      <td style={{ textAlign: 'center' }}>
                        <button
                          className="btn"
                          disabled={unmutingKey === key}
                          onClick={() => unmute(key)}
                          title="Unmute — let future messages in this conversation surface again"
                        >
                          <FiBellOff size={13} /> {unmutingKey === key ? 'Unmuting...' : 'Unmute'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div style={{ color: 'var(--muted)', fontSize: '0.78em', padding: '0.4em 0.6em 0' }}>
                Unmuting only lets future messages surface again — it does not restore any dismissed message. Muted conversations also expire automatically after 30 days.
              </div>
            </div>
          )}
        </div>
      )}

      {/* --- Style preferences (collapsible, collapsed by default) --- */}
      {styleLoaded && (
        <div style={{ marginTop: 'var(--space-lg)' }}>
          <button type="button" style={collapsibleStyle} aria-expanded={styleOpen} onClick={() => setStyleOpen(v => !v)}>
            {styleOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
            Style preferences
          </button>
          {styleOpen && (
            <div className="card" style={{ marginTop: '0.5em', padding: 'var(--space-md)' }}>
              <textarea
                className="form-textarea"
                value={styleContent}
                onChange={e => setStyleContent(e.target.value)}
                placeholder="Style preferences will appear here once the assistant learns your writing patterns."
                style={{ width: '100%', minHeight: '180px' }}
              />
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.7em', marginTop: 'var(--space-sm)' }}>
                <button
                  className="btn"
                  disabled={styleSaving}
                  onClick={saveStyle}
                >
                  <FiSave size={13} /> {styleSaving ? 'Saving...' : 'Save'}
                </button>
                {styleFeedback && (
                  <span style={{
                    display: 'inline-flex', alignItems: 'center', gap: '0.35em',
                    fontSize: '0.85em',
                    color: styleFeedback.type === 'ok' ? 'var(--accent)' : 'var(--warning)',
                  }}>
                    {styleFeedback.type === 'ok' ? <FiCheckCircle size={13} /> : <FiAlertCircle size={13} />}
                    {styleFeedback.text}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
