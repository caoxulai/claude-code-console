import { useEffect, useState, useMemo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { FiChevronDown, FiChevronRight, FiEdit3, FiSave, FiPlus, FiCode, FiCopy, FiCheck, FiZap, FiCheckCircle, FiTrash2 } from 'react-icons/fi';
import { SkeletonCard } from '../components/Skeleton';
import { useTriggerGoal } from '../hooks/useTriggerGoal';
import SessionPickerModal from '../components/SessionPickerModal';

// Small copy-to-clipboard icon button matching the app's icon-btn style.
// Shows a brief check-mark confirmation after a successful copy. Stops click
// propagation so copying inside a clickable card header doesn't toggle the card.
function CopyButton({ text, title = 'Copy' }) {
  const [copied, setCopied] = useState(false);

  const copy = async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for non-secure contexts where the Clipboard API is unavailable.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <button
      className="icon-btn"
      onClick={copy}
      title={copied ? 'Copied!' : title}
      aria-label={copied ? 'Copied' : title}
      style={{ padding: 3, flexShrink: 0 }}
    >
      {copied ? <FiCheck size={12} style={{ color: 'var(--accent)' }} /> : <FiCopy size={12} />}
    </button>
  );
}

const TABS = ['Overview', 'README', 'CLAUDE.md', 'Memory', 'Skills/SOPs', 'Tasks'];

