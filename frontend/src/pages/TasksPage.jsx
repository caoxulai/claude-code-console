import { useEffect, useState, useCallback } from 'react';
import { FiList, FiX, FiTrash2, FiZap, FiPlus, FiSave, FiChevronRight, FiChevronDown } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';
import { useTriggerGoal } from '../hooks/useTriggerGoal';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
import SessionPickerModal from '../components/SessionPickerModal';

const STAGES = [
  { key: 'draft', label: 'Draft', action: 'Clarify', target: 'clarifying' },
  { key: 'clarifying', label: 'Clarifying', action: 'Plan', target: 'planned' },
  { key: 'planned', label: 'Planned', action: 'Execute', target: 'executing' },
  { key: 'executing', label: 'Executing', action: null, target: null },
  { key: 'done', label: 'Done', action: null, target: null },
];


const PRIORITY_STYLES = {
  p1: { background: 'var(--danger, #d9534f)', color: '#fff' },
  p2: { background: '#f0ad4e', color: '#fff' },
  p3: { background: 'var(--muted)', color: '#fff' },
};

function PriorityBadge({ priority }) {
  const p = (priority || 'p2').toLowerCase();
  const style = PRIORITY_STYLES[p] || PRIORITY_STYLES.p2;
  return (
    <span className="badge" style={{ ...style, fontSize: 'var(--fs-xs)', padding: '0.1em 0.45em' }}>
      {p.toUpperCase()}
    </span>
  );
}

const POLL_MS = 60000;

