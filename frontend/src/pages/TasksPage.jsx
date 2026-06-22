import { useEffect, useState, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { FiList, FiX, FiTrash2, FiZap, FiPlus, FiSave, FiChevronRight, FiChevronDown, FiMessageSquare } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { SkeletonLine } from '../components/Skeleton';
import { useTriggerGoal } from '../hooks/useTriggerGoal';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
import SessionPickerModal from '../components/SessionPickerModal';
import ClarifyChat from '../components/ClarifyChat';

const STAGES = [
  { key: 'draft', label: 'Draft', action: 'Clarify', target: 'clarifying' },
  { key: 'clarifying', label: 'Clarifying', action: 'Plan', target: 'planned' },
  { key: 'planned', label: 'Planned', action: 'Execute', target: 'executing' },
  { key: 'executing', label: 'Executing', action: null, target: null },
  { key: 'done', label: 'Done', action: null, target: null },
];

// --- Prompt templates (pure functions, never produce 'null'/'undefined' strings) ---
// The clarify prompt (with the structured-summary marker instruction) now lives
// inside ClarifyChat, which owns the inline clarification conversation. Plan
// still composes its /dev-plan prompt here and navigates to /chat.

function planPrompt(task) {
  const subject = task.subject || 'Untitled';
  const context = task.clarification?.summary || task.description || '';
  const criteria = Array.isArray(task.clarification?.acceptanceCriteria)
    ? task.clarification.acceptanceCriteria.join('\n')
    : 'Not yet defined';
  return `/dev-plan ${subject}\n\nContext:\n${context}\n\nAcceptance criteria:\n${criteria}`;
}

// Build the slug Claude Code uses for a project's session directory.
function projectPathToSlug(path) {
  return '-' + path.replace(/^\//, '').replace(/\//g, '-');
}

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

// --- Task row with hover state ---
function TaskRow({ task, stage, isLast, isExpanded, openMenuId, setOpenMenuId, onPrimaryAction, onAdvance, onDelete, onTrigger, onToggleExpand }) {
  const [hovered, setHovered] = useState(false);
  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={(e) => {
        // Only expand when clicking the row itself, not buttons/menus
        if (e.target.closest('button') || e.target.closest('[data-no-expand]')) return;
        onToggleExpand(task.id);
      }}
      style={{
        padding: 'var(--space-sm) var(--space-md)',
        borderBottom: (isLast && !isExpanded) ? 'none' : '1px solid var(--border)',
        opacity: stage.key === 'done' ? 0.65 : 1,
        background: hovered ? 'var(--surface2)' : 'transparent',
        transition: 'background 0.15s',
        cursor: 'pointer',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-sm)' }}>
        {/* Priority badge (leftmost) */}
        <PriorityBadge priority={task.priority} />

        {/* Subject + description (flex-1, takes remaining space) */}
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{
            fontWeight: 600, fontSize: 'var(--fs-sm)',
            textDecoration: stage.key === 'done' ? 'line-through' : 'none',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {task.subject || `Task ${task.id}`}
          </div>
          {task.description && (
            <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: '2px', lineHeight: 1.4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {task.description}
            </div>
          )}
        </div>

        {/* Project badge */}
        {task.project && <span className="badge" style={{ flexShrink: 0 }}>{task.project}</span>}

        {/* Primary action button (filled) */}
        {stage.action && (
          <button
            onClick={(e) => { e.stopPropagation(); onPrimaryAction(task, stage); }}
            title={stage.key === 'draft' ? 'Advance to clarifying & open chat' : stage.key === 'clarifying' ? 'Open chat with dev-plan prompt' : `Advance to ${stage.target}`}
            style={{
              background: 'var(--accent)',
              color: '#fff',
              border: 'none',
              borderRadius: '4px',
              padding: '0.2em 0.6em',
              fontSize: 'var(--fs-xs)',
              fontWeight: 600,
              cursor: 'pointer',
              flexShrink: 0,
              whiteSpace: 'nowrap',
            }}
          >
            {stage.action} &rarr;
          </button>
        )}

        {/* Overflow menu (always present) */}
        <OverflowMenu
          task={task}
          stage={stage}
          openMenuId={openMenuId}
          setOpenMenuId={setOpenMenuId}
          onAdvance={onAdvance}
          onDelete={onDelete}
          onTrigger={onTrigger}
        />
      </div>
    </div>
  );
}

// --- Overflow "..." menu ---
function OverflowMenu({ task, stage, openMenuId, setOpenMenuId, onAdvance, onDelete, onTrigger }) {
  const menuRef = useRef(null);
  const isOpen = openMenuId === task.id;

  // Click-outside handler: close menu when clicking outside, without stopPropagation.
  useEffect(() => {
    if (!isOpen) return;
    const handleClick = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setOpenMenuId(null);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [isOpen, setOpenMenuId]);

  const menuItemStyle = {
    display: 'flex', alignItems: 'center', gap: 'var(--space-xs)',
    padding: 'var(--space-xs) var(--space-sm)',
    background: 'none', border: 'none', width: '100%', textAlign: 'left',
    cursor: 'pointer', fontSize: 'var(--fs-xs)', color: 'var(--text)',
    borderRadius: '3px',
  };

  return (
    <div ref={menuRef} style={{ position: 'relative', flexShrink: 0 }} data-no-expand>
      <button
        onClick={(e) => { e.stopPropagation(); setOpenMenuId(isOpen ? null : task.id); }}
        title="More actions"
        aria-label="More actions"
        style={{
          background: 'none', border: 'none', cursor: 'pointer',
          fontSize: '1.2em', lineHeight: 1, padding: '0.1em 0.3em',
          color: 'var(--muted)', borderRadius: '4px',
        }}
      >
        &#x22EF;
      </button>
      {isOpen && (
        <div style={{
          position: 'absolute', right: 0, top: '100%', zIndex: 100,
          background: 'var(--surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius)', boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
          minWidth: '150px', padding: 'var(--space-xs)',
          marginTop: '4px',
        }}>
          {/* Trigger Goal */}
          <button
            style={menuItemStyle}
            onClick={(e) => { e.stopPropagation(); onTrigger(task); setOpenMenuId(null); }}
          >
            <FiZap size={12} style={{ color: 'var(--accent)' }} /> Trigger Goal
          </button>
          {/* Mark Done -- hidden if already done */}
          {stage.key !== 'done' && (
            <button
              style={menuItemStyle}
              onClick={(e) => { e.stopPropagation(); onAdvance(task, 'done'); setOpenMenuId(null); }}
            >
              <span style={{ fontSize: '12px' }}>&#10003;</span> Mark Done
            </button>
          )}
          {/* Delete */}
          <button
            style={{ ...menuItemStyle, color: 'var(--danger, #d9534f)' }}
            onClick={(e) => { e.stopPropagation(); onDelete(task); setOpenMenuId(null); }}
          >
            <FiTrash2 size={12} /> Delete
          </button>
        </div>
      )}
    </div>
  );
}

// --- Inline detail panel for task ---
function TaskDetailPanel({ task, onSave, onApprovePlan, saving }) {
  const [planSpec, setPlanSpec] = useState(task.plan?.spec || '');

  // Sync local state when task data changes externally
  useEffect(() => {
    setPlanSpec(task.plan?.spec || '');
  }, [task.plan?.spec]);

  const hasPlanStored = !!task.plan?.spec;
  const stage = task.stage || 'draft';
  const clarSummary = task.clarification?.summary;

  return (
    <div style={{
      borderTop: '1px solid var(--border)',
      padding: 'var(--space-md)',
      background: 'var(--surface2)',
    }}>
      {/* Clarification: read-only summary if saved, else the inline mini-chat.
          A saved summary must NOT re-mount ClarifyChat — re-mounting would
          re-run Claude and could overwrite the saved scope (AC-9/AC-10). */}
      {clarSummary ? (
        <div style={{ marginBottom: 'var(--space-md)' }}>
          <label style={{ display: 'block', fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 'var(--space-xs)', fontWeight: 600 }}>
            Clarification Summary
          </label>
          <div className="markdown-body" style={{
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            padding: 'var(--space-sm)',
            fontSize: '0.92em',
            lineHeight: 1.6,
          }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
              {clarSummary}
            </ReactMarkdown>
          </div>
        </div>
      ) : stage === 'clarifying' ? (
        <div style={{ marginBottom: 'var(--space-md)' }}>
          <label style={{ display: 'block', fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 'var(--space-xs)', fontWeight: 600 }}>
            Clarification
          </label>
          <ClarifyChat task={task} onSave={onSave} />
        </div>
      ) : null}

      {/* Plan spec */}
      <div style={{ marginBottom: 'var(--space-md)' }}>
        <label style={{ display: 'block', fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 'var(--space-xs)', fontWeight: 600 }}>
          Plan Spec
        </label>
        {hasPlanStored ? (
          <div className="markdown-body" style={{ background: 'var(--card-bg)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 'var(--space-sm)', fontSize: '0.92em', lineHeight: 1.6, maxHeight: '300px', overflow: 'auto' }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
              {task.plan.spec}
            </ReactMarkdown>
          </div>
        ) : (
          <>
            <textarea
              className="form-textarea"
              value={planSpec}
              onChange={e => setPlanSpec(e.target.value)}
              placeholder="Paste the dev-plan output or type a plan spec..."
              style={{ minHeight: '80px', fontSize: 'var(--fs-sm)' }}
            />
          </>
        )}
      </div>

      {/* Approve Plan button - only visible when stage is clarifying */}
      {stage === 'clarifying' && (
        <button
          className="btn btn-primary"
          disabled={saving || (!hasPlanStored && !planSpec.trim())}
          onClick={() => onApprovePlan(task.id, hasPlanStored ? task.plan.spec : planSpec.trim())}
          style={{ fontSize: 'var(--fs-sm)', marginBottom: 'var(--space-sm)' }}
        >
          Approve Plan
        </button>
      )}

      {/* Session links */}
      {(task.clarification?.sessionId || task.plan?.sessionId) && (
        <div style={{ marginTop: 'var(--space-sm)', display: 'flex', gap: 'var(--space-md)', flexWrap: 'wrap' }}>
          {task.clarification?.sessionId && (
            <a
              href={`/sessions?id=${encodeURIComponent(task.clarification.sessionId)}`}
              style={{ fontSize: 'var(--fs-xs)', color: 'var(--accent)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
            >
              <FiMessageSquare size={12} /> Clarification session
            </a>
          )}
          {task.plan?.sessionId && (
            <a
              href={`/sessions?id=${encodeURIComponent(task.plan.sessionId)}`}
              style={{ fontSize: 'var(--fs-xs)', color: 'var(--accent)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
            >
              <FiMessageSquare size={12} /> Plan session
            </a>
          )}
        </div>
      )}
    </div>
  );
}

const POLL_MS = 60000;

export default function TasksPage() {
  const navigate = useNavigate();
  const [tasks, setTasks] = useState(null);
  const [projects, setProjects] = useState([]);
  const [error, setError] = useState(null);
  const [openMenuId, setOpenMenuId] = useState(null);
  const [expandedTaskId, setExpandedTaskId] = useState(null);
  const [saving, setSaving] = useState(false);

  // Filters
  const [projectFilter, setProjectFilter] = useState('all');
  const [stageFilter, setStageFilter] = useState(new Set());

  // Which stage groups are collapsed. Done starts folded.
  const [collapsedGroups, setCollapsedGroups] = useState({ done: true });
  const toggleGroup = (stage) =>
    setCollapsedGroups(prev => ({ ...prev, [stage]: !prev[stage] }));

  // Multi-session picker for "trigger goal" and "clarify/plan" navigation.
  const [picker, setPicker] = useState(null);
  const triggerGoal = useTriggerGoal({ onPickSession: setPicker });

  // Create-task form state.
  const [showNew, setShowNew] = useState(false);
  const [allProjects, setAllProjects] = useState([]);
  const [newSubject, setNewSubject] = useState('');
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

  // Advance a task's stage. Returns true on success, false on failure.
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
        return false;
      }
      setError(null);
      fetchTasks({ silent: true });
      return true;
    } catch {
      setError('Failed to advance task.');
      return false;
    }
  }, [fetchTasks]);

  // Navigate to chat with a pre-composed prompt, scoped to the task's project.
  const openChatForTask = useCallback(async (prompt, task) => {
    if (!task.projectPath) {
      navigate('/chat', { state: { prefillPrompt: prompt } });
      return;
    }
    const slug = projectPathToSlug(task.projectPath);
    let sessions = [];
    try {
      const res = await fetch(`/api/sessions?project=${encodeURIComponent(slug)}&limit=50`);
      const json = await res.json();
      sessions = Array.isArray(json.sessions) ? json.sessions : [];
    } catch {
      navigate('/chat', { state: { prefillPrompt: prompt } });
      return;
    }

    if (sessions.length === 0) {
      navigate('/chat', { state: { prefillPrompt: prompt } });
    } else if (sessions.length === 1) {
      const search = `?resume=${encodeURIComponent(sessions[0].id)}`;
      navigate(`/chat${search}`, { state: { prefillPrompt: prompt } });
    } else {
      // Multiple sessions: show picker
      setPicker({
        task,
        prompt,
        sessions,
        resume: (id) => {
          const search = `?resume=${encodeURIComponent(id)}`;
          navigate(`/chat${search}`, { state: { prefillPrompt: prompt } });
        },
      });
    }
  }, [navigate]);

  // Primary button action dispatch based on stage.
  const handlePrimaryAction = useCallback(async (task, stage) => {
    if (stage.key === 'draft') {
      // Clarify: advance THEN expand the detail panel on success (NO nav).
      // The inline ClarifyChat in the panel auto-sends the first prompt.
      // On failure advanceTask already setError(...) and returns false, so
      // the error banner shows and the panel does NOT open (no stuck state).
      const ok = await advanceTask(task, 'clarifying');
      if (ok) {
        setExpandedTaskId(task.id);
      }
    } else if (stage.key === 'clarifying') {
      // Plan: navigate only, NO advance
      openChatForTask(planPrompt(task), task);
    } else if (stage.key === 'planned') {
      // Execute: just advance stage (Phase 4 will wire to /dev-team)
      advanceTask(task, 'executing');
    }
  }, [advanceTask, openChatForTask]);

  // Save partial task fields via PUT.
  const saveTaskFields = useCallback(async (taskId, fields) => {
    setSaving(true);
    try {
      const res = await fetch('/api/tasks/user', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: taskId, ...fields }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.message || data.error || 'Failed to save.');
        return false;
      }
      setError(null);
      fetchTasks({ silent: true });
      return true;
    } catch {
      setError('Failed to save task.');
      return false;
    } finally {
      setSaving(false);
    }
  }, [fetchTasks]);

  // Approve plan: PUT the plan spec + approvedAt, THEN advance to planned.
  const approvePlan = useCallback(async (taskId, specText) => {
    setSaving(true);
    try {
      // Step 1: save plan spec + approvedAt
      const putRes = await fetch('/api/tasks/user', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: taskId, plan: { spec: specText, approvedAt: Date.now() } }),
      });
      if (!putRes.ok) {
        const data = await putRes.json().catch(() => ({}));
        setError(data.message || data.error || 'Failed to save plan.');
        return;
      }

      // Step 2: advance stage to planned
      const advRes = await fetch(`/api/tasks/user/${encodeURIComponent(taskId)}/advance`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage: 'planned' }),
      });
      if (!advRes.ok) {
        const data = await advRes.json().catch(() => ({}));
        setError(data.message || data.error || 'Plan saved but failed to advance stage.');
        return;
      }

      setError(null);
      setExpandedTaskId(null);
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to approve plan.');
    } finally {
      setSaving(false);
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
      if (expandedTaskId === task.id) setExpandedTaskId(null);
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to delete task.');
    }
  }, [fetchTasks, expandedTaskId]);

  // Create a new TODO.
  const createUserTask = useCallback(async () => {
    if (!newSubject.trim()) return;
    try {
      const res = await fetch('/api/tasks/user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          subject: newSubject.trim(),
          project: newProject,
          priority: newPriority,
        }),
      });
      if (!res.ok) { setError('Failed to create task.'); return; }
      setShowNew(false);
      setNewSubject('');
      setNewProject('');
      setNewPriority('p2');
      setError(null);
      fetchTasks({ silent: true });
    } catch {
      setError('Failed to create task.');
    }
  }, [newSubject, newProject, newPriority, fetchTasks]);

  // Stage filter toggle.
  const toggleStageFilter = (key) => {
    setStageFilter(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Toggle expanded detail panel.
  const toggleExpand = (taskId) => {
    setExpandedTaskId(prev => prev === taskId ? null : taskId);
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
            <label className="form-label">Idea</label>
            <input
              className="form-input"
              placeholder="What do you want to do? (you'll refine it in Clarify)"
              value={newSubject}
              onChange={e => setNewSubject(e.target.value)}
              autoFocus
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
            <button className="btn" onClick={() => { setShowNew(false); setNewSubject(''); setNewProject(''); setNewPriority('p2'); }}>
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
                <div className="card" style={{ padding: 0 }}>
                  {items.map((t, i) => (
                    <div key={t.id}>
                      <TaskRow
                        task={t}
                        stage={stage}
                        isLast={i === items.length - 1 && expandedTaskId !== t.id}
                        isExpanded={expandedTaskId === t.id}
                        openMenuId={openMenuId}
                        setOpenMenuId={setOpenMenuId}
                        onPrimaryAction={handlePrimaryAction}
                        onAdvance={advanceTask}
                        onDelete={deleteTask}
                        onTrigger={triggerGoal}
                        onToggleExpand={toggleExpand}
                      />
                      {expandedTaskId === t.id && (
                        <TaskDetailPanel
                          task={t}
                          onSave={saveTaskFields}
                          onApprovePlan={approvePlan}
                          saving={saving}
                        />
                      )}
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
