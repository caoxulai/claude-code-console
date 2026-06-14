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

// Compute the next Date (from now) that matches the expression, scanning forward
// minute-by-minute up to ~366 days. Returns null if it can't parse or find one.
function nextFire(expr) {
  const c = parseCron(expr);
  if (!c) return null;
  const minuteSet = new Set(c.minute);
  const hourSet = new Set(c.hour);
  const domSet = new Set(c.dom);
  const monthSet = new Set(c.month);
  const dowSet = new Set(c.dow);
  const everyDom = c.raw.dom === '*';
  const everyDow = c.raw.dow === '*';

  const d = new Date();
  d.setSeconds(0, 0);
  d.setMinutes(d.getMinutes() + 1); // strictly after now
  const limit = 366 * 24 * 60; // minutes to scan before giving up
  for (let i = 0; i < limit; i++) {
    const month = d.getMonth() + 1;
    const dom = d.getDate();
    const dow = d.getDay();
    // Standard cron: when both DOM and DOW are restricted, either may match.
    let dayOk;
    if (everyDom && everyDow) dayOk = true;
    else if (everyDom) dayOk = dowSet.has(dow);
    else if (everyDow) dayOk = domSet.has(dom);
    else dayOk = domSet.has(dom) || dowSet.has(dow);

    if (monthSet.has(month) && dayOk && hourSet.has(d.getHours()) && minuteSet.has(d.getMinutes())) {
      return new Date(d);
    }
    d.setMinutes(d.getMinutes() + 1);
  }
  return null;
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
  return '';
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

  // New job form state
  const [newCron, setNewCron] = useState('');
  const [newPrompt, setNewPrompt] = useState('');
  const [newRecurring, setNewRecurring] = useState(true);

  // Edit form state
  const [editCron, setEditCron] = useState('');
  const [editPrompt, setEditPrompt] = useState('');
  const [editRecurring, setEditRecurring] = useState(true);

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

  // Fetch run history for a single job. The harness fires these jobs externally
  // and does not report runs, so the endpoint legitimately returns an empty
  // list (or may not exist) — treat any non-array / failure as "no runs".
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
      loadRuns(jobId);
    }
  };

  const createJob = async () => {
    if (!newCron.trim() || !newPrompt.trim()) return;
    try {
      const res = await fetch('/api/crons', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cron: newCron, prompt: newPrompt, recurring: newRecurring }),
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
        body: JSON.stringify({ cron: editCron, prompt: editPrompt, recurring: editRecurring, etag }),
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
    const next = nextFire(job.cron);
    const history = runsByJob[job.id];
    const labelStyle = { fontSize: '0.72em', fontWeight: 600, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '0.3em' };
    const sectionStyle = { marginBottom: 'var(--space-md)' };

    return (
      <div style={{ padding: 'var(--space-md)', background: 'var(--surface2)', borderRadius: 'var(--radius)' }}>
        {/* Full prompt */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Prompt</div>
          <pre style={{
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            fontSize: '0.85em',
            lineHeight: 1.5,
            margin: 0,
            color: 'var(--text)',
            fontFamily: 'monospace',
          }}>{job.prompt}</pre>
        </div>

        {/* Schedule decode + next fire */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Schedule</div>
          <div style={{ fontSize: '0.85em', lineHeight: 1.6 }}>
            <div>
              <span style={{ fontFamily: 'monospace', color: 'var(--accent)' }}>{job.cron}</span>
              {decoded && <span style={{ color: 'var(--muted)' }}> — {decoded}</span>}
              {!decoded && <span style={{ color: 'var(--muted)' }}> — (could not decode this expression)</span>}
            </div>
            <div style={{ color: 'var(--muted)', marginTop: '0.2em' }}>
              Next fire (estimated): {next
                ? <>{next.toLocaleString()} <span>({untilLabel(next)})</span></>
                : 'unknown'}
            </div>
          </div>
        </div>

        {/* Source / metadata */}
        <div style={sectionStyle}>
          <div style={labelStyle}>Source</div>
          <div style={{ fontSize: '0.85em', lineHeight: 1.7, color: 'var(--muted)' }}>
            <div>
              Created: {job.createdAt ? new Date(Number(job.createdAt)).toLocaleString() : '--'}
              {job.createdAt ? ` (${relativeTimeFromMillis(job.createdAt)})` : ''}
            </div>
            <div>
              Mode: <span className={`badge ${job.recurring ? 'badge-ok' : ''}`}>{job.recurring ? 'recurring' : 'once'}</span>
            </div>
            <div>Created by: {job.createdBySessionId || 'console'}</div>
            {/* The harness does not populate lastFiredAt; never imply a run. */}
            <div>Last fired: {job.lastFiredAt ? relativeTime(job.lastFiredAt) : 'never'}</div>
          </div>
        </div>

        {/* Run history */}
        <div>
          <div style={labelStyle}>Run history</div>
          {!history || history.loading ? (
            <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading runs…</div>
          ) : history.runs.length === 0 ? (
            <div className="empty-state" style={{ padding: 'var(--space-md)', textAlign: 'left' }}>
              <p style={{ color: 'var(--muted)', fontSize: '0.85em', margin: 0, lineHeight: 1.6 }}>
                No runs recorded yet — claude-web records a run only when one is triggered
                through the console; the Claude Code scheduler fires these jobs externally and
                does not report run history.
              </p>
            </div>
          ) : (
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              {[...history.runs]
                .sort((a, b) => Number(b.ts ?? 0) - Number(a.ts ?? 0))
                .map((run, i) => {
                  const ts = run.ts;
                  const outcome = run.outcome || 'unknown';
                  return (
                    <div
                      key={run.id || i}
                      style={{
                        padding: '0.6em 0.9em',
                        borderBottom: i < history.runs.length - 1 ? '1px solid var(--border)' : 'none',
                        fontSize: '0.82em',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap' }}>
                        <span className={`badge ${runOutcomeBadgeClass(outcome)}`}>{outcome}</span>
                        <span style={{ color: 'var(--muted)' }}>
                          {ts ? new Date(Number(ts)).toLocaleString() : 'unknown time'}
                        </span>
                      </div>
                      {run.result && (
                        <div style={{ marginTop: '0.3em', color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                          {run.result}
                        </div>
                      )}
                    </div>
                  );
                })}
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
            <button className="btn" onClick={() => { setShowNew(false); setNewCron(''); setNewPrompt(''); setNewRecurring(true); }}>
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
              <tr>
                <th>ID</th>
                <th>Schedule</th>
                <th>Prompt</th>
                <th>Recurring</th>
                <th>Last Fired</th>
                <th>Actions</th>
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
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        checked={editRecurring}
                        onChange={e => setEditRecurring(e.target.checked)}
                      />
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTime(job.lastFiredAt)}</td>
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
                      <td style={{ fontFamily: 'monospace', fontSize: '0.82em' }}>
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
                          <span style={{ color: 'var(--muted)' }}>
                            {expandedId === job.id ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                          </span>
                          {truncate(job.id, 8)}
                        </span>
                      </td>
                      <td style={{ fontFamily: 'monospace' }}>{job.cron}</td>
                      <td title={job.prompt}>{truncate(job.prompt, 80)}</td>
                      <td>
                        <span className={`badge ${job.recurring ? 'badge-ok' : ''}`}>
                          {job.recurring ? 'recurring' : 'once'}
                        </span>
                      </td>
                      <td style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTime(job.lastFiredAt)}</td>
                      <td>
                        <div style={{ display: 'flex', gap: '0.4em' }}>
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