export default function TasksPage() {
  const [tasks, setTasks] = useState(null);
  const [projects, setProjects] = useState([]);
  const [error, setError] = useState(null);

  // Filters
  const [projectFilter, setProjectFilter] = useState('all');
  const [stageFilter, setStageFilter] = useState(new Set());

  // Which stage groups are collapsed. Done starts folded.
  const [collapsedGroups, setCollapsedGroups] = useState({ done: true });
  const toggleGroup = (stage) =>
    setCollapsedGroups(prev => ({ ...prev, [stage]: !prev[stage] }));

  // Multi-session picker for "trigger goal" (when a project has >1 session).
  const [picker, setPicker] = useState(null);
  const triggerGoal = useTriggerGoal({ onPickSession: setPicker });

  // Create-task form state.
  const [showNew, setShowNew] = useState(false);
  const [allProjects, setAllProjects] = useState([]);
  const [newSubject, setNewSubject] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newProject, setNewProject] = useState('');
  const [newPriority, setNewPriority] = useState('p2');

  useEffect(() => {
    fetch('/api/projects')
      .then(r => r.json())
      .then(data => setAllProjects(Array.isArray(data) ? data.map(p => p.name) : []))
      .catch(() => {});
  }, []);

  const fetchTasks = useCallback(async ({ silent = false } = {}) => {
    const params = new URLSearchParams();
    if (projectFilter !== 'all') params.set('project', projectFilter);
    if (stageFilter.size > 0) params.set('stage', [...stageFilter].join(','));
    try {
      const res = await fetch(`/api/tasks?${params.toString()}`);
      const data = await res.json();
      setTasks(Array.isArray(data.tasks) ? data.tasks : []);
      setProjects(Array.isArray(data.projects) ? data.projects : []);
      setError(null);
    } catch {
      if (!silent) setError('Failed to load tasks.');
    }
  }, [projectFilter, stageFilter]);

  // Refetch on mount and whenever a filter changes.
  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  // Live push: the backend broadcasts task_changed on task create/update.
  useLiveUpdates(['task_changed'], () => fetchTasks({ silent: true }));

  // Slow backstop poll so the list self-heals if a WS event is missed.
  useEffect(() => {
    const tick = () => { if (!document.hidden) fetchTasks({ silent: true }); };
    const id = setInterval(tick, POLL_MS);
    return () => clearInterval(id);
  }, [fetchTasks]);

  // Sync on tab focus too.
  useEffect(() => {
    const onFocus = () => fetchTasks({ silent: true });
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [fetchTasks]);

  // Advance a task's stage.
  const advanceTask = useCallback(async (task, targetStage) => {
    try {
      const res = await fetch(`/api/tasks/user/${encodeURIComponent(task.id)}/advance`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage: targetStage }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.message || data.error || `Failed to advance task (${res.status}).`);
        return;
      }
      setError(null);
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to advance task.');
    }
  }, [fetchTasks]);

  // Delete task (permanent).
  const deleteTask = useCallback(async (task) => {
    if (!window.confirm(`Delete task "${task.subject || task.id}"?\n\nThis permanently removes the task.`)) {
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

  // Create a new TODO.
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
          priority: newPriority,
        }),
      });
      if (!res.ok) { setError('Failed to create task.'); return; }
      setShowNew(false);
      setNewSubject('');
      setNewDescription('');
      setNewProject('');
      setNewPriority('p2');
      setError(null);
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to create task.');
    }
  }, [newSubject, newDescription, newProject, newPriority, fetchTasks]);

  // Stage filter toggle.
  const toggleStageFilter = (key) => {
    setStageFilter(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Group tasks by stage in fixed STAGES order.
  const grouped = (() => {
    const all = tasks || [];
    return STAGES
      .map(s => ({ stage: s, items: all.filter(t => (t.stage || 'draft') === s.key) }))
      .filter(g => g.items.length > 0);
  })();

  const counts = tasks
    ? { total: tasks.length, done: tasks.filter(t => (t.stage || 'draft') === 'done').length }
    : null;

  return (
    <div>
      <div className="page-header">
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-md)' }}>
          <h2>TODO</h2>
          {counts && (
            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
              {counts.total} total · {counts.done} done
            </span>
          )}
        </div>
        <button className="btn btn-primary" onClick={() => { setShowNew(!showNew); setError(null); }}>
          <FiPlus size={14} /> New TODO
        </button>
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {/* Create-task form */}
      {showNew && (
        <div className="card" style={{ marginBottom: 'var(--space-md)' }}>
          <div className="card-title">New TODO</div>
          <p style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 0, marginBottom: 'var(--space-md)', lineHeight: 1.5 }}>
            TODOs live in claude-web and appear here. Use the action buttons to advance them through stages.
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
          <div style={{ display: 'flex', gap: 'var(--space-md)', flexWrap: 'wrap' }}>
            <div className="form-group" style={{ flex: 1, minWidth: '140px' }}>
              <label className="form-label">Project <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(optional)</span></label>
              <select className="form-select" value={newProject} onChange={e => setNewProject(e.target.value)}>
                <option value="">No project (global)</option>
                {allProjects.map(p => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <div className="form-group" style={{ minWidth: '100px' }}>
              <label className="form-label">Priority</label>
              <select className="form-select" value={newPriority} onChange={e => setNewPriority(e.target.value)}>
                <option value="p1">P1 (high)</option>
                <option value="p2">P2 (medium)</option>
                <option value="p3">P3 (low)</option>
              </select>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 'var(--space-sm)' }}>
            <button className="btn btn-primary" onClick={createUserTask} disabled={!newSubject.trim()}>
              <FiSave size={14} /> Save
            </button>
            <button className="btn" onClick={() => { setShowNew(false); setNewSubject(''); setNewDescription(''); setNewProject(''); setNewPriority('p2'); }}>
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

        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-xs)', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--muted)' }}>Stage</span>
          {STAGES.map(s => (
            <button
              key={s.key}
              className="btn"
              onClick={() => toggleStageFilter(s.key)}
              style={{
                fontSize: 'var(--fs-xs)', padding: 'var(--space-xs) var(--space-sm)',
                background: stageFilter.has(s.key) ? 'var(--accent)' : undefined,
                color: stageFilter.has(s.key) ? '#fff' : undefined,
                borderColor: stageFilter.has(s.key) ? 'var(--accent)' : undefined,
              }}
            >
              {s.label}
            </button>
          ))}
          {stageFilter.size > 0 && (
            <button
              className="btn"
              onClick={() => setStageFilter(new Set())}
              style={{ fontSize: 'var(--fs-xs)', padding: 'var(--space-xs) var(--space-sm)' }}
              title="Clear stage filter"
            >
              <FiX size={10} /> Clear
            </button>
          )}
        </div>
      </div>

      {!tasks ? (
        <div className="card">{Array.from({ length: 4 }).map((_, i) => <SkeletonLine key={i} />)}</div>
      ) : (tasks.length === 0) ? (
        <div className="card">
          <div className="empty-state">
            <FiList size={32} style={{ color: 'var(--muted)', marginBottom: 'var(--space-sm)' }} />
            <h3>No TODOs yet</h3>
            <p>Create your first TODO with <strong>New TODO</strong> above, or adjust the filters.</p>
          </div>
        </div>
      ) : (
        grouped.map(group => {
          const { stage, items } = group;
          const collapsed = !!collapsedGroups[stage.key];
          return (
            <div key={stage.key} style={{ marginBottom: 'var(--space-lg)' }}>
              <button
                onClick={() => toggleGroup(stage.key)}
                title={collapsed ? 'Expand' : 'Collapse'}
                style={{
                  display: 'flex', alignItems: 'center', gap: 'var(--space-sm)',
                  marginBottom: 'var(--space-sm)', background: 'none', border: 'none',
                  padding: 0, cursor: 'pointer', color: 'var(--text)',
                }}
              >
                {collapsed ? <FiChevronRight size={14} style={{ color: 'var(--muted)' }} /> : <FiChevronDown size={14} style={{ color: 'var(--muted)' }} />}
                <h3 style={{ fontSize: 'var(--fs-base)', fontWeight: 600, margin: 0 }}>{stage.label}</h3>
                <span className="badge" style={{ fontSize: 'var(--fs-xs)' }}>{items.length}</span>
              </button>
              {!collapsed && (
                <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                  {items.map((t, i) => (
                    <div
                      key={t.id}
                      style={{
                        padding: 'var(--space-sm) var(--space-md)',
                        borderBottom: i < items.length - 1 ? '1px solid var(--border)' : 'none',
                        opacity: stage.key === 'done' ? 0.65 : 1,
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-md)', alignItems: 'flex-start' }}>
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div style={{
                            fontWeight: 600, fontSize: 'var(--fs-sm)',
                            textDecoration: stage.key === 'done' ? 'line-through' : 'none',
                          }}>
                            {t.subject || `Task ${t.id}`}
                          </div>
                          {t.description && (
                            <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 'var(--space-xs)', lineHeight: 1.5 }}>
                              {t.description}
                            </div>
                          )}
                        </div>
                        <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 'var(--space-xs)' }}>
                          <div style={{ display: 'flex', gap: 'var(--space-xs)', alignItems: 'center' }}>
                            <PriorityBadge priority={t.priority} />
                            {t.project && <span className="badge">{t.project}</span>}
                          </div>
                          <TaskActions
                            task={t}
                            stage={stage}
                            onAdvance={advanceTask}
                            onDelete={deleteTask}
                            onTrigger={triggerGoal}
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

// Per-task action buttons.
function actionBtn(extra = {}) {
  return {
    background: 'none', border: 'none', cursor: 'pointer',
    color: 'var(--muted)', display: 'flex', alignItems: 'center',
    gap: 'var(--space-xs)', fontSize: 'var(--fs-xs)', padding: '0.1em 0', ...extra,
  };
}

function TaskActions({ task, stage, onAdvance, onDelete, onTrigger }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
      <button onClick={() => onTrigger(task)} title="Trigger a /goal in this project's session" style={actionBtn({ color: 'var(--accent)' })}>
        <FiZap size={11} /> goal
      </button>
      {/* Context-aware advance button: shows the NEXT stage action */}
      {stage.action && (
        <button
          onClick={() => onAdvance(task, stage.target)}
          title={`Advance to ${stage.target}`}
          style={actionBtn({ color: 'var(--accent)' })}
        >
          {stage.action} &rarr;
        </button>
      )}
      {/* Done button available on any non-done item */}
      {stage.key !== 'done' && (
        <button
          onClick={() => onAdvance(task, 'done')}
          title="Mark done"
          style={actionBtn()}
        >
          Done &#10003;
        </button>
      )}
      <button onClick={() => onDelete(task)} title="Delete the task (permanent)" style={actionBtn({ color: 'var(--danger, #d9534f)' })}>
        <FiTrash2 size={11} /> delete
      </button>
    </div>
  );
}
