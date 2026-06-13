import { useEffect, useState, useCallback } from 'react';
import { FiCheckCircle, FiCircle, FiLoader, FiList, FiX, FiRotateCcw, FiTrash2, FiZap } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';
import { useTriggerGoal } from '../hooks/useTriggerGoal';
import SessionPickerModal from '../components/SessionPickerModal';

const STATUS_META = {
  in_progress: { label: 'In Progress', cls: 'badge-warn', icon: FiLoader, order: 0 },
  pending: { label: 'Pending', cls: '', icon: FiCircle, order: 1 },
  completed: { label: 'Completed', cls: 'badge-ok', icon: FiCheckCircle, order: 2 },
};

function statusMeta(status) {
  return STATUS_META[status] || { label: status || 'unknown', cls: '', icon: FiCircle, order: 3 };
}

const TIME_RANGES = [
  { key: '7', label: '7 days' },
  { key: '30', label: '30 days' },
  { key: 'all', label: 'All time' },
];

const POLL_MS = 5000;

export default function TasksPage() {
  const [tasks, setTasks] = useState(null);
  const [projects, setProjects] = useState([]);
  const [dismissedCount, setDismissedCount] = useState(0);
  const [error, setError] = useState(null);

  // Filters
  const [projectFilter, setProjectFilter] = useState('all');
  const [timeRange, setTimeRange] = useState('all');
  const [showCompleted, setShowCompleted] = useState(true);
  const [showDismissed, setShowDismissed] = useState(false);

  // Multi-session picker for "trigger goal" (when a project has >1 session).
  const [picker, setPicker] = useState(null); // { task, prompt, sessions, resume }
  const triggerGoal = useTriggerGoal({ onPickSession: setPicker });

  // `silent` skips the loading skeleton so background polls don't flicker the view.
  const fetchTasks = useCallback(async ({ silent = false } = {}) => {
    const params = new URLSearchParams();
    if (projectFilter !== 'all') params.set('project', projectFilter);
    if (timeRange !== 'all') params.set('sinceDays', timeRange);
    if (showDismissed) params.set('includeDismissed', '1');
    try {
      const res = await fetch(`/api/tasks?${params.toString()}`);
      const data = await res.json();
      setTasks(Array.isArray(data.tasks) ? data.tasks : []);
      setProjects(Array.isArray(data.projects) ? data.projects : []);
      setDismissedCount(data.dismissedCount || 0);
      setError(null);
    } catch {
      if (!silent) setError('Failed to load tasks.');
    }
  }, [projectFilter, timeRange, showDismissed]);

  // Refetch on mount and whenever a filter changes.
  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  // Background poll so new/updated/completed tasks appear without a manual
  // reload. Silent (no skeleton) and paused while the tab is hidden.
  useEffect(() => {
    const tick = () => { if (!document.hidden) fetchTasks({ silent: true }); };
    const id = setInterval(tick, POLL_MS);
    return () => clearInterval(id);
  }, [fetchTasks]);

  // Sync on tab focus too (cheap, matches SessionsPage behavior).
  useEffect(() => {
    const onFocus = () => fetchTasks({ silent: true });
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [fetchTasks]);

  const setDismissed = useCallback(async (task, dismiss) => {
    const path = dismiss ? '/api/tasks/dismiss' : '/api/tasks/undismiss';
    try {
      await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: task._sessionId, taskId: task.id }),
      });
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to update task visibility.');
    }
  }, [fetchTasks]);

  // Mark complete — REAL write to the source task file.
  const completeTask = useCallback(async (task) => {
    try {
      await fetch('/api/tasks/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: task._sessionId, taskId: task.id }),
      });
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to complete task.');
    }
  }, [fetchTasks]);

  // Delete — REMOVES the source task file. Confirm first (destructive).
  const deleteTask = useCallback(async (task) => {
    if (!window.confirm(`Delete task "${task.subject || task.id}"?\n\nThis permanently removes the task file from ~/.claude/tasks.`)) {
      return;
    }
    try {
      await fetch('/api/tasks/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: task._sessionId, taskId: task.id }),
      });
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to delete task.');
    }
  }, [fetchTasks]);

  // Client-side "hide completed" toggle (the server still returns them so the
  // count stays accurate; we just filter the rendered list).
  const visible = (tasks || []).filter(t => showCompleted || t.status !== 'completed');

  const grouped = (() => {
    const order = ['in_progress', 'pending', 'completed'];
    const seen = new Set(order);
    const others = [...new Set(visible.map(t => t.status).filter(s => !seen.has(s)))];
    return [...order, ...others]
      .map(status => ({ status, items: visible.filter(t => t.status === status) }))
      .filter(g => g.items.length > 0);
  })();

  const counts = tasks
    ? {
        total: visible.length,
        in_progress: visible.filter(t => t.status === 'in_progress').length,
        completed: visible.filter(t => t.status === 'completed').length,
      }
    : null;

  const selectStyle = {
    fontSize: '0.78em', padding: '0.25em 0.5em',
    background: 'var(--bg)', color: 'var(--text)',
    border: '1px solid var(--border)', borderRadius: 4,
  };

  return (
    <div>
      <div className="page-header">
        <h2>Tasks</h2>
        {counts && (
          <span style={{ fontSize: '0.82em', color: 'var(--muted)' }}>
            {counts.total} shown · {counts.in_progress} in progress · {counts.completed} done
          </span>
        )}
      </div>

      {/* Filter bar */}
      <div className="card" style={{ padding: '0.6em 0.9em', marginBottom: '1em', display: 'flex', flexWrap: 'wrap', gap: '0.9em', alignItems: 'center' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.4em', fontSize: '0.8em', color: 'var(--muted)' }}>
          Project
          <select value={projectFilter} onChange={e => setProjectFilter(e.target.value)} style={selectStyle}>
            <option value="all">All projects</option>
            {projects.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.3em' }}>
          <span style={{ fontSize: '0.8em', color: 'var(--muted)' }}>Range</span>
          {TIME_RANGES.map(r => (
            <button
              key={r.key}
              className="btn"
              onClick={() => setTimeRange(r.key)}
              style={{
                fontSize: '0.72em', padding: '0.2em 0.6em',
                background: timeRange === r.key ? 'var(--accent)' : undefined,
                color: timeRange === r.key ? '#fff' : undefined,
                borderColor: timeRange === r.key ? 'var(--accent)' : undefined,
              }}
            >
              {r.label}
            </button>
          ))}
        </div>

        <label style={{ display: 'flex', alignItems: 'center', gap: '0.4em', fontSize: '0.8em', color: 'var(--muted)', cursor: 'pointer' }}>
          <input type="checkbox" checked={showCompleted} onChange={e => setShowCompleted(e.target.checked)} />
          Show completed
        </label>

        <label style={{ display: 'flex', alignItems: 'center', gap: '0.4em', fontSize: '0.8em', color: 'var(--muted)', cursor: 'pointer' }}>
          <input type="checkbox" checked={showDismissed} onChange={e => setShowDismissed(e.target.checked)} />
          Show dismissed{dismissedCount ? ` (${dismissedCount})` : ''}
        </label>
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {!tasks ? (
        <div className="card">{Array.from({ length: 4 }).map((_, i) => <SkeletonLine key={i} />)}</div>
      ) : visible.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <FiList size={32} style={{ color: 'var(--muted)', marginBottom: '0.5em' }} />
            <h3>No tasks tracked</h3>
            <p>Tasks created by Claude Code sessions appear here. Try widening the filters above.</p>
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
                      opacity: t.dismissed ? 0.5 : t.status === 'completed' ? 0.65 : 1,
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
                        <span className="badge" style={{ fontSize: '0.68em' }}>{t.project}</span>
                        <TaskActions
                          task={t}
                          onComplete={completeTask}
                          onDelete={deleteTask}
                          onTrigger={triggerGoal}
                          onDismiss={setDismissed}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })
      )}

      {picker && (
        <SessionPickerModal picker={picker} onClose={() => setPicker(null)} />
      )}
    </div>
  );
}

