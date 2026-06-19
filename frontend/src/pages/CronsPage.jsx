import { Fragment, useEffect, useState } from 'react';
import { FiPlus, FiEdit3, FiTrash2, FiSave, FiX, FiChevronDown, FiChevronRight } from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

function relativeTime(iso) {
  if (!iso) return '--';
  const diff = Date.now() - new Date(iso).getTime();
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

// Derive a short 1-2 sentence summary from a prompt when no stored description
// exists. Skips blank lines and shell-comment lines (# ...), takes the first
// meaningful line, prefers its first sentence, and trims to ~140 chars. Pure and
// defensive: never throws on null/empty prompts or prompts that open with shell
// commands or heredocs — worst case it returns a trimmed first line or ''.
function deriveCronSummary(prompt) {
  if (!prompt || typeof prompt !== 'string') return '';
  const lines = prompt.split('\n');
  let candidate = '';
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#')) continue; // shell comment / markdown heading noise
    candidate = line;
    break;
  }
  if (!candidate) candidate = prompt.trim();
  // Prefer the first sentence if one ends within the first line.
  const sentenceMatch = candidate.match(/^(.*?[.!?])(\s|$)/);
  let summary = sentenceMatch ? sentenceMatch[1] : candidate;
  summary = summary.trim();
  return truncate(summary, 140);
}

// Format an epoch-millis timestamp (the harness stores createdAt as ms, not an
// ISO string) into a compact relative label. relativeTime() above takes an ISO
// string, so millis need their own helper to avoid double-parsing.
function relativeTimeFromMillis(ms) {
  if (ms === null || ms === undefined) return '--';
  const num = Number(ms);
  if (!Number.isFinite(num) || num <= 0) return '--';
  return relativeTime(new Date(num).toISOString());
}

const DOW = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

// How many recent run-history records to show per job before "Show all" (mirrors
// SystemCronPage's EXECUTIONS_PREVIEW so both cron tabs cap their history identically).
const EXECUTIONS_PREVIEW = 10;

// --- Cron decoding -----------------------------------------------------------
// Self-contained, dependency-free helpers that turn a 5-field cron expression
// (minute hour day-of-month month day-of-week) into prose and estimate the next
// fire time. Handles the common cases — numeric values, '*', '*/n' steps, and
// comma lists — and falls back to null so callers can show the raw expression
// when it can't be parsed.

// Parse a single cron field into a sorted list of matching integer values for
// the given inclusive [min, max] range. Returns null if unparseable.
function parseField(field, min, max) {
  if (field === '*') {
    const all = [];
    for (let i = min; i <= max; i++) all.push(i);
    return all;
  }
  // Step over the whole range: */n
  const stepMatch = field.match(/^\*\/(\d+)$/);
  if (stepMatch) {
    const step = parseInt(stepMatch[1], 10);
    if (!step) return null;
    const out = [];
    for (let i = min; i <= max; i += step) out.push(i);
    return out;
  }
  // Comma list of plain integers: 0,15,30,45
  const parts = field.split(',');
  const out = [];
  for (const p of parts) {
    if (!/^\d+$/.test(p)) return null;
    const n = parseInt(p, 10);
    if (n < min || n > max) return null;
    out.push(n);
  }
  return Array.from(new Set(out)).sort((a, b) => a - b);
}

