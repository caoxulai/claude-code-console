import { useEffect, useState, useRef, useCallback, useMemo } from 'react';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

function relativeTime(iso) {
  if (!iso) return '--';
  const diff = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(diff)) return '--';
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function humanInterval(intervalS) {
  if (intervalS == null || intervalS <= 0) return '--';
  if (intervalS >= 86400) return `every ${Math.round(intervalS / 86400)}d`;
  if (intervalS >= 3600) return `every ${Math.round(intervalS / 3600)}h`;
  if (intervalS >= 60) return `every ${Math.round(intervalS / 60)}m`;
  return `every ${intervalS}s`;
}

function statusColor(status) {
  if (status === 'running') return '#22c55e';
  if (status === 'errored') return '#f59e0b';
  return '#6b7280'; // stopped / unknown
}

export default function WorkersPage() {
  const [workers, setWorkers] = useState([]);
  const [filter, setFilter] = useState('all');
  const [loading, setLoading] = useState(true);

  // Keep a ref to the latest filter so the interval callback reads it without
  // recreating the interval on filter change.
  const filterRef = useRef(filter);
  filterRef.current = filter;

  const fetchWorkers = useCallback(async (opts) => {
    const silent = opts && opts.silent;
    try {
      const res = await fetch('/api/workers');
      if (!res.ok) return;
      const json = await res.json();
      const list = Array.isArray(json) ? json : Array.isArray(json.workers) ? json.workers : [];
      setWorkers(list);
    } catch {
      // Degrade silently
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchWorkers();
    const id = setInterval(() => fetchWorkers({ silent: true }), 5000);
    return () => clearInterval(id);
  }, [fetchWorkers]);

  // Live updates as a complement to the interval
  useLiveUpdates(['worker_changed'], () => fetchWorkers({ silent: true }));

  // Derive the list of distinct project names for the filter dropdown.
  const projectOptions = useMemo(() => {
    const projects = [...new Set(workers.filter(w => w.project).map(w => w.project))];
    projects.sort();
    return projects;
  }, [workers]);

  // Client-side filtering:
  // 'all' -> show all
  // 'global' -> show only where project is null
  // A project name -> show where project matches OR project is null (union rule)
  const filteredWorkers = useMemo(() => {
    if (filter === 'all') return workers;
    if (filter === 'global') return workers.filter(w => !w.project);
    return workers.filter(w => !w.project || w.project === filter);
  }, [workers, filter]);

  // Column alignment tokens (shared between th and td)
  const colLeft = { textAlign: 'left' };
  const colCenter = { textAlign: 'center' };

  return (
    <div>
      <div className="page-header">
        <h2>Workers</h2>
        <select
          value={filter}
          onChange={e => setFilter(e.target.value)}
          style={{
            padding: '0.4em 0.7em',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: '0.85em',
          }}
        >
          <option value="all">All</option>
          <option value="global">Global</option>
          {projectOptions.map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>

      <div className="card">
        {loading ? (
          <div style={{ color: 'var(--muted)', fontSize: '0.9em', padding: 'var(--space-md)' }}>
            Loading workers...
          </div>
        ) : filteredWorkers.length === 0 ? (
          <div className="empty-state">
            <h3>No workers registered</h3>
            <p>Workers will appear here once the server registers background tasks.</p>
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th style={colLeft}>Name</th>
                <th style={colCenter}>Status</th>
                <th style={colCenter}>Project</th>
                <th style={colCenter}>Interval</th>
                <th style={colCenter}>Last Run</th>
                <th style={colCenter}>Last Result</th>
                <th style={colLeft}>Description</th>
              </tr>
            </thead>
            <tbody>
              {filteredWorkers.map(w => (
                <tr key={w.name || w.id}>
                  <td style={colLeft}>
                    <span style={{ fontWeight: 500 }}>{w.name || w.id || '--'}</span>
                  </td>
                  <td style={colCenter}>
                    <span style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '0.4em',
                      fontSize: '0.85em',
                    }}>
                      <span style={{
                        width: '8px',
                        height: '8px',
                        borderRadius: '50%',
                        background: statusColor(w.status),
                        display: 'inline-block',
                        flexShrink: 0,
                      }} />
                      {w.status || 'unknown'}
                    </span>
                  </td>
                  <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }}>
                    {w.project || 'Global'}
                  </td>
                  <td style={{ ...colCenter, fontFamily: 'monospace', fontSize: '0.85em' }}>
                    {humanInterval(w.intervalS)}
                  </td>
                  <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }}>
                    {relativeTime(w.lastRunAt)}
                  </td>
                  <td style={{ ...colCenter, color: 'var(--muted)', fontSize: '0.85em' }}>
                    {w.lastResult || '--'}
                  </td>
                  <td style={{ ...colLeft, color: 'var(--muted)', fontSize: '0.85em' }}>
                    {w.description || '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
