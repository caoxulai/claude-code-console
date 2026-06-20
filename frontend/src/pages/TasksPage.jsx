import { useEffect, useState, useCallback } from 'react';
import { FiCheckCircle, FiCircle, FiLoader, FiList, FiX, FiRotateCcw, FiTrash2, FiZap, FiPlus, FiSave, FiEdit3, FiChevronRight, FiChevronDown } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';
import { useTriggerGoal } from '../hooks/useTriggerGoal';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
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

const POLL_MS = 60000;

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

  // Which status groups are collapsed. Completed starts folded (it's usually
  // noise); the user expands it by clicking the group header.
  const [collapsedGroups, setCollapsedGroups] = useState({ completed: true });
  const toggleGroup = (status) =>
    setCollapsedGroups(prev => ({ ...prev, [status]: !prev[status] }));

  // Multi-session picker for "trigger goal" (when a project has >1 session).
  const [picker, setPicker] = useState(null); // { task, prompt, sessions, resume }
  const triggerGoal = useTriggerGoal({ onPickSession: setPicker });

  // Create-task form state. The project dropdown uses the workspace project
  // list from /api/projects (names), separate from the filter's project set.
  const [showNew, setShowNew] = useState(false);
  const [allProjects, setAllProjects] = useState([]);
  const [newSubject, setNewSubject] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newProject, setNewProject] = useState('');

  useEffect(() => {
    fetch('/api/projects')
      .then(r => r.json())
      .then(data => setAllProjects(Array.isArray(data) ? data.map(p => p.name) : []))
      .catch(() => {});
  }, []);

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

  // Live push: the backend broadcasts `task_changed` on task create/update, so
  // refetch immediately instead of waiting on the poll. The hook calls back with
  // no args, so just re-run the existing fetch (it reads current filters).
  useLiveUpdates(['task_changed'], () => fetchTasks({ silent: true }));

  // Slow backstop poll so the list can't get permanently stuck if a WS event is
  // missed. Silent (no skeleton) and paused while the tab is hidden.
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

  // Create a console task (stored in ~/.claude-web, not ~/.claude/tasks).
  const createUserTask = useCallback(async () => {
    if (!newSubject.trim()) return;
    try {
      const res = await fetch('/api/tasks/user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          subject: newSubject.trim(),
          description: newDescription.trim(),
          project: newProject,
        }),
      });
      if (!res.ok) { setError('Failed to create task.'); return; }
      setShowNew(false);
      setNewSubject('');
      setNewDescription('');
      setNewProject('');
      setError(null);
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to create task.');
    }
  }, [newSubject, newDescription, newProject, fetchTasks]);

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

  return (
    <div>
      <div className="page-header">
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-md)' }}>
          <h2>Tasks</h2>
          {counts && (
            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
              {counts.total} shown · {counts.in_progress} in progress · {counts.completed} done
            </span>
          )}
        </div>
        <button className="btn btn-primary" onClick={() => { setShowNew(!showNew); setError(null); }}>
          <FiPlus size={14} /> New Task
        </button>
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {/* Create-task form */}
      {showNew && (
        <div className="card" style={{ marginBottom: 'var(--space-md)' }}>
          <div className="card-title">New Task</div>
          <p style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 0, marginBottom: 'var(--space-md)', lineHeight: 1.5 }}>
            Console tasks live in claude-web and appear here in the Tasks tab — they won&apos;t
            show up in the Claude Code CLI&apos;s <code>tasks</code> list (the CLI keeps its own
            per-session store).
          </p>
          <div className="form-group">
            <label className="form-label">Subject</label>
            <input
              className="form-input"
              placeholder="What needs doing?"
              value={newSubject}
              onChange={e => setNewSubject(e.target.value)}
              autoFocus
            />
          </div>
          <div className="form-group">
            <label className="form-label">Description <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(optional)</span></label>
            <textarea
              className="form-textarea"
              placeholder="More detail — this becomes the /goal body when you trigger it."
              value={newDescription}
              onChange={e => setNewDescription(e.target.value)}
              style={{ minHeight: '70px' }}
            />
          </div>
          <div className="form-group">
            <label className="form-label">Project <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(optional)</span></label>
            <select className="form-select" value={newProject} onChange={e => setNewProject(e.target.value)}>
              <option value="">No project (global)</option>
              {allProjects.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div style={{ display: 'flex', gap: 'var(--space-sm)' }}>
            <button className="btn btn-primary" onClick={createUserTask} disabled={!newSubject.trim()}>
              <FiSave size={14} /> Save
            </button>
            <button className="btn" onClick={() => { setShowNew(false); setNewSubject(''); setNewDescription(''); setNewProject(''); }}>
              <FiX size={14} /> Cancel
            </button>
          </div>
        </div>
      )}

      {/* Filter bar */}
      <div className="card" style={{ padding: 'var(--space-sm) var(--space-md)', marginBottom: 'var(--space-md)', display: 'flex', flexWrap: 'wrap', gap: 'var(--space-md)', alignItems: 'center' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-xs)', fontSize: 'var(--fs-sm)', color: 'var(--muted)' }}>
          Project
          <select className="form-select" value={projectFilter} onChange={e => setProjectFilter(e.target.value)} style={{ width: 'auto', fontSize: 'var(--fs-sm)', padding: 'var(--space-xs) var(--space-sm)' }}>
            <option value="all">All projects</option>
            {projects.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>

        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-xs)' }}>
          <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--muted)' }}>Range</span>
          {TIME_RANGES.map(r => (
            <button
              key={r.key}
              className="btn"
              onClick={() => setTimeRange(r.key)}
              style={{
                fontSize: 'var(--fs-xs)', padding: 'var(--space-xs) var(--space-sm)',
                background: timeRange === r.key ? 'var(--accent)' : undefined,
                color: timeRange === r.key ? '#fff' : undefined,
                borderColor: timeRange === r.key ? 'var(--accent)' : undefined,
              }}
            >
              {r.label}
            </button>
          ))}
        </div>

        <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-xs)', fontSize: 'var(--fs-sm)', color: 'var(--muted)', cursor: 'pointer' }}>
          <input type="checkbox" checked={showCompleted} onChange={e => setShowCompleted(e.target.checked)} />
          Show completed
        </label>

        <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-xs)', fontSize: 'var(--fs-sm)', color: 'var(--muted)', cursor: 'pointer' }}>
          <input type="checkbox" checked={showDismissed} onChange={e => setShowDismissed(e.target.checked)} />
          Show dismissed{dismissedCount ? ` (${dismissedCount})` : ''}
        </label>
      </div>

      {!tasks ? (
        <div className="card">{Array.from({ length: 4 }).map((_, i) => <SkeletonLine key={i} />)}</div>
      ) : visible.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <FiList size={32} style={{ color: 'var(--muted)', marginBottom: 'var(--space-sm)' }} />
            <h3>No tasks tracked</h3>
            <p>Tasks from Claude Code sessions appear here, or create your own with <strong>New Task</strong>. Try widening the filters above.</p>
          </div>
        </div>
      ) : (
        grouped.map(group => {
          const meta = statusMeta(group.status);
          const Icon = meta.icon;
          const collapsed = !!collapsedGroups[group.status];
          return (
            <div key={group.status} style={{ marginBottom: 'var(--space-lg)' }}>
              <button
                onClick={() => toggleGroup(group.status)}
                title={collapsed ? 'Expand' : 'Collapse'}
                style={{
                  display: 'flex', alignItems: 'center', gap: 'var(--space-sm)',
                  marginBottom: 'var(--space-sm)', background: 'none', border: 'none',
                  padding: 0, cursor: 'pointer', color: 'var(--text)',
                }}
              >
                {collapsed ? <FiChevronRight size={14} style={{ color: 'var(--muted)' }} /> : <FiChevronDown size={14} style={{ color: 'var(--muted)' }} />}
                <Icon size={15} style={{ color: 'var(--accent)' }} />
                <h3 style={{ fontSize: 'var(--fs-base)', fontWeight: 600, margin: 0 }}>{meta.label}</h3>
                <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>({group.items.length})</span>
              </button>
              {!collapsed && (
              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                {group.items.map((t, i) => (
                  <div
                    key={`${t._sessionId}-${t.id}`}
                    style={{
                      padding: 'var(--space-sm) var(--space-md)',
                      borderBottom: i < group.items.length - 1 ? '1px solid var(--border)' : 'none',
                      opacity: t.dismissed ? 0.5 : t.status === 'completed' ? 0.65 : 1,
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-md)', alignItems: 'flex-start' }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{
                          fontWeight: 600, fontSize: 'var(--fs-sm)',
                          textDecoration: t.status === 'completed' ? 'line-through' : 'none',
                        }}>
                          {t.subject || `Task ${t.id}`}
                        </div>
                        {t.description && (
                          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 'var(--space-xs)', lineHeight: 1.5 }}>
                            {t.description}
                          </div>
                        )}
                        {(t.blockedBy?.length > 0) && (
                          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 'var(--space-xs)' }}>
                            Blocked by: {t.blockedBy.join(', ')}
                          </div>
                        )}
                      </div>
                      <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 'var(--space-xs)' }}>
                        <span className={`badge ${meta.cls}`}>{meta.label}</span>
                        <span className="badge">{t.project}</span>
                        {t.source === 'user' && (
                          <span className="badge badge-user" title="Created in the console">added</span>
                        )}
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
              )}
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
    gap: 'var(--space-xs)', fontSize: 'var(--fs-xs)', padding: '0.1em 0', ...extra,
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

