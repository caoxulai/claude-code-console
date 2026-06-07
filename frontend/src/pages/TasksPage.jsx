import { useEffect, useState } from 'react';
import { FiCheckCircle, FiCircle, FiLoader, FiList } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';

const STATUS_META = {
  in_progress: { label: 'In Progress', cls: 'badge-warn', icon: FiLoader, order: 0 },
  pending: { label: 'Pending', cls: '', icon: FiCircle, order: 1 },
  completed: { label: 'Completed', cls: 'badge-ok', icon: FiCheckCircle, order: 2 },
};

function statusMeta(status) {
  return STATUS_META[status] || { label: status || 'unknown', cls: '', icon: FiCircle, order: 3 };
}

export default function TasksPage() {
  const [tasks, setTasks] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/tasks')
      .then(r => r.json())
      .then(data => setTasks(Array.isArray(data) ? data : []))
      .catch(() => setError('Failed to load tasks.'));
  }, []);

  const grouped = (() => {
    if (!tasks) return [];
    const order = ['in_progress', 'pending', 'completed'];
    const seen = new Set(order);
    const others = [...new Set(tasks.map(t => t.status).filter(s => !seen.has(s)))];
    return [...order, ...others]
      .map(status => ({ status, items: tasks.filter(t => t.status === status) }))
      .filter(g => g.items.length > 0);
  })();

  const counts = tasks
    ? {
        total: tasks.length,
        in_progress: tasks.filter(t => t.status === 'in_progress').length,
        completed: tasks.filter(t => t.status === 'completed').length,
      }
    : null;

  return (
    <div>
      <div className="page-header">
        <h2>Tasks</h2>
        {counts && (
          <span style={{ fontSize: '0.82em', color: 'var(--muted)' }}>
            {counts.total} total · {counts.in_progress} in progress · {counts.completed} done
          </span>
        )}
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {!tasks ? (
        <div className="card">{Array.from({ length: 4 }).map((_, i) => <SkeletonLine key={i} />)}</div>
      ) : tasks.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <FiList size={32} style={{ color: 'var(--muted)', marginBottom: '0.5em' }} />
            <h3>No tasks tracked</h3>
            <p>Tasks created by Claude Code sessions appear here.</p>
          </div>
        </div>
      ) : (
        grouped.map(group => {
          const meta = statusMeta(group.status);
          const Icon = meta.icon;
          return (
            <div key={group.status} style={{ marginBottom: '1.5em' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: '0.5em' }}>
                <Icon size={15} style={{ color: 'var(--accent)' }} />
                <h3 style={{ fontSize: '1em', fontWeight: 600, margin: 0 }}>{meta.label}</h3>
                <span style={{ fontSize: '0.78em', color: 'var(--muted)' }}>({group.items.length})</span>
              </div>
              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                {group.items.map((t, i) => (
                  <div
                    key={`${t._sessionId}-${t.id}`}
                    style={{
                      padding: '0.75em 1em',
                      borderBottom: i < group.items.length - 1 ? '1px solid var(--border)' : 'none',
                      opacity: t.status === 'completed' ? 0.65 : 1,
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1em', alignItems: 'flex-start' }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{
                          fontWeight: 600, fontSize: '0.92em',
                          textDecoration: t.status === 'completed' ? 'line-through' : 'none',
                        }}>
                          {t.subject || `Task ${t.id}`}
                        </div>
                        {t.description && (
                          <div style={{ fontSize: '0.8em', color: 'var(--muted)', marginTop: '0.25em', lineHeight: 1.5 }}>
                            {t.description}
                          </div>
                        )}
                        {(t.blockedBy?.length > 0) && (
                          <div style={{ fontSize: '0.75em', color: 'var(--muted)', marginTop: '0.3em' }}>
                            Blocked by: {t.blockedBy.join(', ')}
                          </div>
                        )}
                      </div>
                      <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.3em' }}>
                        <span className={`badge ${meta.cls}`}>{meta.label}</span>
                        {t._sessionId && (
                          <span style={{ fontSize: '0.7em', color: 'var(--muted)', fontFamily: 'monospace' }}>
                            {t._sessionId.slice(0, 8)}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