function parseFrontmatter(content) {
  if (!content) return { meta: null, body: content };
  const match = content.match(/^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/);
  if (!match) return { meta: null, body: content };
  const raw = match[1];
  const body = match[2].trim();
  const meta = {};
  let currentKey = null;
  for (const line of raw.split('\n')) {
    const kv = line.match(/^(\w[\w-]*):\s*(.*)$/);
    if (kv) {
      currentKey = kv[1];
      const val = kv[2].replace(/^["']|["']$/g, '').trim();
      if (val) meta[currentKey] = val;
    } else if (currentKey && line.match(/^\s+\w/)) {
      const nested = line.match(/^\s+(\w[\w-]*):\s*(.*)$/);
      if (nested) {
        if (typeof meta[currentKey] !== 'object') meta[currentKey] = {};
        meta[currentKey][nested[1]] = nested[2].replace(/^["']|["']$/g, '').trim();
      }
    }
  }
  return { meta, body };
}

function typeBadgeClass(type) {
  const map = { feedback: 'badge-feedback', user: 'badge-user', project: 'badge-project', reference: 'badge-reference' };
  return map[type] || '';
}

function MemoryContentView({ content }) {
  const { meta, body } = useMemo(() => parseFrontmatter(content), [content]);
  const description = meta?.description;
  return (
    <div>
      {description && (
        <div style={{
          marginBottom: '1em',
          padding: '0.75em 1em',
          background: 'var(--surface2)',
          borderRadius: 'var(--radius)',
          border: '1px solid var(--border)',
        }}>
          <div style={{ fontSize: '0.85em', color: 'var(--muted)', lineHeight: 1.5 }}>
            {description}
          </div>
        </div>
      )}
      <div className="markdown-body" style={{ fontSize: '0.92em', lineHeight: 1.6 }}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{body}</ReactMarkdown>
      </div>
    </div>
  );
}

function projectPathToSlug(path) {
  return path.replace(/\//g, "-");
}

// Format a raw epoch timestamp (seconds or ms) into a compact relative-time
// label like "3d ago". Returns null when the value is missing/unparseable so
// callers can fall back to an em-dash.
function formatRelativeTime(ts) {
  if (ts === null || ts === undefined) return null;
  const num = Number(ts);
  if (!Number.isFinite(num) || num <= 0) return null;
  // Accept seconds or milliseconds.
  const ms = num < 1e12 ? num * 1000 : num;
  const diff = Date.now() - ms;
  if (diff < 0) return 'just now';
  const min = Math.floor(diff / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.floor(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

const CLAUDE_MD_TEMPLATE = `# Project Instructions

<!-- Add project-specific instructions for Claude Code here -->

## Build & Test
- \`npm install\` to install dependencies
- \`npm run dev\` to start dev server
- \`npm test\` to run tests

## Architecture
<!-- Describe key patterns, conventions, important files -->

## Guidelines
<!-- Coding standards, naming conventions, etc. -->
`;

export default function ProjectsPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(searchParams.get('expand') || null);
  const [activeTab, setActiveTab] = useState('Overview');

  // CLAUDE.md editing state
  const [editingClaudeMd, setEditingClaudeMd] = useState(false);
  const [claudeMdDraft, setClaudeMdDraft] = useState('');

  // Memory state
  const [selectedMemory, setSelectedMemory] = useState(null);
  const [memoryContent, setMemoryContent] = useState('');
  const [memoryEtag, setMemoryEtag] = useState(null);
  const [editingMemory, setEditingMemory] = useState(false);
  const [memoryDraft, setMemoryDraft] = useState('');

  // SOP state
  const [selectedSop, setSelectedSop] = useState(null);
  const [sopContent, setSopContent] = useState('');

  // Tasks state (loaded per-project when the Tasks tab opens)
  const [projectTasks, setProjectTasks] = useState([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [picker, setPicker] = useState(null);
  const triggerGoal = useTriggerGoal({ onPickSession: setPicker });

  const fetchProjects = async () => {
    try {
      const res = await fetch('/api/projects');
      const json = await res.json();
      setProjects(json);
    } catch { /* ignore */ }
    setLoading(false);
  };

  useEffect(() => { fetchProjects(); }, []);

  // Load the expanded project's tasks whenever the Tasks tab is active.
  useEffect(() => {
    if (activeTab !== 'Tasks' || !expandedId) return;
    const project = projects.find(p => p.id === expandedId);
    if (project) loadProjectTasks(project);
  }, [activeTab, expandedId, projects]);

  const toggleCard = (projectId) => {
    if (expandedId === projectId) {
      setExpandedId(null);
    } else {
      setExpandedId(projectId);
      setActiveTab('Overview');
      setEditingClaudeMd(false);
      setSelectedMemory(null);
      setMemoryContent('');
      setEditingMemory(false);
      setSelectedSop(null);
      setSopContent('');
      setProjectTasks([]);
    }
  };

  // --- CLAUDE.md actions ---

  const startEditClaudeMd = (project) => {
    setEditingClaudeMd(true);
    setClaudeMdDraft(project.claudeMd || '');
  };

  const createClaudeMd = async (project) => {
    try {
      await fetch(`/api/projects/${encodeURIComponent(project.id)}/claude-md`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: CLAUDE_MD_TEMPLATE }),
      });
      await fetchProjects();
      setEditingClaudeMd(false);
    } catch { /* ignore */ }
  };

  const saveClaudeMd = async (project) => {
    try {
      await fetch(`/api/projects/${encodeURIComponent(project.id)}/claude-md`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: claudeMdDraft }),
      });
      setEditingClaudeMd(false);
      await fetchProjects();
    } catch { /* ignore */ }
  };

  // --- Memory actions ---

  const selectMemoryFile = async (project, name) => {
    setSelectedMemory(name);
    setEditingMemory(false);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(project.id)}/memory/${encodeURIComponent(name)}`);
      const json = await res.json();
      setMemoryContent(json.content);
      setMemoryEtag(json.etag);
      setMemoryDraft(json.content);
    } catch {
      setMemoryContent('Failed to load file.');
      setMemoryEtag(null);
    }
  };

  const saveMemoryFile = async (project) => {
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(project.id)}/memory/${encodeURIComponent(selectedMemory)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: memoryDraft, etag: memoryEtag }),
      });
      if (res.status === 409) {
        alert('File changed externally. Reload and retry.');
        return;
      }
      const json = await res.json();
      setMemoryEtag(json.etag);
      setMemoryContent(memoryDraft);
      setEditingMemory(false);
    } catch { /* ignore */ }
  };

  // --- SOP actions ---

  const selectSopFile = async (project, filename) => {
    setSelectedSop(filename);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(project.id)}/sop/${encodeURIComponent(filename)}`);
      const json = await res.json();
      setSopContent(json.content);
    } catch {
      setSopContent('Failed to load SOP file.');
    }
  };

  // --- Tasks actions ---

  // Tasks are filtered by project NAME (how /api/tasks attributes them). A
  // project's directory name is its project name, so project.name is the key.
  const loadProjectTasks = async (project, { silent = false } = {}) => {
    if (!silent) setTasksLoading(true);
    try {
      const res = await fetch(`/api/tasks?project=${encodeURIComponent(project.name)}&includeDismissed=1`);
      const json = await res.json();
      setProjectTasks(Array.isArray(json.tasks) ? json.tasks : []);
    } catch {
      if (!silent) setProjectTasks([]);
    }
    if (!silent) setTasksLoading(false);
  };

  const completeProjectTask = async (project, task) => {
    try {
      await fetch('/api/tasks/complete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: task._sessionId, taskId: task.id }),
      });
      loadProjectTasks(project, { silent: true });
    } catch { /* ignore */ }
  };

  const deleteProjectTask = async (project, task) => {
    if (!window.confirm(`Delete task "${task.subject || task.id}"?\n\nThis permanently removes the task file.`)) return;
    try {
      await fetch('/api/tasks/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: task._sessionId, taskId: task.id }),
      });
      loadProjectTasks(project, { silent: true });
    } catch { /* ignore */ }
  };

  // --- Render helpers ---

  const renderCollapsedHeader = (project) => {
    const isExpanded = expandedId === project.id;
    return (
      <div
        onClick={() => toggleCard(project.id)}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '0.6em',
          cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        <span style={{ marginTop: '0.15em', color: 'var(--muted)', flexShrink: 0 }}>
          {isExpanded ? <FiChevronDown size={16} /> : <FiChevronRight size={16} />}
        </span>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 700, fontSize: 'var(--fs-lg)' }}>
              {project.name}
            </span>
            {(project.appUrls || []).map((u, idx) => (
              <a
                key={idx}
                href={u.url}
                target="_blank"
                rel="noopener noreferrer"
                title={u.url}
                onClick={(e) => e.stopPropagation()}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '0.3em',
                  background: u.type === 'deployed' ? 'var(--pill-deployed-bg)' : 'var(--pill-local-bg)',
                  color: u.type === 'deployed' ? 'var(--pill-deployed-text)' : 'var(--pill-local-text)',
                  fontSize: '0.72em',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: '999px',
                  border: `1px solid ${u.type === 'deployed' ? 'var(--pill-deployed-border)' : 'var(--pill-local-border)'}`,
                  textDecoration: 'none',
                }}
              >
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: u.type === 'deployed' ? 'var(--pill-deployed-text)' : 'var(--pill-local-text)', display: 'inline-block' }} />
                {u.label}
              </a>
            ))}
            {project.codeUrl && (
              <a
                href={project.codeUrl}
                target="_blank"
                rel="noopener noreferrer"
                title={project.codeUrl}
                onClick={(e) => e.stopPropagation()}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '0.3em',
                  background: 'var(--pill-dev-bg)',
                  color: 'var(--pill-dev-text)',
                  fontSize: '0.72em',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: '999px',
                  border: '1px solid var(--pill-dev-border)',
                  textDecoration: 'none',
                }}
              >
                <FiCode size={11} />
                Code Repo
              </a>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.25em', marginTop: '0.2em', minWidth: 0 }}>
            <span style={{
              fontSize: 'var(--fs-xs)',
              color: 'var(--muted)',
              fontFamily: 'monospace',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>
              {project.path}
            </span>
            <CopyButton text={project.path} title="Copy directory path" />
          </div>
        </div>

        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: '0.5em',
          flexWrap: 'wrap',
          justifyContent: 'flex-end',
          flexShrink: 0,
        }}>
          <span
            className={`badge ${project.sessionCount > 0 ? 'badge-ok' : ''}`}
            onClick={(e) => {
              e.stopPropagation();
              const slug = projectPathToSlug(project.path);
              navigate(`/sessions?project=${slug}`);
            }}
            style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
            onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
            onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
          >
            {project.sessionCount} sessions
          </span>
          <span
            className="badge"
            onClick={(e) => {
              e.stopPropagation();
              setExpandedId(project.id);
              setActiveTab('Memory');
            }}
            style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
            onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
            onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
          >
            {project.memoryCount} memories
          </span>
          {(project.sopFiles || []).length > 0 && (
            <span
              className="badge"
              onClick={(e) => {
                e.stopPropagation();
                setExpandedId(project.id);
                setActiveTab('Skills/SOPs');
              }}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {(project.sopFiles || []).length} {(project.sopFiles || []).length === 1 ? 'SOP' : 'SOPs'}
            </span>
          )}
          {project.taskCount > 0 && (
            <span
              className={`badge ${project.openTaskCount > 0 ? 'badge-warn' : ''}`}
              onClick={(e) => {
                e.stopPropagation();
                setExpandedId(project.id);
                setActiveTab('Tasks');
              }}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {project.openTaskCount > 0 ? `${project.openTaskCount} open tasks` : `${project.taskCount} tasks`}
            </span>
          )}
          {project.hasSettings && (
            <span className="badge badge-ok">has settings</span>
          )}
          {formatRelativeTime(project.lastActivityTs) && (
            <span
              className="badge"
              title="Most recent session activity"
              onClick={(e) => {
                e.stopPropagation();
                const slug = projectPathToSlug(project.path);
                navigate(`/sessions?project=${slug}`);
              }}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              active {formatRelativeTime(project.lastActivityTs)}
            </span>
          )}
        </div>
      </div>
    );
  };

  const renderTabBar = () => (
    <div style={{
      display: 'flex',
      gap: 0,
      borderBottom: '1px solid var(--border)',
      marginBottom: 'var(--space-md)',
    }}>
      {TABS.map(tab => (
        <button
          key={tab}
          onClick={() => {
            setActiveTab(tab);
            setEditingClaudeMd(false);
            setSelectedMemory(null);
            setEditingMemory(false);
            setSelectedSop(null);
          }}
          style={{
            padding: '0.5em 1em',
            fontSize: 'var(--fs-sm)',
            fontWeight: activeTab === tab ? 600 : 400,
            color: activeTab === tab ? 'var(--accent)' : 'var(--muted)',
            background: 'none',
            border: 'none',
            borderBottom: activeTab === tab ? '2px solid var(--accent)' : '2px solid transparent',
            cursor: 'pointer',
            transition: 'color 0.15s, border-color 0.15s',
          }}
        >
          {tab}
        </button>
      ))}
    </div>
  );

  const statCardStyle = {
    background: 'var(--surface2)',
    borderRadius: 'var(--radius)',
    padding: 'var(--space-md)',
    cursor: 'pointer',
    border: '1px solid var(--border)',
    transition: 'border-color 0.15s, background 0.15s',
    textAlign: 'center',
  };

  const renderOverviewTab = (project) => (
    <div style={{ fontSize: 'var(--fs-sm)' }}>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
        gap: 'var(--space-md)',
        marginBottom: 'var(--space-md)',
      }}>
        <div
          onClick={() => navigate(`/sessions?project=${projectPathToSlug(project.path)}`)}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.sessionCount}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>Sessions</div>
        </div>
        <div
          onClick={() => setActiveTab('Memory')}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.memoryCount}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>Memory Files</div>
        </div>
        <div
          onClick={() => setActiveTab('Skills/SOPs')}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{(project.sopFiles || []).length}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>Skills/SOPs</div>
        </div>
        <div
          onClick={() => setActiveTab('Tasks')}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.taskCount ?? 0}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>
            Tasks{project.openTaskCount ? ` · ${project.openTaskCount} open` : ''}
          </div>
        </div>
        <div
          onClick={() => navigate(`/sessions?project=${projectPathToSlug(project.path)}`)}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{formatRelativeTime(project.lastActivityTs) || '—'}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>Last Activity</div>
        </div>
      </div>

      {project.hasSettings && (
        <div style={{ color: 'var(--muted)', fontStyle: 'italic', fontSize: 'var(--fs-xs)' }}>
          Project-level configuration (.claude/settings.json) present
        </div>
      )}
    </div>
  );

  const renderReadmeTab = (project) => {
    if (!project.readme) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3>No README</h3>
          <p style={{ color: 'var(--muted)' }}>
            No README found in this project&apos;s root directory.
          </p>
        </div>
      );
    }

    return (
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: 'var(--space-sm)' }}>
          <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)', fontFamily: 'monospace', color: 'var(--muted)' }}>
            {project.readmeName || 'README.md'}
          </span>
        </div>
        <div className="markdown-body" style={{ fontSize: 'var(--fs-sm)' }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {project.readme}
          </ReactMarkdown>
        </div>
      </div>
    );
  };

  const renderClaudeMdTab = (project) => {
    if (!project.claudeMd && !editingClaudeMd) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3 style={{ marginBottom: 'var(--space-sm)' }}>No CLAUDE.md</h3>
          <p style={{ color: 'var(--muted)', marginBottom: 'var(--space-md)' }}>
            No CLAUDE.md exists for this project. Create one to give Claude project-specific instructions.
          </p>
          <button className="btn btn-primary" onClick={() => createClaudeMd(project)}>
            <FiPlus size={14} /> Create CLAUDE.md
          </button>
        </div>
      );
    }

    if (editingClaudeMd) {
      return (
        <div>
          <textarea
            className="form-textarea"
            value={claudeMdDraft}
            onChange={e => setClaudeMdDraft(e.target.value)}
            style={{ minHeight: '300px', fontFamily: 'monospace', fontSize: 'var(--fs-sm)' }}
          />
          <div style={{ display: 'flex', gap: 'var(--space-sm)', marginTop: 'var(--space-sm)' }}>
            <button className="btn btn-primary" onClick={() => saveClaudeMd(project)}>
              <FiSave size={13} /> Save
            </button>
            <button className="btn" onClick={() => setEditingClaudeMd(false)}>
              Cancel
            </button>
          </div>
        </div>
      );
    }

    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 'var(--space-sm)' }}>
          <button className="btn" onClick={() => startEditClaudeMd(project)} style={{ fontSize: 'var(--fs-xs)' }}>
            <FiEdit3 size={13} /> Edit
          </button>
        </div>
        <div className="markdown-body" style={{ fontSize: 'var(--fs-sm)' }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {project.claudeMd}
          </ReactMarkdown>
        </div>
      </div>
    );
  };

  const renderMemoryTab = (project) => {
    const memoryFiles = project.memoryFiles || [];

    if (memoryFiles.length === 0) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3>No memory files</h3>
          <p style={{ color: 'var(--muted)' }}>
            Memory files will appear here once Claude creates them for this project.
          </p>
        </div>
      );
    }

    return (
      <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 'var(--space-md)', minHeight: '300px' }}>
        {/* File list */}
        <div style={{
          background: 'var(--bg)',
          borderRadius: 'var(--radius)',
          padding: 'var(--space-sm)',
          overflowY: 'auto',
          maxHeight: '400px',
        }}>
          {memoryFiles.map(f => (
            <div
              key={f.name}
              onClick={() => selectMemoryFile(project, f.name)}
              style={{
                padding: '0.4em 0.6em',
                cursor: 'pointer',
                borderRadius: 'var(--radius)',
                background: selectedMemory === f.name ? 'var(--user-bg)' : 'transparent',
                fontSize: 'var(--fs-xs)',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                marginBottom: '2px',
              }}
            >
              {f.name}
            </div>
          ))}
        </div>

        {/* Content area */}
        <div style={{ minWidth: 0 }}>
          {!selectedMemory ? (
            <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>
              Select a memory file to view its contents.
            </div>
          ) : editingMemory ? (
            <div>
              <textarea
                className="form-textarea"
                value={memoryDraft}
                onChange={e => setMemoryDraft(e.target.value)}
                style={{ minHeight: '250px', fontFamily: 'monospace', fontSize: 'var(--fs-xs)' }}
              />
              <div style={{ display: 'flex', gap: 'var(--space-sm)', marginTop: 'var(--space-sm)' }}>
                <button className="btn btn-primary" onClick={() => saveMemoryFile(project)}>
                  <FiSave size={13} /> Save
                </button>
                <button className="btn" onClick={() => setEditingMemory(false)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-sm)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                  <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)' }}>{selectedMemory}</span>
                  {(() => {
                    const { meta } = parseFrontmatter(memoryContent);
                    const t = meta?.metadata?.type || meta?.type;
                    return t ? <span className={`badge ${typeBadgeClass(t)}`}>{t}</span> : null;
                  })()}
                </div>
                <button className="btn" onClick={() => { setEditingMemory(true); setMemoryDraft(memoryContent); }} style={{ fontSize: 'var(--fs-xs)' }}>
                  <FiEdit3 size={13} /> Edit
                </button>
              </div>
              <MemoryContentView content={memoryContent} />
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderSopsTab = (project) => {
    const sopFiles = project.sopFiles || [];

    if (sopFiles.length === 0) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3>No Skills or SOPs</h3>
          <p style={{ color: 'var(--muted)' }}>
            Add .md files in agent-sops/, skills/, or docs/ to see them here.
          </p>
        </div>
      );
    }

    return (
      <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 'var(--space-md)', minHeight: '300px' }}>
        {/* File list */}
        <div style={{
          background: 'var(--bg)',
          borderRadius: 'var(--radius)',
          padding: 'var(--space-sm)',
          overflowY: 'auto',
          maxHeight: '400px',
        }}>
          {sopFiles.map(f => (
            <div
              key={f.name}
              onClick={() => selectSopFile(project, f.name)}
              style={{
                padding: '0.4em 0.6em',
                cursor: 'pointer',
                borderRadius: 'var(--radius)',
                background: selectedSop === f.name ? 'var(--user-bg)' : 'transparent',
                fontSize: 'var(--fs-xs)',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                marginBottom: '2px',
              }}
            >
              {f.name}
            </div>
          ))}
        </div>

        {/* Content area */}
        <div style={{ minWidth: 0 }}>
          {!selectedSop ? (
            <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>
              Select a skill or SOP file to view.
            </div>
          ) : (
            <div>
              <div style={{ fontWeight: 600, fontSize: 'var(--fs-sm)', marginBottom: 'var(--space-sm)' }}>
                {selectedSop}
              </div>
              <div className="markdown-body" style={{ fontSize: 'var(--fs-sm)' }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                  {sopContent}
                </ReactMarkdown>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderTasksTab = (project) => {
    if (tasksLoading) {
      return <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>Loading tasks…</div>;
    }
    if (projectTasks.length === 0) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3>No tasks</h3>
          <p style={{ color: 'var(--muted)' }}>
            Tasks Claude Code creates in this project&apos;s sessions appear here.
          </p>
        </div>
      );
    }

    const statusBadge = (s) => {
      if (s === 'completed') return 'badge-ok';
      if (s === 'in_progress') return 'badge-warn';
      return '';
    };

    return (
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {projectTasks.map((t, i) => (
          <div
            key={`${t._sessionId}-${t.id}`}
            style={{
              padding: '0.7em 1em',
              borderBottom: i < projectTasks.length - 1 ? '1px solid var(--border)' : 'none',
              opacity: t.dismissed ? 0.5 : t.status === 'completed' ? 0.7 : 1,
              display: 'flex', justifyContent: 'space-between', gap: '1em', alignItems: 'flex-start',
            }}
          >
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: '0.9em', textDecoration: t.status === 'completed' ? 'line-through' : 'none' }}>
                {t.subject || `Task ${t.id}`}
              </div>
              {t.description && (
                <div style={{ fontSize: '0.78em', color: 'var(--muted)', marginTop: '0.2em', lineHeight: 1.5 }}>{t.description}</div>
              )}
            </div>
            <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.3em' }}>
              <span className={`badge ${statusBadge(t.status)}`}>{t.status}</span>
              <div style={{ display: 'flex', gap: '0.6em', alignItems: 'center' }}>
                <button onClick={() => triggerGoal(t)} title="Trigger a /goal in this project" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', display: 'flex', alignItems: 'center', gap: '0.2em', fontSize: '0.7em' }}>
                  <FiZap size={11} /> goal
                </button>
                {t.status !== 'completed' && (
                  <button onClick={() => completeProjectTask(project, t)} title="Mark complete" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: '0.2em', fontSize: '0.7em' }}>
                    <FiCheckCircle size={11} /> done
                  </button>
                )}
                <button onClick={() => deleteProjectTask(project, t)} title="Delete task file" style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--danger, #d9534f)', display: 'flex', alignItems: 'center', gap: '0.2em', fontSize: '0.7em' }}>
                  <FiTrash2 size={11} /> delete
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    );
  };

  const renderExpandedContent = (project) => {
    if (expandedId !== project.id) return null;

    return (
      <div style={{
        borderTop: '1px solid var(--border)',
        marginTop: 'var(--space-md)',
        paddingTop: 'var(--space-md)',
      }}>
        {renderTabBar()}
        {activeTab === 'Overview' && renderOverviewTab(project)}
        {activeTab === 'README' && renderReadmeTab(project)}
        {activeTab === 'CLAUDE.md' && renderClaudeMdTab(project)}
        {activeTab === 'Memory' && renderMemoryTab(project)}
        {activeTab === 'Skills/SOPs' && renderSopsTab(project)}
        {activeTab === 'Tasks' && renderTasksTab(project)}
      </div>
    );
  };

  return (
    <div>
      <div className="page-header">
        <h2>Projects</h2>
      </div>

      {loading ? (
        <div style={{ display: 'grid', gap: 'var(--space-md)' }}>
          {Array.from({ length: 3 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      ) : projects.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <h3>No projects found</h3>
            <p>Projects will appear here once you start using Claude Code in a directory.</p>
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 'var(--space-md)' }}>
          {projects.map(project => (
            <div
              key={project.id}
              className="card"
              style={{
                transition: 'border-color 0.15s',
                borderColor: expandedId === project.id ? 'var(--accent)' : undefined,
              }}
            >
              {renderCollapsedHeader(project)}
              {renderExpandedContent(project)}
            </div>
          ))}
        </div>
      )}

      {picker && (
        <SessionPickerModal picker={picker} onClose={() => setPicker(null)} />
      )}
    </div>
  );
}
