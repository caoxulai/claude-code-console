import { Fragment, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FiChevronDown, FiChevronRight, FiRefreshCw, FiCalendar } from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

// Format epoch-millis into a local timestamp WITH a short timezone label, mirroring
// CronsPage's `new Date(Number(ms)).toLocaleString(undefined, { timeZoneName: 'short' })`
// pattern. Returns null for missing/invalid — callers render an explicit fallback,
// never a fabricated time.
function fmtWhen(ms) {
  if (ms === null || ms === undefined) return null;
  const num = Number(ms);
  if (!Number.isFinite(num) || num <= 0) return null;
  return new Date(num).toLocaleString(undefined, { timeZoneName: 'short' });
}

// Compact relative label ("in 5m" / "3h ago") from epoch millis, or null.
function relativeTimeFromMillis(ms) {
  if (ms === null || ms === undefined) return null;
  const num = Number(ms);
  if (!Number.isFinite(num) || num <= 0) return null;
  const diff = num - Date.now();
  const future = diff > 0;
  const mins = Math.floor(Math.abs(diff) / 60000);
  if (mins < 1) return future ? 'in <1m' : 'just now';
  if (mins < 60) return future ? `in ${mins}m` : `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return future ? `in ${hrs}h` : `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return future ? `in ${days}d` : `${days}d ago`;
}

// Muted info-blue palette (same tone as CronsPage's FIRED_BADGE_STYLE / .badge-user):
// distinct from green success / amber failure, not bright, dark-mode-comfortable.
const INFO_BADGE_STYLE = { background: '#1a2a3a', color: '#7ab8e6' };

// How many recent executions to show per job before "Show all".
const EXECUTIONS_PREVIEW = 10;

