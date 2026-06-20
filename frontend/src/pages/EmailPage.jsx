import { Fragment, useEffect, useRef, useState } from 'react';
import {
  FiRefreshCw, FiRotateCw, FiTrash2, FiSave, FiChevronDown, FiChevronRight,
  FiCheckCircle, FiAlertCircle, FiClock, FiBellOff, FiPause, FiPlay,
  FiClipboard, FiMail,
} from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

// --- Email-specific actionable/grouping logic (mirrors slackQueue.js but
// terminal state is 'approved' instead of 'sent') ---

function isActionable(item) {
  const s = item && item.status;
  return s !== 'approved' && s !== 'dismissed';
}

function reviewGroup(item) {
  if (!isActionable(item)) return null;
  if (item && item.status === 'needs-classify') return 'classifying';
  if (item && item.classification === 'fyi') return 'fyi';
  return 'reply';
}

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
const FYI_BADGE_STYLE = { background: '#26262e', color: '#9a9ab0' };
const NEEDS_CLASSIFY_BADGE_STYLE = { background: '#2b2730', color: '#a39aad' };
const APPROVED_BADGE_STYLE = { background: '#1a2e1a', color: '#7ae67a' };
const PULSE_ICON_STYLE = { animation: 'pulse 1.4s ease-in-out infinite' };