// Parse a 5-field expression into resolved value lists, or null on failure.
function parseCron(expr) {
  if (!expr || typeof expr !== 'string') return null;
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const minute = parseField(fields[0], 0, 59);
  const hour = parseField(fields[1], 0, 23);
  const dom = parseField(fields[2], 1, 31);
  const month = parseField(fields[3], 1, 12);
  const dow = parseField(fields[4], 0, 6);
  if (!minute || !hour || !dom || !month || !dow) return null;
  return {
    minute, hour, dom, month, dow,
    raw: { minute: fields[0], hour: fields[1], dom: fields[2], month: fields[3], dow: fields[4] },
  };
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

// Turn an expression into a human-readable sentence, or null if it can't parse.
function decodeCron(expr) {
  const c = parseCron(expr);
  if (!c) return null;

  let timePart;
  // A single minute+hour reads as a clock time; anything more is "every".
  if (c.minute.length === 1 && c.hour.length === 1) {
    timePart = `at ${pad2(c.hour[0])}:${pad2(c.minute[0])}`;
  } else if (c.raw.minute.match(/^\*\/(\d+)$/) && c.raw.hour === '*') {
    timePart = `every ${c.raw.minute.slice(2)} minutes`;
  } else if (c.raw.hour.match(/^\*\/(\d+)$/) && c.minute.length === 1) {
    timePart = `at minute ${c.minute[0]} every ${c.raw.hour.slice(2)} hours`;
  } else if (c.raw.minute === '*' && c.raw.hour === '*') {
    timePart = 'every minute';
  } else {
    const hours = c.hour.map(pad2).join(', ');
    const mins = c.minute.map(pad2).join(', ');
    timePart = `at hour(s) ${hours}, minute(s) ${mins}`;
  }

  let datePart;
  const everyDom = c.raw.dom === '*';
  const everyDow = c.raw.dow === '*';
  if (everyDom && everyDow) {
    datePart = 'every day';
  } else if (!everyDow) {
    datePart = `on ${c.dow.map(d => DOW[d]).join(', ')}`;
  } else {
    datePart = `on day ${c.dom.join(', ')} of the month`;
  }

  const monthPart = c.raw.month === '*' ? '' : ` in month(s) ${c.month.join(', ')}`;

  const sentence = `${timePart.charAt(0).toUpperCase()}${timePart.slice(1)} ${datePart}${monthPart}`;
  return sentence;
}

// Compute the next `count` Dates (from now) that match the expression, scanning
// forward minute-by-minute up to ~366 days. Returns [] if it can't parse or finds
// none. Callers that want a single fire use nextFires(expr, 1)[0] (may be undefined).
//
// Cron fields are matched against UTC components, because the harness scheduler
// runs on the server (a UTC host) and fires on the server's wall clock — NOT the
// viewer's local time. The returned Dates are real instants, so toLocaleString()
// at the call site converts them to the viewer's timezone (e.g. Seattle) for display.
function nextFires(expr, count = 1) {
  const c = parseCron(expr);
  if (!c) return [];
  const minuteSet = new Set(c.minute);
  const hourSet = new Set(c.hour);
  const domSet = new Set(c.dom);
  const monthSet = new Set(c.month);
  const dowSet = new Set(c.dow);
  const everyDom = c.raw.dom === '*';
  const everyDow = c.raw.dow === '*';

  const fires = [];
  const d = new Date();
  d.setUTCSeconds(0, 0);
  d.setUTCMinutes(d.getUTCMinutes() + 1); // strictly after now
  const limit = 366 * 24 * 60; // minutes to scan before giving up
  for (let i = 0; i < limit; i++) {
    const month = d.getUTCMonth() + 1;
    const dom = d.getUTCDate();
    const dow = d.getUTCDay();
    // Standard cron: when both DOM and DOW are restricted, either may match.
    let dayOk;
    if (everyDom && everyDow) dayOk = true;
    else if (everyDom) dayOk = dowSet.has(dow);
    else if (everyDow) dayOk = domSet.has(dom);
    else dayOk = domSet.has(dom) || dowSet.has(dow);

    if (monthSet.has(month) && dayOk && hourSet.has(d.getUTCHours()) && minuteSet.has(d.getUTCMinutes())) {
      fires.push(new Date(d));
      if (fires.length >= count) return fires;
    }
    d.setUTCMinutes(d.getUTCMinutes() + 1);
  }
  return fires;
}

// "in 5m" / "in 3h" style label for an upcoming Date.
function untilLabel(date) {
  const diff = date.getTime() - Date.now();
  if (diff <= 0) return 'now';
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  const days = Math.floor(hrs / 24);
  return `in ${days}d`;
}

function runOutcomeBadgeClass(outcome) {
  if (outcome === 'success') return 'badge-ok';
  if (outcome === 'failure') return 'badge-warn';
  if (outcome === 'fired') return 'badge-fired';
  return '';
}

// Inline style for the 'fired' badge. App.css owns the success/failure badge
// colors (.badge-ok / .badge-warn); this page can't edit App.css, so the
// scheduler-fire badge is styled here with a muted info/blue tone (matching the
// existing .badge-user palette) — visually distinct from the green success and
// amber failure badges, not bright, and comfortable in dark mode.
const FIRED_BADGE_STYLE = { background: '#1a2a3a', color: '#7ab8e6' };
function runOutcomeBadgeStyle(outcome) {
  return outcome === 'fired' ? FIRED_BADGE_STYLE : undefined;
}

// Derive an at-a-glance health pill purely from the reconciled runs + lastFiredAt.
// NEVER fabricates an outcome:
//   healthy     — most recent CONSOLE run (success/failure source) succeeded
//   problem     — most recent console run failed
//   fired       — only scheduled-fire entries, no captured success/failure
//   never-fired — no runs at all and the harness never recorded a fire
// Returns { key, label, className, style } reusing the existing badge palette.
function deriveStatusPill(runs, lastFiredAt) {
  const list = Array.isArray(runs) ? runs : [];
  if (list.length === 0 && !lastFiredAt) {
    return { key: 'never', label: 'never fired', className: 'badge', style: FIRED_BADGE_STYLE };
  }
  const sorted = [...list].sort((a, b) => Number(b.ts ?? 0) - Number(a.ts ?? 0));
  const latestConsole = sorted.find(r => r.outcome === 'success' || r.outcome === 'failure');
  if (latestConsole) {
    if (latestConsole.outcome === 'success') {
      return { key: 'healthy', label: 'healthy', className: 'badge badge-ok', style: undefined };
    }
    return { key: 'problem', label: 'problem', className: 'badge badge-warn', style: undefined };
  }
  // No captured success/failure — only scheduled fires (or just lastFiredAt).
  return { key: 'fired', label: 'fired', className: 'badge badge-fired', style: FIRED_BADGE_STYLE };
}

export default function CronsPage() {
  const [jobs, setJobs] = useState([]);
  const [etag, setEtag] = useState(null);
  const [error, setError] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  // Run-history for the open panel: { [jobId]: { loading, runs } }.
  const [runsByJob, setRunsByJob] = useState({});

  // Whether the full prompt is expanded in the open detail panel. Folded by
  // default; reset whenever a different job is expanded (one job open at a time).
  const [promptExpanded, setPromptExpanded] = useState(false);
  // Whether the reference "Details" block (created / mode / source) is shown.
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  // Per-entry expansion of long run results, keyed by run id/index.
  const [expandedResults, setExpandedResults] = useState({});
  // Jobs whose run-history list is expanded to ALL runs. By default each job
  // shows only the most-recent EXECUTIONS_PREVIEW; "Show all" reveals the rest.
  // Per-job Set (mirrors SystemCronPage's showAllRunsJobs) so other jobs are
  // unaffected; the entry harmlessly persists when a job is collapsed.
  const [showAllRuns, setShowAllRuns] = useState(() => new Set());

  // New job form state
  const [newCron, setNewCron] = useState('');
  const [newPrompt, setNewPrompt] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newRecurring, setNewRecurring] = useState(true);

  // Edit form state
  const [editCron, setEditCron] = useState('');
  const [editPrompt, setEditPrompt] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editRecurring, setEditRecurring] = useState(true);

  // Shared presentation tokens, mirroring SystemCronPage's proven style objects so
  // the two cron tabs read identically. Defined LOCALLY (the pages are independent —
  // we do NOT import styles across pages); scoped here so BOTH the table render and
  // renderDetailPanel can use them. Alignment: the Description column reads as text,
  // so it's left-aligned; every other column is short and centers cleanly. Headers
  // and cells share the same alignment so columns line up (App.css centers all <th>
  // and left-aligns all <td>, so the explicit per-cell override is what makes them track).
  const labelStyle = { fontSize: '0.72em', fontWeight: 600, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '0.3em' };
  const sectionStyle = { marginBottom: 'var(--space-md)' };
  const colLeft = { textAlign: 'left' };
  const colCenter = { textAlign: 'center' };
  // Monospace pre for the Prompt section, matching SystemCronPage's Command pre.
  const preStyle = {
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: '0.85em',
    lineHeight: 1.5,
    margin: 0,
    color: 'var(--text)',
    fontFamily: 'monospace',
  };

  const refresh = async () => {
    try {
      const res = await fetch('/api/crons');
      const json = await res.json();
      setJobs(json.jobs || []);
      setEtag(json.etag);
      setError(null);
    } catch {
      setError('Failed to load cron jobs.');
    }
  };

  // Fetch run history for a single job. The server reconciles two sources into
  // a single newest-first list: console-recorded runs (success/failure) from
  // cron_runs.json AND the harness's lastFiredAt fire timestamp, surfaced as a
  // distinct {outcome:'fired'} entry. We just render what the server returns;
  // treat any non-array / failure as "no runs".
  const loadRuns = async (jobId) => {
    setRunsByJob(prev => ({ ...prev, [jobId]: { loading: true, runs: prev[jobId]?.runs || [] } }));
    try {
      const res = await fetch(`/api/crons/${encodeURIComponent(jobId)}/runs`);
      const json = res.ok ? await res.json() : null;
      const runs = Array.isArray(json) ? json : Array.isArray(json?.runs) ? json.runs : [];
      setRunsByJob(prev => ({ ...prev, [jobId]: { loading: false, runs } }));
    } catch {
      setRunsByJob(prev => ({ ...prev, [jobId]: { loading: false, runs: [] } }));
    }
  };

  useEffect(() => { refresh(); }, []);
  // Don't refetch while the user is mid-edit (would clobber the form); only
  // when not actively editing or creating.
  useLiveUpdates(['cron_changed', 'cron_deleted'], () => {
    if (!editingId && !showNew) refresh();
  });
  // A run was recorded through the console — refresh the open panel's history.
  useLiveUpdates(['cron_run_recorded'], () => {
    if (expandedId) loadRuns(expandedId);
  });

  // Expand/collapse a row. Editing a row takes precedence over expansion.
  const toggleExpand = (jobId) => {
    if (editingId === jobId) return;
    if (expandedId === jobId) {
      setExpandedId(null);
    } else {
      setExpandedId(jobId);
      setPromptExpanded(false);
      setDetailsExpanded(false);
      setExpandedResults({});
      loadRuns(jobId);
    }
  };

  const createJob = async () => {
    if (!newCron.trim() || !newPrompt.trim()) return;
    try {
      const res = await fetch('/api/crons', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cron: newCron, prompt: newPrompt, description: newDescription, recurring: newRecurring }),
      });
      if (!res.ok) {
        setError('Failed to create job.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag);
      setShowNew(false);
      setNewCron('');
      setNewPrompt('');
      setNewDescription('');
      setNewRecurring(true);
      setError(null);
      refresh();
    } catch {
      setError('Failed to create job.');
    }
  };

  const startEdit = (job) => {
    setEditingId(job.id);
    setExpandedId(null); // a row being edited is never also expanded
    setEditCron(job.cron);
    setEditPrompt(job.prompt);
    setEditDescription(job.description || '');
    setEditRecurring(job.recurring);
    setError(null);
  };

  const cancelEdit = () => {
    setEditingId(null);
  };

  const saveEdit = async (id) => {
    try {
      const res = await fetch(`/api/crons/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cron: editCron, prompt: editPrompt, description: editDescription, recurring: editRecurring, etag }),
      });
      if (res.status === 409) {
        setError('Conflict: job was modified externally. Refreshing...');
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to update job.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag);
      setEditingId(null);
      setError(null);
      refresh();
    } catch {
      setError('Failed to update job.');
    }
  };

  const deleteJob = async (id) => {
    if (!confirm('Delete this cron job?')) return;
    try {
      const res = await fetch(`/api/crons/${id}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (!res.ok) {
        setError('Failed to delete job.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag);
      if (expandedId === id) setExpandedId(null);
      setError(null);
      refresh();
    } catch {
      setError('Failed to delete job.');
    }
  };

  // --- Detail panel rendered inside a full-width row when a job is expanded ---
  const renderDetailPanel = (job) => {
    const decoded = decodeCron(job.cron);
    const fires = nextFires(job.cron, 5);
    const next = fires[0];
    const history = runsByJob[job.id];
    const toggleStyle = {
      display: 'inline-flex', alignItems: 'center', gap: '0.35em',
      cursor: 'pointer', color: 'var(--accent)', fontSize: '0.8em', background: 'none',
      border: 'none', padding: 0,
    };

    // Stored description wins; otherwise auto-derive a short summary from the
    // prompt so existing jobs (no description) still read cleanly. Plain text.
    const summary = (job.description && job.description.trim())
      ? job.description.trim()
      : deriveCronSummary(job.prompt);

    // Reconciled, newest-first runs for status pill + last-run line.
    const runs = (history && Array.isArray(history.runs)) ? history.runs : [];
    const runsLoaded = history && !history.loading;
    const sortedRuns = [...runs].sort((a, b) => Number(b.ts ?? 0) - Number(a.ts ?? 0));
    const lastRun = sortedRuns[0];
    const pill = deriveStatusPill(runs, job.lastFiredAt);

    return (
      <div style={{ padding: 'var(--space-md)', background: 'var(--surface2)', borderRadius: 'var(--radius)' }}>
        {/* --- Status at a glance + last run (kept at the very top) ----------- */}
        <div style={sectionStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap' }}>
            <span className={pill.className} style={pill.style}>{pill.label}</span>
            {runsLoaded && lastRun && (
              <span style={{ color: 'var(--muted)', fontSize: '0.82em' }}>
                Last run: {lastRun.outcome || 'unknown'} · {lastRun.ts ? relativeTimeFromMillis(lastRun.ts) : 'unknown time'}
              </span>
            )}
            {runsLoaded && !lastRun && (
              <span style={{ color: 'var(--muted)', fontSize: '0.82em' }}>No runs yet</span>
            )}
          </div>
        </div>

        {/* --- About (summary) ----------------------------------------------- */}
        {summary && (
          <div style={sectionStyle}>
            <div style={labelStyle}>About</div>
            <div style={{ fontSize: '0.85em', lineHeight: 1.6, color: 'var(--text)' }}>{summary}</div>
          </div>
        )}

        {/* --- Prompt (full prompt, folded behind a toggle; collapsed) -------- */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Prompt</div>
          <button
            type="button"
            style={toggleStyle}
            onClick={() => setPromptExpanded(v => !v)}
          >
            {promptExpanded ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
            {promptExpanded ? 'Hide full prompt' : 'Show full prompt'}
          </button>
          {promptExpanded && (
            <pre style={{ ...preStyle, marginTop: '0.4em' }}>{job.prompt}</pre>
          )}
        </div>

        {/* --- Schedule & timing --------------------------------------------- */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Schedule &amp; timing</div>
          <div style={{ fontSize: '0.85em', lineHeight: 1.6 }}>
            <div>
              <span style={{ fontFamily: 'monospace', color: 'var(--accent)' }}>{job.cron}</span>
              {decoded && <span style={{ color: 'var(--muted)' }}> — {decoded} (UTC)</span>}
              {!decoded && <span style={{ color: 'var(--muted)' }}> — (could not decode this expression)</span>}
            </div>
            <div style={{ color: 'var(--muted)', marginTop: '0.2em' }}>
              Next fire (earliest matching time): {next
                ? <>{next.toLocaleString(undefined, { timeZoneName: 'short' })} <span>({untilLabel(next)})</span></>
                : 'unknown'}
            </div>
            {fires.length > 0 && (
              <div style={{ color: 'var(--muted)', marginTop: '0.4em' }}>
                Upcoming fire times:
                <ul style={{ margin: '0.25em 0 0', paddingLeft: '1.2em' }}>
                  {fires.map((d, i) => (
                    <li key={i}>{d.toLocaleString(undefined, { timeZoneName: 'short' })}</li>
                  ))}
                </ul>
              </div>
            )}
            {/* Make the scheduled-vs-actual drift obvious when the job has fired. */}
            {job.lastFiredAt && (
              <div style={{ color: 'var(--muted)', marginTop: '0.2em' }}>
                Last actually fired: {new Date(Number(job.lastFiredAt)).toLocaleString(undefined, { timeZoneName: 'short' })}
                {' '}({relativeTimeFromMillis(job.lastFiredAt)})
              </div>
            )}
            <div style={{ color: 'var(--muted)', marginTop: '0.2em', fontSize: '0.92em', fontStyle: 'italic' }}>
              The scheduler may fire later than this — it runs jobs opportunistically
              (e.g. when polled or when a session is active), not at the exact minute.
            </div>
          </div>
        </div>

        {/* --- Run history --------------------------------------------------- */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Run history{runsLoaded ? ` (${runs.length})` : ''}</div>
          {!history || history.loading ? (
            <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading runs…</div>
          ) : runs.length === 0 ? (
            <div className="empty-state" style={{ padding: 'var(--space-md)', textAlign: 'left' }}>
              <p style={{ color: 'var(--muted)', fontSize: '0.85em', margin: 0, lineHeight: 1.6 }}>
                No runs recorded yet — this job has not fired on schedule and no run has been
                triggered through the console. Scheduled fires appear here once the scheduler
                runs the job.
              </p>
            </div>
          ) : (() => {
            // Cap to the most-recent EXECUTIONS_PREVIEW unless the user expanded
            // this job (mirrors SystemCronPage). sortedRuns is already newest-first.
            const showAll = showAllRuns.has(job.id);
            const visibleRuns = showAll ? sortedRuns : sortedRuns.slice(0, EXECUTIONS_PREVIEW);
            return (
            <>
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              {visibleRuns.map((run, i) => {
                const ts = run.ts;
                const outcome = run.outcome || 'unknown';
                const key = run.id || i;
                const result = run.result || '';
                const longResult = result.length > 200;
                const resultOpen = !!expandedResults[key];
                return (
                  <div
                    key={key}
                    style={{
                      padding: '0.6em 0.9em',
                      borderBottom: i < visibleRuns.length - 1 ? '1px solid var(--border)' : 'none',
                      fontSize: '0.82em',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap' }}>
                      <span className={`badge ${runOutcomeBadgeClass(outcome)}`} style={runOutcomeBadgeStyle(outcome)}>{outcome}</span>
                      <span style={{ color: 'var(--muted)' }}>
                        {ts ? new Date(Number(ts)).toLocaleString() : 'unknown time'}
                      </span>
                    </div>
                    {result ? (
                      longResult ? (
                        <div style={{ marginTop: '0.3em' }}>
                          <button
                            type="button"
                            style={toggleStyle}
                            onClick={() => setExpandedResults(prev => ({ ...prev, [key]: !prev[key] }))}
                          >
                            {resultOpen ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
                            {resultOpen ? 'Hide result' : 'Show result'}
                          </button>
                          {resultOpen && (
                            <div style={{ marginTop: '0.3em', color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                              {result}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div style={{ marginTop: '0.3em', color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {result}
                        </div>
                      )
                    ) : outcome === 'fired' ? (
                      // A scheduled fire the harness recorded with no output we could
                      // harvest. Never fabricate a success/failure — show an explicit
                      // muted note that the result wasn't captured.
                      <div style={{ marginTop: '0.3em', color: 'var(--muted)', fontStyle: 'italic' }}>
                        fired (result not captured by the scheduler)
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
            {sortedRuns.length > EXECUTIONS_PREVIEW && (
              <div
                onClick={() => setShowAllRuns(prev => {
                  const next = new Set(prev);
                  if (next.has(job.id)) next.delete(job.id); else next.add(job.id);
                  return next;
                })}
                style={{ cursor: 'pointer', color: 'var(--accent)', fontSize: '0.82em', marginTop: '0.5em' }}
              >
                {showAll ? 'Show less' : `Show all ${sortedRuns.length} executions (${sortedRuns.length - visibleRuns.length} more)`}
              </div>
            )}
            </>
            );
          })()}
        </div>

        {/* --- Details (reference metadata, collapsed) ----------------------- */}
        <div>
          <button
            type="button"
            style={toggleStyle}
            onClick={() => setDetailsExpanded(v => !v)}
          >
            {detailsExpanded ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
            {detailsExpanded ? 'Hide details' : 'Show details'}
          </button>
          {detailsExpanded && (
            <div style={{ fontSize: '0.85em', lineHeight: 1.7, color: 'var(--muted)', marginTop: '0.4em' }}>
              <div>ID: <span style={{ fontFamily: 'monospace' }}>{job.id}</span></div>
              <div>
                Created: {job.createdAt ? new Date(Number(job.createdAt)).toLocaleString() : '--'}
                {job.createdAt ? ` (${relativeTimeFromMillis(job.createdAt)})` : ''}
              </div>
              <div>
                Mode: <span className={`badge ${job.recurring ? 'badge-ok' : ''}`}>{job.recurring ? 'recurring' : 'once'}</span>
              </div>
              <div>Created by: {job.createdBySessionId || 'console'}</div>
              {/* The harness DOES populate lastFiredAt — a real fire timestamp in
                  epoch millis, written when the scheduler runs the job. */}
              <div>Last fired: {job.lastFiredAt ? relativeTimeFromMillis(job.lastFiredAt) : 'never'}</div>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div>
      <div className="page-header">
        <h2>Cron Jobs</h2>
        <button className="btn btn-primary" onClick={() => { setShowNew(!showNew); setError(null); }}>
          <FiPlus size={14} /> New Job
        </button>
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {showNew && (
        <div className="card" style={{ marginBottom: '1em' }}>
          <div className="card-title">New Cron Job</div>
          <div className="form-group">
            <label className="form-label">Cron Expression</label>
            <input
              className="form-input"
              placeholder="*/5 * * * *"
              value={newCron}
              onChange={e => setNewCron(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label">Prompt</label>
            <textarea
              className="form-textarea"
              placeholder="What should Claude do on this schedule?"
              value={newPrompt}
              onChange={e => setNewPrompt(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label">Description (optional)</label>
            <input
              className="form-input"
              placeholder="A 1–2 sentence summary of what this job does"
              value={newDescription}
              onChange={e => setNewDescription(e.target.value)}
            />
          </div>
          <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
            <input
              type="checkbox"
              id="new-recurring"
              checked={newRecurring}
              onChange={e => setNewRecurring(e.target.checked)}
            />
            <label htmlFor="new-recurring" style={{ fontSize: '0.88em' }}>Recurring</label>
          </div>
          <div style={{ display: 'flex', gap: '0.5em' }}>
            <button className="btn btn-primary" onClick={createJob}><FiSave size={14} /> Save</button>
            <button className="btn" onClick={() => { setShowNew(false); setNewCron(''); setNewPrompt(''); setNewDescription(''); setNewRecurring(true); }}>
              <FiX size={14} /> Cancel
            </button>
          </div>
        </div>
      )}

      <div className="card">
        {jobs.length === 0 ? (
          <div className="empty-state">
            <h3>No cron jobs</h3>
            <p>Create a job to run prompts on a schedule.</p>
          </div>
        ) : (
          <table className="data-table">
            <thead>
              {/* Description left-aligned (it's text); the rest centered. Headers
                  match their cells' alignment so columns line up. */}
              <tr>
                <th style={colLeft}>Description</th>
                <th style={colCenter}>Schedule</th>
                <th style={colCenter}>Next run</th>
                <th style={colCenter}>Last run</th>
                <th style={colCenter}>Status</th>
                <th style={colCenter}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map(job => (
                editingId === job.id ? (
                  <tr key={job.id}>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.82em' }}>{truncate(job.id, 8)}</td>
                    <td>
                      <input
                        className="form-input"
                        value={editCron}
                        onChange={e => setEditCron(e.target.value)}
                        style={{ width: '140px' }}
                      />
                    </td>
                    <td>
                      <textarea
                        className="form-textarea"
                        value={editPrompt}
                        onChange={e => setEditPrompt(e.target.value)}
                        style={{ minHeight: '60px', width: '100%' }}
                      />
                      <input
                        className="form-input"
                        placeholder="Description (optional) — 1–2 sentence summary"
                        value={editDescription}
                        onChange={e => setEditDescription(e.target.value)}
                        style={{ width: '100%', marginTop: '0.4em' }}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        checked={editRecurring}
                        onChange={e => setEditRecurring(e.target.checked)}
                      />
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTimeFromMillis(job.lastFiredAt)}</td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.4em' }}>
                        <button className="btn btn-primary" onClick={() => saveEdit(job.id)} title="Save">
                          <FiSave size={13} />
                        </button>
                        <button className="btn" onClick={cancelEdit} title="Cancel">
                          <FiX size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ) : (
                  <Fragment key={job.id}>
                    <tr
                      onClick={() => toggleExpand(job.id)}
                      style={{ cursor: 'pointer' }}
                    >
                      <td style={colLeft}>
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
                          <span style={{ color: 'var(--muted)' }}>
                            {expandedId === job.id ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                          </span>
                          {job.description?.trim() || deriveCronSummary(job.prompt) || '(no description)'}
                        </span>
                      </td>
                      <td style={{ ...colCenter, fontFamily: 'monospace' }}>{job.cron}</td>
                      {/* Row pill derives from lastFiredAt alone (empty runs) so the
                          table never eagerly fetches every job's runs; see Next run. */}
                      {(() => {
                        const next = nextFires(job.cron, 1)[0];
                        const pill = deriveStatusPill([], job.lastFiredAt);
                        return (
                          <>
                            <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }}>
                              {next ? untilLabel(next) : 'unknown'}
                            </td>
                            <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }}>
                              {relativeTimeFromMillis(job.lastFiredAt)}
                            </td>
                            <td style={colCenter}>
                              <span className={pill.className} style={pill.style}>{pill.label}</span>
                            </td>
                          </>
                        );
                      })()}
                      <td style={colCenter}>
                        <div style={{ display: 'flex', gap: '0.4em', justifyContent: 'center' }}>
                          <button className="btn" onClick={(e) => { e.stopPropagation(); startEdit(job); }} title="Edit">
                            <FiEdit3 size={13} />
                          </button>
                          <button className="btn btn-danger" onClick={(e) => { e.stopPropagation(); deleteJob(job.id); }} title="Delete">
                            <FiTrash2 size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                    {expandedId === job.id && (
                      <tr>
                        <td colSpan={6} style={{ padding: 0, background: 'var(--surface2)' }}>
                          {renderDetailPanel(job)}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