// Humanize a duration in milliseconds: "47s", "2m13s", "1h3m". Returns "—" for
// null/invalid so a run with no measurable span never shows a fabricated number.
function fmtDuration(ms) {
  if (ms === null || ms === undefined) return '—';
  const num = Number(ms);
  if (!Number.isFinite(num) || num < 0) return '—';
  const totalSec = Math.round(num / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  if (mins < 60) return secs ? `${mins}m${secs}s` : `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins ? `${hrs}h${remMins}m` : `${hrs}h`;
}

// Status → badge presentation, driven ONLY by backend-derived evidence. We never
// upgrade "running"/"unknown" to a success/failure the logs didn't state.
function runStatusBadge(status) {
  if (status === 'succeeded') return { label: 'succeeded', className: 'badge badge-ok', style: undefined };
  if (status === 'failed') return { label: 'failed', className: 'badge badge-warn', style: undefined };
  if (status === 'running') return { label: 'running', className: 'badge', style: INFO_BADGE_STYLE };
  return { label: 'unknown', className: 'badge', style: { color: 'var(--muted)' } };
}

// Run-history badge driven ONLY by the backend `runHistory` field. We never imply a
// captured outcome cron didn't record: logs_only ⇒ "logs available", everything else
// (none/unknown) ⇒ an explicit "discovered — no run history".
function runHistoryBadge(runHistory) {
  if (runHistory === 'logs_only') {
    return { label: 'logs available', className: 'badge badge-ok', style: undefined };
  }
  return { label: 'discovered — no run history', className: 'badge', style: INFO_BADGE_STYLE };
}

function sourceLabel(source) {
  if (source === 'crontab') return 'crontab';
  if (source === 'systemd') return 'systemd timer';
  return source || 'unknown';
}

export default function SystemCronPage() {
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);
  // System/fleet timers are platform-provided noise for most users, so the section
  // is collapsed by default; the user's own crontab schedules show prominently.
  const [showSystem, setShowSystem] = useState(false);

  // Lazy-fetched log tail for the open panel: { [jobId]: { loading, lines, path, error } }.
  const [logsByJob, setLogsByJob] = useState({});

  // Lazy-fetched execution history per job:
  //   { [jobId]: { loading, runs, parsed, source, error } }.
  // parsed:false (or empty/errored) ⇒ the UI falls back to the single tail panel.
  const [runsByJob, setRunsByJob] = useState({});

  // Which execution row's log slice is open (one at a time), keyed `${jobId}:${idx}`,
  // and the lazily-fetched slice for it: { [runKey]: { loading, lines, path, error, truncated } }.
  const [openRunKey, setOpenRunKey] = useState(null);
  const [runSliceByKey, setRunSliceByKey] = useState({});

  // Jobs whose execution list is expanded to ALL runs. By default each job shows
  // only the most recent EXECUTIONS_PREVIEW; "Show all" reveals the rest.
  const [showAllRunsJobs, setShowAllRunsJobs] = useState(() => new Set());

  // GET /api/oscron — the read-only OS schedule (user crontab + systemd timers),
  // parsed deterministically by the backend. No mutating calls exist on this page.
  const refresh = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/oscron');
      const json = res.ok ? await res.json() : null;
      setJobs(Array.isArray(json?.jobs) ? json.jobs : []);
      setError(null);
    } catch {
      setError('Failed to load the OS schedule.');
    } finally {
      setLoading(false);
    }
  };

  // Lazily fetch the tail of a job's redirect log on expand. Surfaces only what the
  // file actually contains; an empty/missing/unreadable log becomes an explicit
  // "not available" state below — never a fabricated outcome.
  const loadLog = async (jobId) => {
    setLogsByJob(prev => ({ ...prev, [jobId]: { loading: true } }));
    try {
      const res = await fetch(`/api/oscron/${encodeURIComponent(jobId)}/log?tail=200`);
      const json = res.ok ? await res.json() : null;
      setLogsByJob(prev => ({
        ...prev,
        [jobId]: {
          loading: false,
          lines: Array.isArray(json?.lines) ? json.lines : [],
          path: json?.path || null,
          truncated: !!json?.truncated,
          error: json?.error || (res.ok ? null : 'not available'),
        },
      }));
    } catch {
      setLogsByJob(prev => ({ ...prev, [jobId]: { loading: false, lines: [], path: null, error: 'not available' } }));
    }
  };

  // Lazily fetch the honest execution history on expand. The backend segments the
  // crontab redirect log / journald invocations into runs and returns parsed:false
  // when run boundaries aren't detectable — in which case we keep the tail view.
  // A 404 / non-ok / throw is treated as parsed:false so an unbuilt or unavailable
  // endpoint degrades to the existing tail rather than erroring the panel.
  const loadRuns = async (jobId) => {
    setRunsByJob(prev => ({ ...prev, [jobId]: { loading: true } }));
    try {
      const res = await fetch(`/api/oscron/${encodeURIComponent(jobId)}/runs`);
      const json = res.ok ? await res.json() : null;
      setRunsByJob(prev => ({
        ...prev,
        [jobId]: {
          loading: false,
          runs: Array.isArray(json?.runs) ? json.runs : [],
          parsed: !!json?.parsed,
          source: json?.source || null,
          error: res.ok ? null : 'not available',
        },
      }));
    } catch {
      setRunsByJob(prev => ({ ...prev, [jobId]: { loading: false, runs: [], parsed: false, source: null, error: 'not available' } }));
    }
  };

  // Lazily fetch just one execution's log slice (the line range between this run's
  // start and the next run's start). Same {lines,path,truncated,error} shape as the
  // tail endpoint; an empty/missing slice becomes an explicit "not available" below.
  const loadRunSlice = async (jobId, runKey, run) => {
    setRunSliceByKey(prev => ({ ...prev, [runKey]: { loading: true } }));
    try {
      const params = new URLSearchParams();
      if (run.lineStart !== null && run.lineStart !== undefined) params.set('lineStart', String(run.lineStart));
      if (run.lineEnd !== null && run.lineEnd !== undefined) params.set('lineEnd', String(run.lineEnd));
      const res = await fetch(`/api/oscron/${encodeURIComponent(jobId)}/log?${params.toString()}`);
      const json = res.ok ? await res.json() : null;
      setRunSliceByKey(prev => ({
        ...prev,
        [runKey]: {
          loading: false,
          lines: Array.isArray(json?.lines) ? json.lines : [],
          path: json?.path || null,
          truncated: !!json?.truncated,
          error: json?.error || (res.ok ? null : 'not available'),
        },
      }));
    } catch {
      setRunSliceByKey(prev => ({ ...prev, [runKey]: { loading: false, lines: [], path: null, error: 'not available' } }));
    }
  };

  useEffect(() => { refresh(); }, []);
  // oscron is read-only with no dedicated broadcast events yet; a mount fetch plus a
  // manual Refresh is sufficient. Subscribe to nothing so the shared socket isn't held
  // open just for this page, and so we don't invent event names the backend never emits.
  useLiveUpdates([], () => {});

  // One row open at a time. Opening a row lazily fetches its execution history
  // (and, as a fallback for the parsed:false case, its log tail). Switching rows
  // closes any open per-run log slice so stale state doesn't leak across jobs.
  const toggleExpand = (jobId) => {
    if (expandedId === jobId) {
      setExpandedId(null);
      setOpenRunKey(null);
    } else {
      setExpandedId(jobId);
      setOpenRunKey(null);
      if (!runsByJob[jobId]) loadRuns(jobId);
      if (!logsByJob[jobId]) loadLog(jobId);
    }
  };

  // Toggle one execution's log slice open (one at a time across the whole page).
  const toggleRun = (jobId, idx, run) => {
    const runKey = `${jobId}:${idx}`;
    if (openRunKey === runKey) {
      setOpenRunKey(null);
    } else {
      setOpenRunKey(runKey);
      if (!runSliceByKey[runKey]) loadRunSlice(jobId, runKey, run);
    }
  };

  const labelStyle = { fontSize: '0.72em', fontWeight: 600, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: '0.3em' };
  const sectionStyle = { marginBottom: 'var(--space-md)' };
  // Alignment: the Description column reads as text, so it's left-aligned; every
  // other column (source/run-history badges, schedule, next/last run) is short and
  // centers cleanly. Headers and cells share the same alignment so they line up.
  const colLeft = { textAlign: 'left' };
  const colCenter = { textAlign: 'center' };

  // Shared monospace scrollable log style, reused by the tail panel and per-run slices
  // so they look identical.
  const preStyle = {
    maxHeight: '320px',
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: '0.82em',
    lineHeight: 1.5,
    margin: 0,
    padding: '0.6em 0.8em',
    background: 'var(--surface2)',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius)',
    color: 'var(--text)',
    fontFamily: 'monospace',
  };

  // Split jobs into "yours" (user crontab entries you created) vs the platform-provided
  // systemd timers (stock OS housekeeping + Amazon fleet agents) — neither set is
  // created by this dashboard, but the crontab line is the one schedule you authored.
  const mineJobs = jobs.filter(j => j.source === 'crontab');
  const systemJobs = jobs.filter(j => j.source !== 'crontab');

  // --- Last-execution log panel, lazy-fetched on expand ----------------------
  const renderLogPanel = (job) => {
    const log = logsByJob[job.id];
    if (!log || log.loading) {
      return (
        <>
          <div style={labelStyle}>Last execution result</div>
          <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading last execution result…</div>
        </>
      );
    }
    const lines = Array.isArray(log.lines) ? log.lines : [];
    if (log.error || lines.length === 0) {
      // cron records no result; a missing/empty redirect log means we genuinely have
      // nothing to show. Be explicit rather than implying a success/failure.
      return (
        <>
          <div style={labelStyle}>Last execution result</div>
          <div style={{ color: 'var(--muted)', fontSize: '0.85em', fontStyle: 'italic' }}>
            Result not captured — no execution output available
            {log.path ? <span> (no readable log at {log.path}).</span> : '.'}
          </div>
        </>
      );
    }
    return (
      <div>
        <div style={labelStyle}>Last execution result</div>
        {log.path && (
          <div style={{ color: 'var(--muted)', fontSize: '0.78em', marginBottom: '0.3em' }}>
            tail of {log.path}{log.truncated ? ' (truncated)' : ''}
          </div>
        )}
        <pre style={preStyle}>{lines.join('\n')}</pre>
      </div>
    );
  };

  // --- Per-run log slice, lazy-fetched when an execution row is clicked ------
  const renderRunSlice = (runKey) => {
    const slice = runSliceByKey[runKey];
    if (!slice || slice.loading) {
      return <div style={{ color: 'var(--muted)', fontSize: '0.85em', marginTop: '0.4em' }}>Loading this run's log…</div>;
    }
    const lines = Array.isArray(slice.lines) ? slice.lines : [];
    if (slice.error || lines.length === 0) {
      return (
        <div style={{ color: 'var(--muted)', fontSize: '0.82em', fontStyle: 'italic', marginTop: '0.4em' }}>
          No log output captured for this run.
        </div>
      );
    }
    return (
      <pre style={{ ...preStyle, marginTop: '0.4em' }}>{lines.join('\n')}</pre>
    );
  };

  // --- Executions list (most-recent-first), lazy-fetched on expand ----------
  // Rendered only when the backend reports parsed:true with at least one run; the
  // caller falls back to renderLogPanel otherwise. Each row is clickable to reveal
  // just that run's log slice. Statuses come solely from backend evidence.
  const renderExecutions = (job, runs) => {
    // Most-recent-first. Backend ordering isn't assumed; sort by startedAt desc,
    // keeping originally-missing-start runs last but stable.
    const ordered = runs
      .map((run, origIdx) => ({ run, origIdx }))
      .sort((a, b) => (Number(b.run.startedAt) || 0) - (Number(a.run.startedAt) || 0));
    // Cap to the most recent EXECUTIONS_PREVIEW unless the user expanded this job.
    const showAll = showAllRunsJobs.has(job.id);
    const visible = showAll ? ordered : ordered.slice(0, EXECUTIONS_PREVIEW);
    const hiddenCount = ordered.length - visible.length;
    return (
      <div>
        <div style={{ ...labelStyle, marginBottom: '0.5em' }}>Executions ({runs.length})</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3em' }}>
          {visible.map(({ run, origIdx }) => {
            const runKey = `${job.id}:${origIdx}`;
            const isOpen = openRunKey === runKey;
            const badge = runStatusBadge(run.status);
            const startWhen = fmtWhen(run.startedAt);
            const startRel = relativeTimeFromMillis(run.startedAt);
            const finishWhen = fmtWhen(run.finishedAt);
            const endLabel = finishWhen || (run.status === 'running' ? 'running' : '—');
            return (
              <div
                key={runKey}
                style={{
                  border: '1px solid var(--border)',
                  borderRadius: 'var(--radius)',
                  background: 'var(--surface2)',
                  padding: '0.25em 0.7em',
                }}
              >
                <div
                  onClick={() => toggleRun(job.id, origIdx, run)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.6em',
                    cursor: 'pointer',
                    flexWrap: 'wrap',
                    fontSize: '0.84em',
                  }}
                >
                  <span style={{ color: 'var(--muted)' }}>
                    {isOpen ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
                  </span>
                  <span style={{ color: 'var(--text)' }}>
                    {startWhen || 'start unknown'}
                    {startRel && <span style={{ color: 'var(--muted)' }}> ({startRel})</span>}
                  </span>
                  <span style={{ color: 'var(--muted)' }}>→ {endLabel}</span>
                  <span style={{ color: 'var(--muted)' }}>· {fmtDuration(run.durationMs)}</span>
                  {run.trigger && (
                    <span style={{ color: 'var(--muted)' }}>· {run.trigger}</span>
                  )}
                  <span className={badge.className} style={{ ...(badge.style || {}), marginLeft: 'auto' }}>{badge.label}</span>
                </div>
                {isOpen && renderRunSlice(runKey)}
              </div>
            );
          })}
        </div>
        {ordered.length > EXECUTIONS_PREVIEW && (
          <div
            onClick={() => setShowAllRunsJobs(prev => {
              const next = new Set(prev);
              if (next.has(job.id)) next.delete(job.id); else next.add(job.id);
              return next;
            })}
            style={{ cursor: 'pointer', color: 'var(--accent)', fontSize: '0.82em', marginTop: '0.5em' }}
          >
            {showAll ? 'Show less' : `Show all ${ordered.length} executions (${hiddenCount} more)`}
          </div>
        )}
      </div>
    );
  };

  // Chooses the Executions list (parsed runs) vs the single tail panel fallback.
  const renderExecutionsPanel = (job) => {
    const hist = runsByJob[job.id];
    if (hist && hist.loading) {
      return <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading executions…</div>;
    }
    const runs = Array.isArray(hist?.runs) ? hist.runs : [];
    if (hist && hist.parsed && runs.length > 0) {
      return renderExecutions(job, runs);
    }
    // parsed:false / empty / errored / endpoint unavailable ⇒ existing tail view.
    return renderLogPanel(job);
  };

  // --- Detail panel rendered in a full-width row when a job is expanded ------
  const renderDetailPanel = (job) => {
    const upcoming = Array.isArray(job.nextRuns) ? job.nextRuns.filter(Boolean) : [];
    return (
      <div style={{ padding: 'var(--space-md)', background: 'var(--surface2)', borderRadius: 'var(--radius)' }}>
        {job.description && (
          <div style={sectionStyle}>
            <div style={labelStyle}>About</div>
            <div style={{ fontSize: '0.85em', lineHeight: 1.6, color: 'var(--text)' }}>{job.description}</div>
          </div>
        )}

        <div style={sectionStyle}>
          <div style={labelStyle}>Command</div>
          <pre style={{
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            fontSize: '0.85em',
            lineHeight: 1.5,
            margin: 0,
            color: 'var(--text)',
            fontFamily: 'monospace',
          }}>{job.command || '—'}</pre>
        </div>

        <div style={sectionStyle}>
          <div style={labelStyle}>Schedule &amp; timing</div>
          <div style={{ fontSize: '0.85em', lineHeight: 1.6 }}>
            <div>
              <span style={{ fontFamily: 'monospace', color: 'var(--accent)' }}>{job.schedule || '—'}</span>
              {job.cronTz && <span style={{ color: 'var(--muted)' }}> ({job.cronTz})</span>}
            </div>
            {job.schedule === '@reboot' ? (
              <div style={{ color: 'var(--muted)', marginTop: '0.2em' }}>
                Fires on reboot only
              </div>
            ) : (
              <div style={{ color: 'var(--muted)', marginTop: '0.2em' }}>
                Next run: {fmtWhen(job.nextRun)
                  ? <>{fmtWhen(job.nextRun)} <span>({relativeTimeFromMillis(job.nextRun)})</span></>
                  : 'unknown'}
              </div>
            )}
            <div style={{ color: 'var(--muted)', marginTop: '0.2em' }}>
              Last run: {fmtWhen(job.lastRun)
                ? <>{fmtWhen(job.lastRun)} <span>({relativeTimeFromMillis(job.lastRun)})</span></>
                : 'not recorded'}
            </div>
            {job.schedule !== '@reboot' && upcoming.length > 0 && (
              <div style={{ color: 'var(--muted)', marginTop: '0.4em' }}>
                Upcoming fire times:
                <ul style={{ margin: '0.25em 0 0', paddingLeft: '1.2em' }}>
                  {upcoming.map((ms, i) => (
                    <li key={i}>{fmtWhen(ms) || 'unknown'}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>

        <div>
          {renderExecutionsPanel(job)}
        </div>
      </div>
    );
  };

  // Renders one job table. Shared by both the "yours" and "system" groups so the
  // row/expand/badge behavior stays identical across sections.
  const renderJobTable = (rows) => (
    <table className="data-table">
      <thead>
        {/* Description left-aligned (it's text); the rest centered. Headers match
            their cells' alignment so columns line up. */}
        <tr>
          <th style={colLeft}>Description</th>
          <th style={colCenter}>Source</th>
          <th style={colCenter}>Schedule</th>
          <th style={colCenter}>Next run</th>
          <th style={colCenter}>Last run</th>
          <th style={colCenter}>Run history</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(job => {
          const hist = runHistoryBadge(job.runHistory);
          const nextWhen = fmtWhen(job.nextRun);
          const lastWhen = fmtWhen(job.lastRun);
          return (
            <Fragment key={job.id}>
              <tr onClick={() => toggleExpand(job.id)} style={{ cursor: 'pointer' }}>
                <td style={colLeft}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
                    <span style={{ color: 'var(--muted)' }}>
                      {expandedId === job.id ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
                    </span>
                    {job.title || job.description || job.unit || job.command || '(no description)'}
                  </span>
                </td>
                <td style={colCenter}>
                  <span className="badge" style={INFO_BADGE_STYLE}>{sourceLabel(job.source)}</span>
                </td>
                <td style={{ ...colCenter, fontFamily: 'monospace', fontSize: '0.85em' }}>{job.schedule || '—'}</td>
                <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }} title={job.schedule === '@reboot' ? 'fires once at boot' : (nextWhen || '')}>
                  {job.schedule === '@reboot'
                    ? <span style={{ color: 'var(--muted)' }}>on reboot</span>
                    : (nextWhen ? relativeTimeFromMillis(job.nextRun) : 'unknown')}
                </td>
                <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }} title={lastWhen || ''}>
                  {lastWhen ? relativeTimeFromMillis(job.lastRun) : 'not recorded'}
                </td>
                <td style={colCenter}>
                  <span className={hist.className} style={hist.style}>{hist.label}</span>
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
          );
        })}
      </tbody>
    </table>
  );

  return (
    <div>
      <div className="page-header">
        <h2>System Cron</h2>
        <button className="btn" onClick={refresh} title="Refresh">
          <FiRefreshCw size={14} /> Refresh
        </button>
      </div>

      <div className="card" style={{ marginBottom: '1em' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: '0.4em' }}>
          <FiCalendar size={15} style={{ color: 'var(--accent)' }} />
          <strong>Reliable — runs unattended whether or not a Claude session is active.</strong>
        </div>
        <p style={{ color: 'var(--muted)', fontSize: '0.88em', lineHeight: 1.6, margin: 0 }}>
          These are the OS-level schedulers actually running on this box: your user crontab and
          systemd timers. They fire on the operating system's clock regardless of the harness.
          By contrast, the harness <Link to="/crons">Cron Jobs</Link> tab only fires while a
          Claude session is active and its recurring tasks expire after about 7 days.
        </p>
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {loading ? (
        <div className="card">
          <div className="empty-state">
            <p style={{ color: 'var(--muted)' }}>Loading OS schedule…</p>
          </div>
        </div>
      ) : jobs.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <h3>No OS schedulers found</h3>
            <p>No user crontab entries or systemd timers were discovered on this box.</p>
          </div>
        </div>
      ) : (
        <>
          <div className="card" style={{ marginBottom: '1em' }}>
            <h3 style={{ margin: '0 0 0.2em' }}>Your schedules</h3>
            <p style={{ color: 'var(--muted)', fontSize: '0.85em', margin: '0 0 0.8em' }}>
              Cron jobs in your user crontab — the schedules you set up on this box.
            </p>
            {mineJobs.length === 0 ? (
              <div className="empty-state" style={{ padding: 'var(--space-md)' }}>
                <p style={{ color: 'var(--muted)' }}>No user crontab entries found.</p>
              </div>
            ) : renderJobTable(mineJobs)}
          </div>

          <div className="card">
            <div
              onClick={() => setShowSystem(v => !v)}
              style={{ display: 'flex', alignItems: 'center', gap: '0.5em', cursor: 'pointer' }}
            >
              <span style={{ color: 'var(--muted)' }}>
                {showSystem ? <FiChevronDown size={15} /> : <FiChevronRight size={15} />}
              </span>
              <h3 style={{ margin: 0 }}>System &amp; fleet timers</h3>
              <span className="badge" style={INFO_BADGE_STYLE}>{systemJobs.length}</span>
            </div>
            <p style={{ color: 'var(--muted)', fontSize: '0.85em', margin: '0.4em 0 0' }}>
              Platform-provided systemd timers — stock OS housekeeping (log rotation, temp
              cleanup) and Amazon fleet agents. You didn't create these; they ship with the box.
            </p>
            {showSystem && (
              <div style={{ marginTop: '0.8em' }}>
                {systemJobs.length === 0
                  ? <div className="empty-state" style={{ padding: 'var(--space-md)' }}>
                      <p style={{ color: 'var(--muted)' }}>No systemd timers found.</p>
                    </div>
                  : renderJobTable(systemJobs)}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