function statusBadge(item) {
  const status = typeof item === 'string' ? item : (item && item.status);
  const classification = typeof item === 'string' ? '' : (item && item.classification);
  if (status === 'approved') return { label: 'approved', className: 'badge', style: APPROVED_BADGE_STYLE };
  if (status === 'edited') return { label: 'edited', className: 'badge badge-warn', style: undefined };
  if (status === 'dismissed') return { label: 'dismissed', className: 'badge', style: DISMISSED_BADGE_STYLE };
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

  const [draftingIds, setDraftingIds] = useState(() => new Set());
  const [draftFailedIds, setDraftFailedIds] = useState(() => new Set());
  const draftingRef = useRef(draftingIds);
  draftingRef.current = draftingIds;

  const [health, setHealth] = useState(null);

  // Approve confirmation message per item (transient)
  const [approveMsg, setApproveMsg] = useState({});
  const approveMsgRef = useRef({});

  // Collapsible sections
  const [approvedOpen, setApprovedOpen] = useState(false);
  const [dismissedOpen, setDismissedOpen] = useState(false);
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
      setHealth({
        ready: json.ready === true,
        detail: typeof json.detail === 'string' ? json.detail : '',
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
    } catch {
      setError('Failed to load the email queue.');
    }
  };

  useEffect(() => { refresh(); fetchHealth(); fetchStyle(); }, []);

  useLiveUpdates(['email_changed'], () => {
    clearRef(scanHintRef);
    setScanning(false);
    if (!editingId) refresh();
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

  // Expand/collapse a row
  const toggleExpand = (item) => {
    if (expandedId === item.id) {
      setExpandedId(null);
      setEditingId(null);
      setDraftText('');
      setEmailBodyOpen(false);
    } else {
      setExpandedId(item.id);
      setEditingId(null);
      setDraftText(item.draft || '');
      setEmailBodyOpen(false);
      setError(null);
    }
  };

  // Save edited draft
  const saveDraft = async (id) => {
    try {
      const res = await fetch(`/api/email/queue/${encodeURIComponent(id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft: draftText, etag }),
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

      // If the draft was edited, save it first
      if (editingId === id && draftText !== (item ? (item.draft || '') : '')) {
        const saveRes = await fetch(`/api/email/queue/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ draft: draftText, etag }),
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
    } catch {
      setError('Failed to mute the conversation.');
    } finally {
      setBusyId(null);
    }
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
    const isApproved = item.status === 'approved';
    const isDismissed = item.status === 'dismissed';
    const isClassifying = item.status === 'needs-classify';
    const isFyiClassified = item.classification === 'fyi';
    const isReadOnly = isApproved || isDismissed || isClassifying;
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

    return (
      <div style={{ padding: 'var(--space-md)', background: 'var(--surface2)', borderRadius: 'var(--radius)' }}>
        {/* --- Email body (collapsible so it doesn't dominate) --- */}
        <div style={sectionStyle}>
          <button type="button" style={toggleStyle} onClick={() => setEmailBodyOpen(v => !v)}>
            {emailBodyOpen ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
            {emailBodyOpen ? 'Hide full email' : 'Show full email'}
          </button>
          {emailBodyOpen && (
            <div
              className="card"
              style={{
                marginTop: '0.4em', padding: '0.7em 0.9em', fontSize: '0.85em',
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text)',
                maxHeight: '400px', overflow: 'auto', lineHeight: 1.5,
              }}
            >
              {item.emailBody || item.snippet || '(no email body available)'}
            </div>
          )}
        </div>

        {/* --- Thread context summary --- */}
        {item.threadContext && (
          <div style={sectionStyle}>
            <div style={labelStyle}>Thread summary</div>
            <div style={{ color: 'var(--text)', fontSize: '0.85em', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {item.threadContext}
            </div>
          </div>
        )}

        {/* --- Draft reply --- */}
        <div style={sectionStyle}>
          <div style={labelStyle}>
            {isApproved ? 'Approved draft'
              : isDismissed ? 'Dismissed draft'
              : isClassifying ? 'Classification'
              : 'Draft reply'}
          </div>
          {isFyiClassified && !isReadOnly && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35em', color: 'var(--muted)', fontSize: '0.8em', marginBottom: '0.4em' }}>
              <FiCheckCircle size={12} /> Classified FYI — no reply likely needed, but a draft is ready if you want to approve.
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

        {/* --- Actions --- */}
        {isDismissed && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn" disabled={isBusy} onClick={() => undismiss(item.id)} title="Restore this item to the review queue">
              <FiRotateCw size={13} /> Undo dismiss
            </button>
          </div>
        )}
        {undrafted && !isReadOnly && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            <button className="btn btn-danger" disabled={isBusy} onClick={() => dismiss(item.id)} title="Dismiss without replying">
              <FiTrash2 size={13} /> Dismiss
            </button>
            <button className="btn" disabled={isBusy} onClick={() => mute(item.id)} title="Mute this conversation — dismiss and ignore future messages">
              <FiBellOff size={13} /> Mute conversation
            </button>
          </div>
        )}
        {!isReadOnly && !undrafted && (
          <div style={{ display: 'flex', gap: '0.5em', flexWrap: 'wrap', alignItems: 'center' }}>
            {dirty && (
              <button className="btn" disabled={isBusy} onClick={() => saveDraft(item.id)} title="Save the edited draft">
                <FiSave size={13} /> Save edit
              </button>
            )}
            <button
              className="btn btn-primary"
              disabled={isBusy}
              onClick={() => approve(item.id)}
              title="Approve: copies to clipboard and saves as Outlook draft"
            >
              <FiClipboard size={13} /> Approve
            </button>
            <button className="btn" disabled={isBusy} onClick={() => regenerate(item.id)} title="Regenerate this draft">
              <FiRotateCw size={13} /> Regenerate
            </button>
            <button className="btn btn-danger" disabled={isBusy} onClick={() => dismiss(item.id)} title="Dismiss without replying">
              <FiTrash2 size={13} /> Dismiss
            </button>
            <button className="btn" disabled={isBusy} onClick={() => mute(item.id)} title="Mute this conversation — dismiss and ignore future messages">
              <FiBellOff size={13} /> Mute conversation
            </button>
            {dirty && (
              <span style={{ color: 'var(--muted)', fontSize: '0.8em' }}>
                Unsaved edits — Approve will use the edited text.
              </span>
            )}
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
              <tr onClick={() => toggleExpand(item)} style={{ cursor: 'pointer' }}>
                <td>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
                    <span style={{ color: 'var(--muted)' }}>
                      {open ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                    </span>
                    <span>{item.sender || 'unknown'}</span>
                  </span>
                </td>
                <td style={{ fontWeight: 500 }}>{truncate(item.subject, 60)}</td>
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

  // Partition items
  const needsReviewItems = items.filter(isActionable);
  const approvedItems = items.filter(it => it.status === 'approved');
  const dismissedItems = items.filter(it => it.status === 'dismissed');

  const replyItems = needsReviewItems.filter(it => reviewGroup(it) === 'reply');

  // Sub-group Reply by recipientType: to-only (direct to me), to (me in To shared), cc (me in CC), dl (via distribution list), unknown
  const toOnlyItems = replyItems.filter(it => it.recipientType === 'to-only');
  const toItems = replyItems.filter(it => it.recipientType === 'to');
  const ccItems = replyItems.filter(it => it.recipientType === 'cc');
  const dlItems = replyItems.filter(it => it.recipientType === 'dl');
  const unknownItems = replyItems.filter(it => !it.recipientType || it.recipientType === 'unknown');
  const fyiItems = needsReviewItems.filter(it => reviewGroup(it) === 'fyi');
  const classifyingItems = needsReviewItems.filter(it => reviewGroup(it) === 'classifying');

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

      {/* Progress summary */}
      {needsReviewItems.length > 0 && (
        <div style={{ color: 'var(--muted)', fontSize: '0.85em', marginBottom: 'var(--space-md)' }}>
          {needsReviewItems.length} to review
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
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  To me only ({toOnlyItems.length})
                </span>
              </div>
              <div className="card">{renderTable(toOnlyItems)}</div>
            </div>
          )}
          {toItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Me in To ({toItems.length})
                </span>
              </div>
              <div className="card">{renderTable(toItems)}</div>
            </div>
          )}
          {ccItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Me in CC ({ccItems.length})
                </span>
              </div>
              <div className="card">{renderTable(ccItems)}</div>
            </div>
          )}
          {dlItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Via DL ({dlItems.length})
                </span>
              </div>
              <div className="card">{renderTable(dlItems)}</div>
            </div>
          )}
          {unknownItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '0.4em' }}>
                <span style={{ fontWeight: 600, fontSize: '0.9em', color: 'var(--text)' }}>
                  Other ({unknownItems.length})
                </span>
              </div>
              <div className="card">{renderTable(unknownItems)}</div>
            </div>
          )}

          {/* FYI section (collapsible) */}
          {fyiItems.length > 0 && (
            <div style={{ marginBottom: 'var(--space-md)' }}>
              <button type="button" style={collapsibleStyle} onClick={() => setFyiOpen(v => !v)}>
                {fyiOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
                FYI — no reply needed ({fyiItems.length})
              </button>
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
          <button type="button" style={collapsibleStyle} onClick={() => setApprovedOpen(v => !v)}>
            {approvedOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
            Approved ({approvedItems.length})
          </button>
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

      {/* --- Style preferences (collapsible, collapsed by default) --- */}
      {styleLoaded && (
        <div style={{ marginTop: 'var(--space-lg)' }}>
          <button type="button" style={collapsibleStyle} onClick={() => setStyleOpen(v => !v)}>
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