// Per-task action buttons: trigger-goal (always), complete (unless already
// completed), hide/restore (completed or dismissed), delete (always).
function actionBtn(extra = {}) {
  return {
    background: 'none', border: 'none', cursor: 'pointer',
    color: 'var(--muted)', display: 'flex', alignItems: 'center',
    gap: '0.25em', fontSize: '0.7em', padding: '0.1em 0', ...extra,
  };
}

function TaskActions({ task, onComplete, onDelete, onTrigger, onDismiss }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
      <button onClick={() => onTrigger(task)} title="Trigger a /goal in this project's session" style={actionBtn({ color: 'var(--accent)' })}>
        <FiZap size={11} /> goal
      </button>
      {task.status !== 'completed' && (
        <button onClick={() => onComplete(task)} title="Mark complete (writes to the task file)" style={actionBtn()}>
          <FiCheckCircle size={11} /> done
        </button>
      )}
      {(task.status === 'completed' || task.dismissed) && (
        <button onClick={() => onDismiss(task, !task.dismissed)} title={task.dismissed ? 'Restore task' : 'Hide this completed task'} style={actionBtn()}>
          {task.dismissed ? <><FiRotateCcw size={11} /> restore</> : <><FiX size={11} /> hide</>}
        </button>
      )}
      <button onClick={() => onDelete(task)} title="Delete the task file (permanent)" style={actionBtn({ color: 'var(--danger, #d9534f)' })}>
        <FiTrash2 size={11} /> delete
      </button>
    </div>
  );
}

