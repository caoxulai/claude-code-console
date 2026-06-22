import { useEffect, useState, useMemo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { FiChevronDown, FiChevronRight, FiEdit3, FiSave, FiPlus, FiCode, FiCopy, FiCheck, FiZap, FiCheckCircle, FiTrash2, FiUser, FiGlobe, FiFolder, FiCpu, FiArrowUpCircle, FiArrowDownCircle, FiAlertTriangle } from 'react-icons/fi';
import { SkeletonCard } from '../components/Skeleton';
import { useTriggerGoal } from '../hooks/useTriggerGoal';
import SessionPickerModal from '../components/SessionPickerModal';
import AgentContextPanel from '../components/AgentContextPanel';

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

const TABS = ['Overview', 'README', 'Docs', 'Proposals', 'Tasks', 'Agents', 'Memory', 'CLAUDE.md'];

// Map an ADR status to a badge class. `accepted` reads as success, `proposed`
// as needs-attention (it's awaiting Xulai's review), `superseded` as muted.
function adrStatusBadgeClass(status) {
  if (status === 'accepted') return 'badge-ok';
  if (status === 'proposed') return 'badge-warn';
  return '';
}

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

// Make a clickable <span> badge behave like a button for keyboard users:
// focusable + Enter/Space activates it. `activate` receives the keyboard event
// (it already calls stopPropagation/navigates). Spread onto the span.
function clickableBadgeProps(activate) {
  return {
    role: 'button',
    tabIndex: 0,
    onKeyDown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activate(e);
      }
    },
  };
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

  // Docs state
  const [selectedSop, setSelectedSop] = useState(null);
  const [sopContent, setSopContent] = useState('');

  // Proposals state (loaded per-project when the Proposals tab opens)
  const [design, setDesign] = useState(null);
  const [designLoading, setDesignLoading] = useState(false);

  // Agents state (loaded per-project when the Agents tab opens)
  const [agents, setAgents] = useState([]);
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [agentBusy, setAgentBusy] = useState(null); // slug currently moving scope
  const [expandedAgent, setExpandedAgent] = useState(null); // slug whose detail is open
  const [agentDetail, setAgentDetail] = useState(null); // loaded definition of expandedAgent
  const [agentView, setAgentView] = useState('definition'); // 'definition' | 'context'

  // Tasks state (loaded per-project when the Tasks tab opens)
  const [projectTasks, setProjectTasks] = useState([]);
  const [agentTasks, setAgentTasks] = useState([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [agentTasksLoading, setAgentTasksLoading] = useState(false);
  const [picker, setPicker] = useState(null);
  const triggerGoal = useTriggerGoal({ onPickSession: setPicker });

  const fetchProjects = async () => {
    try {
      const res = await fetch('/api/projects');
      const json = await res.json();
      // Carry forward any already-loaded /detail body so an unrelated listing
      // refresh (e.g. after a design/agent edit) doesn't blank an open
      // README/CLAUDE.md tab and refetch. The save/create handlers explicitly
      // invalidate the edited project's detail when its body actually changed.
      setProjects(prev => {
        const cache = new Map(prev.map(p => [p.id, p]));
        return (Array.isArray(json) ? json : []).map(p => {
          const old = cache.get(p.id);
          if (old?.detailLoaded) {
            return { ...p, claudeMd: old.claudeMd, readme: old.readme, readmeName: old.readmeName, detailLoaded: true };
          }
          return p;
        });
      });
    } catch { /* ignore */ }
    setLoading(false);
  };

  useEffect(() => { fetchProjects(); }, []);

  // The projects LISTING no longer inlines the full CLAUDE.md / README bodies
  // (it carries only a short `description`); the full content is fetched
  // on-demand from GET /api/projects/{id}/detail when a project's README or
  // CLAUDE.md tab is opened. We merge the bodies onto the in-memory project
  // object and flag `detailLoaded` so the tabs can gate their empty-states on
  // detail HAVING loaded — critically, the "Create CLAUDE.md" button and the
  // "No README" state must NEVER show while detail is still in flight (showing
  // Create for a project that HAS a CLAUDE.md would template-overwrite the real
  // file on click). Cached on the object: re-expanding does not refetch.
  const loadProjectDetail = async (projectId) => {
    const current = projects.find(p => p.id === projectId);
    if (!current || current.detailLoaded || current.detailLoading) return;
    // Mark in-flight so concurrent triggers (toggleCard + the tab effect) don't
    // double-fetch.
    setProjects(prev => prev.map(p => p.id === projectId ? { ...p, detailLoading: true } : p));
    let detail = {};
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/detail`);
      // Only trust a real JSON detail payload. If the endpoint isn't present
      // yet (the SPA fallback serves index.html, content-type text/html), keep
      // whatever the listing already provided rather than blanking the body.
      const ct = res.headers.get('content-type') || '';
      if (res.ok && ct.includes('application/json')) {
        const json = await res.json();
        if (json && typeof json === 'object') {
          // Merge only the detail-owned fields, and only when present, so a
          // partial/old payload can't clobber listing-provided content.
          for (const key of ['claudeMd', 'readme', 'readmeName']) {
            if (key in json) detail[key] = json[key];
          }
        }
      }
    } catch { /* keep listing-provided fields */ }
    setProjects(prev => prev.map(p =>
      p.id === projectId ? { ...p, ...detail, detailLoaded: true, detailLoading: false } : p
    ));
  };

  // Load the expanded project's tasks whenever the Tasks tab is active.
  useEffect(() => {
    if (activeTab !== 'Tasks' || !expandedId) return;
    const project = projects.find(p => p.id === expandedId);
    if (project) loadProjectTasks(project);
  }, [activeTab, expandedId, projects]);

  // Load the expanded project's design decisions when the Proposals tab is active.
  useEffect(() => {
    if (activeTab !== 'Proposals' || !expandedId) return;
    loadProjectDesign(expandedId);
  }, [activeTab, expandedId]);

  // Load the expanded project's agents when the Agents tab is active.
  useEffect(() => {
    if (activeTab !== 'Agents' || !expandedId) return;
    loadProjectAgents(expandedId);
  }, [activeTab, expandedId]);

  // Lazy-load the full CLAUDE.md / README bodies when either content tab opens.
  // (`loadProjectDetail` no-ops if already loaded/in-flight.) This covers both
  // the expand path and the badges that jump straight to a tab via
  // setExpandedId without going through toggleCard.
  useEffect(() => {
    if (!expandedId) return;
    if (activeTab === 'README' || activeTab === 'CLAUDE.md') loadProjectDetail(expandedId);
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
      setAgentTasks([]);
      setDesign(null);
      setAgents([]);
      setExpandedAgent(null);
      setAgentDetail(null);
      // Warm the full CLAUDE.md / README bodies so the content tabs render
      // instantly when opened (no-ops if already cached on the object).
      loadProjectDetail(projectId);
    }
  };

  // --- Design decisions actions ---

  // Fetch the parsed ADR list for a project. The endpoint returns
  // {exists, content, etag, decisions:[{id,title,date,status,body}]}.
  const loadProjectDesign = async (projectId) => {
    setDesignLoading(true);
    setDesign(null);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/design`);
      const json = await res.json();
      setDesign(json);
    } catch {
      setDesign({ exists: false, decisions: [] });
    }
    setDesignLoading(false);
  };

  // Accept (proposed → accepted) or reject (remove) a proposed ADR. Both are
  // text edits to DESIGN.md, PUT back etag-guarded. We rewrite just the matching
  // `## D-NNN: ... (date, status)` header line (accept) or drop the whole entry
  // block (reject), preserving everything else in the file.
  const updateProposedAdr = async (projectId, adrId, mode) => {
    if (!design?.content) return;
    const lines = design.content.split('\n');
    const headerIdx = lines.findIndex(l => new RegExp(`^##\\s+${adrId}:`).test(l));
    if (headerIdx === -1) return;
    let next;
    if (mode === 'accept') {
      next = [...lines];
      next[headerIdx] = next[headerIdx].replace(/\bproposed\b/, 'accepted');
    } else {
      // reject: drop from this header to the line before the next "## " header.
      let end = headerIdx + 1;
      while (end < lines.length && !/^##\s/.test(lines[end])) end++;
      next = [...lines.slice(0, headerIdx), ...lines.slice(end)];
    }
    const content = next.join('\n');
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/design`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, etag: design.etag }),
      });
      if (res.status === 409) { alert('DESIGN.md changed externally. Reload and retry.'); return; }
    } catch { /* ignore */ }
    await loadProjectDesign(projectId);
    fetchProjects();
  };

  // --- Agents actions ---

  const loadProjectAgents = async (projectId) => {
    setAgentsLoading(true);
    setAgents([]);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/agents`);
      const json = await res.json();
      setAgents(Array.isArray(json) ? json : []);
    } catch {
      setAgents([]);
    }
    setAgentsLoading(false);
  };

  // Expand/collapse an agent row in place to reveal its definition + context.
  // The context (self-appended entries + conflict review) is reached HERE, via
  // the agent entry itself — no jumping to a separate page.
  const toggleAgent = async (project, slug) => {
    if (expandedAgent === slug) { setExpandedAgent(null); return; }
    setExpandedAgent(slug);
    setAgentView('definition');
    setAgentDetail(null);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(project.id)}/agents/${encodeURIComponent(slug)}`);
      setAgentDetail(await res.json());
    } catch {
      setAgentDetail({ content: 'Failed to load agent.' });
    }
  };

  // Promote a project agent to global (universal) or demote a global one into
  // this project. The backend moves the .md file; 409 means a same-named agent
  // already exists at the destination.
  const moveAgentScope = async (projectId, agent, targetScope) => {
    const verb = targetScope === 'global' ? 'make this agent universal (global)' : 'move this agent into this project';
    if (!window.confirm(`${agent.name}: ${verb}?`)) return;
    setAgentBusy(agent.slug);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(agent.slug)}/scope`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: targetScope }),
      });
      if (res.status === 409) {
        alert(`An agent named "${agent.slug}" already exists in ${targetScope} scope.`);
      }
    } catch { /* ignore */ }
    setAgentBusy(null);
    await loadProjectAgents(projectId);
    fetchProjects();
  };

  // --- CLAUDE.md actions ---

  const startEditClaudeMd = (project) => {
    setEditingClaudeMd(true);
    setClaudeMdDraft(project.claudeMd || '');
  };

  // After writing CLAUDE.md, drop the cached detail body for this project so it
  // is re-fetched fresh from /detail (the file's content just changed); seed
  // the just-written content immediately so the view doesn't flash empty. The
  // CLAUDE.md tab effect re-fires on the projects change and reloads /detail.
  const applyClaudeMdWrite = (projectId, content) => {
    setProjects(prev => prev.map(p =>
      p.id === projectId ? { ...p, claudeMd: content, detailLoaded: false, detailLoading: false } : p
    ));
  };

  const createClaudeMd = async (project) => {
    try {
      await fetch(`/api/projects/${encodeURIComponent(project.id)}/claude-md`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: CLAUDE_MD_TEMPLATE }),
      });
      setEditingClaudeMd(false);
      applyClaudeMdWrite(project.id, CLAUDE_MD_TEMPLATE);
      await fetchProjects();
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
      applyClaudeMdWrite(project.id, claudeMdDraft);
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

  // --- Docs actions ---

  const selectSopFile = async (project, filename) => {
    setSelectedSop(filename);
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(project.id)}/sop/${encodeURIComponent(filename)}`);
      const json = await res.json();
      setSopContent(json.content);
    } catch {
      setSopContent('Failed to load doc file.');
    }
  };

  // --- Tasks actions ---

  // Tasks are filtered by project NAME (how /api/tasks attributes them). A
  // project's directory name is its project name, so project.name is the key.
  // Fetches BOTH user TODOs (backlog) and CLI agent tasks in parallel.
  const loadProjectTasks = async (project, { silent = false } = {}) => {
    if (!silent) { setTasksLoading(true); setAgentTasksLoading(true); }
    const name = encodeURIComponent(project.name);
    const [userRes, agentRes] = await Promise.allSettled([
      fetch(`/api/tasks?project=${name}`),
      fetch(`/api/tasks/agent?project=${name}`),
    ]);
    // User TODOs (backlog)
    if (userRes.status === 'fulfilled' && userRes.value.ok) {
      try {
        const json = await userRes.value.json();
        setProjectTasks(Array.isArray(json.tasks) ? json.tasks : []);
      } catch { if (!silent) setProjectTasks([]); }
    } else {
      if (!silent) setProjectTasks([]);
    }
    // CLI agent tasks
    if (agentRes.status === 'fulfilled' && agentRes.value.ok) {
      try {
        const json = await agentRes.value.json();
        setAgentTasks(Array.isArray(json.tasks) ? json.tasks : Array.isArray(json) ? json : []);
      } catch { if (!silent) setAgentTasks([]); }
    } else {
      if (!silent) setAgentTasks([]);
    }
    if (!silent) { setTasksLoading(false); setAgentTasksLoading(false); }
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

  const advanceProjectTask = async (project, task, targetStage) => {
    try {
      const res = await fetch(`/api/tasks/user/${encodeURIComponent(task.id)}/advance`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage: targetStage }),
      });
      if (res.ok) loadProjectTasks(project, { silent: true });
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
            {(project.codeUrls && project.codeUrls.length > 0
              ? project.codeUrls
              : project.codeUrl ? [{ name: 'Code Repo', url: project.codeUrl }] : []
            ).map((repo) => (
              <a
                key={repo.url}
                href={repo.url}
                target="_blank"
                rel="noopener noreferrer"
                title={repo.url}
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
                {/* Label with the repo name when there are several; a single
                    repo keeps the familiar "Code Repo" label. */}
                {(project.codeUrls && project.codeUrls.length > 1) ? repo.name : 'Code Repo'}
              </a>
            ))}
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
            {...clickableBadgeProps(() => navigate(`/sessions?project=${projectPathToSlug(project.path)}`))}
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
            {...clickableBadgeProps(() => { setExpandedId(project.id); setActiveTab('Memory'); })}
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
                setActiveTab('Docs');
              }}
              {...clickableBadgeProps(() => { setExpandedId(project.id); setActiveTab('Docs'); })}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {(project.sopFiles || []).length} {(project.sopFiles || []).length === 1 ? 'doc' : 'docs'}
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
              {...clickableBadgeProps(() => { setExpandedId(project.id); setActiveTab('Tasks'); })}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {project.openTaskCount > 0 ? `${project.openTaskCount} open tasks` : `${project.taskCount} tasks`}
            </span>
          )}
          {project.agentCount > 0 && (
            <span
              className="badge"
              title={`${project.agentCount} role agent(s)`}
              onClick={(e) => {
                e.stopPropagation();
                navigate(`/agents?project=${encodeURIComponent(project.id)}`);
              }}
              {...clickableBadgeProps(() => navigate(`/agents?project=${encodeURIComponent(project.id)}`))}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {project.agentCount} {project.agentCount === 1 ? 'agent' : 'agents'}
            </span>
          )}
          {project.unreviewedAgentEntries > 0 && (
            <span
              className="badge badge-warn"
              title={`${project.unreviewedAgentEntries} new agent context entr(ies) to review`}
              onClick={(e) => {
                e.stopPropagation();
                navigate(`/agents?project=${encodeURIComponent(project.id)}`);
              }}
              {...clickableBadgeProps(() => navigate(`/agents?project=${encodeURIComponent(project.id)}`))}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {project.unreviewedAgentEntries} to review
            </span>
          )}
          {project.hasDesignDoc && (
            <span
              className={`badge ${project.proposedDecisionCount > 0 ? 'badge-warn' : ''}`}
              title={project.proposedDecisionCount > 0
                ? `${project.proposedDecisionCount} proposal(s) awaiting review`
                : 'Proposals'}
              onClick={(e) => {
                e.stopPropagation();
                setExpandedId(project.id);
                setActiveTab('Proposals');
              }}
              {...clickableBadgeProps(() => { setExpandedId(project.id); setActiveTab('Proposals'); })}
              style={{ cursor: 'pointer', transition: 'opacity 0.15s' }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = '0.7'; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = '1'; }}
            >
              {project.proposedDecisionCount > 0 ? `${project.proposedDecisionCount} proposals to review` : 'proposals'}
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
              {...clickableBadgeProps(() => navigate(`/sessions?project=${projectPathToSlug(project.path)}`))}
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
          onClick={() => setActiveTab('Docs')}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{(project.sopFiles || []).length}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>Docs</div>
        </div>
        <div
          onClick={() => setActiveTab('Proposals')}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.hasDesignDoc ? (project.designDecisionCount ?? '✓') : '—'}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>
            Proposals{project.proposedDecisionCount ? ` · ${project.proposedDecisionCount} to review` : ''}
          </div>
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
          onClick={() => setActiveTab('Agents')}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.agentCount ?? 0}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>
            Agents{project.unreviewedAgentEntries ? ` · ${project.unreviewedAgentEntries} to review` : ''}
          </div>
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
          onClick={() => navigate(`/sessions?project=${projectPathToSlug(project.path)}`)}
          style={statCardStyle}
          onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.sessionCount}</div>
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>Sessions</div>
          {project.backgroundSessionCount > 0 && (
            <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', marginTop: '0.15em' }}>
              ({project.backgroundSessionCount.toLocaleString()} background)
            </div>
          )}
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
    // README body rides the on-demand /detail fetch, not the listing. Show a
    // loading state until detail resolves so we don't flash "No README" for a
    // project that actually has one.
    if (!project.detailLoaded) {
      return (
        <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>
          Loading README…
        </div>
      );
    }

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
    // The full CLAUDE.md body rides the on-demand /detail fetch, not the
    // listing. Until detail has loaded we must NOT render the "No CLAUDE.md"
    // empty-state — its Create button would template-overwrite an existing
    // CLAUDE.md (createClaudeMd PUTs CLAUDE_MD_TEMPLATE). Show a loading state
    // instead; offer Create ONLY once detail loaded and confirmed it's absent.
    if (!project.detailLoaded && !editingClaudeMd) {
      return (
        <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>
          Loading CLAUDE.md…
        </div>
      );
    }

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
          <h3>No docs</h3>
          <p style={{ color: 'var(--muted)' }}>
            Add .md files in commands/, agent-sops/, skills/, docs/, or design/ to see them here.
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
                display: 'flex',
                alignItems: 'center',
                gap: '0.4em',
              }}
            >
              {f.number != null && (
                <span style={{ color: 'var(--muted)', fontFamily: 'monospace', fontSize: '0.85em', flexShrink: 0 }}>
                  #{f.number}
                </span>
              )}
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</span>
            </div>
          ))}
        </div>

        {/* Content area */}
        <div style={{ minWidth: 0 }}>
          {!selectedSop ? (
            <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>
              Select a doc file to view.
            </div>
          ) : (
            <div>
              <div style={{ fontWeight: 600, fontSize: 'var(--fs-sm)', marginBottom: 'var(--space-sm)', display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                {(() => { const f = sopFiles.find(s => s.name === selectedSop); return f?.number != null ? <span style={{ color: 'var(--muted)', fontFamily: 'monospace', fontSize: '0.85em' }}>#{f.number}</span> : null; })()}
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

  const renderDesignTab = (project) => {
    if (designLoading) {
      return <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>Loading proposals…</div>;
    }

    const decisions = design?.decisions || [];

    if (decisions.length === 0) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3>No proposals</h3>
          <p style={{ color: 'var(--muted)' }}>
            Architectural proposals for this project will appear here once a
            <code style={{ margin: '0 0.3em' }}>.claude/DESIGN.md</code> exists.
            It&apos;s an append-only ADR log — each entry records a proposal, its rationale,
            and the alternatives rejected.
          </p>
        </div>
      );
    }

    const proposedCount = decisions.filter(d => d.status === 'proposed').length;

    return (
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: 'var(--space-sm)', flexWrap: 'wrap' }}>
          <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)', fontFamily: 'monospace', color: 'var(--muted)' }}>
            .claude/DESIGN.md
          </span>
          <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
            {decisions.length} {decisions.length === 1 ? 'decision' : 'decisions'}
          </span>
          {proposedCount > 0 && (
            <span className="badge badge-warn" title="Proposed decisions awaiting your review">
              {proposedCount} to review
            </span>
          )}
        </div>

        <div style={{ display: 'grid', gap: 'var(--space-sm)' }}>
          {decisions.map((d, i) => (
            <div
              key={`${d.id}-${i}`}
              className="card"
              style={{
                padding: 'var(--space-md)',
                // Highlight proposed entries — they're the ones needing a decision.
                borderColor: d.status === 'proposed' ? 'var(--warning, #d9a400)' : undefined,
                opacity: d.status === 'superseded' ? 0.6 : 1,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.5em', flexWrap: 'wrap', marginBottom: '0.3em' }}>
                <span style={{ fontFamily: 'monospace', fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>{d.id}</span>
                <span style={{ fontWeight: 700, fontSize: 'var(--fs-sm)' }}>{d.title}</span>
                {d.status && <span className={`badge ${adrStatusBadgeClass(d.status)}`}>{d.status}</span>}
                {d.date && <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>{d.date}</span>}
                {d.status === 'proposed' && (
                  <span style={{ display: 'inline-flex', gap: '0.4em', marginLeft: 'auto' }}>
                    <button className="btn" style={{ fontSize: 'var(--fs-xs)', padding: '0.2em 0.6em' }}
                      title="Accept this decision (proposed → accepted)"
                      onClick={() => updateProposedAdr(project.id, d.id, 'accept')}>
                      <FiCheckCircle size={12} /> Accept
                    </button>
                    <button className="btn" style={{ fontSize: 'var(--fs-xs)', padding: '0.2em 0.6em', color: 'var(--danger, #d9534f)' }}
                      title="Reject and remove this proposed decision"
                      onClick={() => { if (window.confirm(`Reject and remove ${d.id}?`)) updateProposedAdr(project.id, d.id, 'reject'); }}>
                      <FiTrash2 size={12} /> Reject
                    </button>
                  </span>
                )}
              </div>
              {d.body && (
                <div className="markdown-body" style={{ fontSize: 'var(--fs-sm)', lineHeight: 1.55 }}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                    {d.body}
                  </ReactMarkdown>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    );
  };

  const renderAgentsTab = (project) => {
    if (agentsLoading) {
      return <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>Loading agents…</div>;
    }

    const projectAgents = agents.filter(a => a.scope === 'project');
    const globalAgents = agents.filter(a => a.scope === 'global');

    const agentRow = (a) => {
      const isOpen = expandedAgent === a.slug;
      return (
      <div key={`${a.scope}-${a.slug}`} style={{ borderBottom: '1px solid var(--border)' }}>
        {/* Clickable row header — toggles the inline definition + context */}
        <div
          onClick={() => toggleAgent(project, a.slug)}
          style={{ padding: '0.7em 1em', display: 'flex', justifyContent: 'space-between', gap: '1em', alignItems: 'flex-start', cursor: 'pointer' }}
        >
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--muted)', flexShrink: 0 }}>
                {isOpen ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
              </span>
              <FiUser size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
              <span style={{ fontWeight: 600, fontSize: '0.9em' }}>{a.name}</span>
              <span className="badge" title={a.scope === 'global' ? 'Universal — available in every project' : 'Project-level'}>
                {a.scope === 'global'
                  ? <><FiGlobe size={9} style={{ marginRight: 3 }} />universal</>
                  : <><FiFolder size={9} style={{ marginRight: 3 }} />project</>}
              </span>
              {a.model && <span className="badge" style={{ fontSize: '0.7em' }}><FiCpu size={9} style={{ marginRight: 3 }} />{a.model}</span>}
              {a.newEntryCount > 0 && <span className="badge badge-warn">{a.newEntryCount} new</span>}
              {a.conflictClusterCount > 0 && (
                <span className="badge" title="Context conflicts to reconcile"><FiAlertTriangle size={9} style={{ marginRight: 3 }} />{a.conflictClusterCount}</span>
              )}
              {a.contextExists && a.newEntryCount === 0 && a.conflictClusterCount === 0 && (
                <span className="badge" title="Append-only context entries">{a.contextEntryCount} {a.contextEntryCount === 1 ? 'entry' : 'entries'}</span>
              )}
            </div>
            {a.description && (
              <div style={{ fontSize: '0.78em', color: 'var(--muted)', marginTop: '0.2em', lineHeight: 1.5, marginLeft: '1.4em' }}>{a.description}</div>
            )}
          </div>
          <div style={{ flexShrink: 0 }}>
            {a.scope === 'project' ? (
              <button
                onClick={(e) => { e.stopPropagation(); moveAgentScope(project.id, a, 'global'); }}
                disabled={agentBusy === a.slug}
                title="Make this agent universal (move to ~/.claude/agents, available in every project)"
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--accent)', display: 'flex', alignItems: 'center', gap: '0.2em', fontSize: '0.7em' }}
              >
                <FiArrowUpCircle size={12} /> make universal
              </button>
            ) : (
              <button
                onClick={(e) => { e.stopPropagation(); moveAgentScope(project.id, a, 'project'); }}
                disabled={agentBusy === a.slug}
                title="Move this universal agent into this project only"
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: '0.2em', fontSize: '0.7em' }}
              >
                <FiArrowDownCircle size={12} /> move to project
              </button>
            )}
          </div>
        </div>

        {/* Inline detail: definition / context toggle + the shared context panel */}
        {isOpen && (
          <div style={{ padding: '0 1em 1em 2.4em', background: 'var(--surface2)' }}>
            <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid var(--border)', marginBottom: 'var(--space-sm)' }}>
              {[['definition', 'Definition'], ['context', 'Context']].map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => setAgentView(key)}
                  style={{
                    padding: '0.35em 0.8em', fontSize: 'var(--fs-xs)',
                    fontWeight: agentView === key ? 600 : 400,
                    color: agentView === key ? 'var(--accent)' : 'var(--muted)',
                    background: 'none', border: 'none',
                    borderBottom: agentView === key ? '2px solid var(--accent)' : '2px solid transparent',
                    cursor: 'pointer',
                  }}
                >
                  {label}
                  {key === 'context' && a.newEntryCount > 0 ? ` (${a.newEntryCount})` : ''}
                </button>
              ))}
            </div>
            {agentView === 'definition' ? (
              !agentDetail ? (
                <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)' }}>Loading…</div>
              ) : (
                <div>
                  {agentDetail.tools && agentDetail.tools.length > 0 && (
                    <div style={{ display: 'flex', gap: '0.3em', flexWrap: 'wrap', marginBottom: '0.5em' }}>
                      {agentDetail.tools.map(t => <span key={t} className="badge" style={{ fontSize: '0.65em' }}>{t}</span>)}
                    </div>
                  )}
                  <div className="markdown-body" style={{ fontSize: 'var(--fs-sm)', lineHeight: 1.55 }}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                      {(agentDetail.content || '').replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, '').trim()}
                    </ReactMarkdown>
                  </div>
                </div>
              )
            ) : (
              <AgentContextPanel
                projectId={project.id}
                slug={a.slug}
                onChanged={() => loadProjectAgents(project.id)}
              />
            )}
          </div>
        )}
      </div>
      );
    };

    if (agents.length === 0) {
      return (
        <div className="empty-state" style={{ padding: 'var(--space-lg)' }}>
          <h3>No agents</h3>
          <p style={{ color: 'var(--muted)' }}>
            Role agents are native Claude Code subagents — add <code>.claude/agents/&lt;role&gt;.md</code>
            files to this project, or define universal ones in <code>~/.claude/agents/</code> to use
            them everywhere.
          </p>
        </div>
      );
    }

    return (
      <div style={{ fontSize: 'var(--fs-sm)' }}>
        <div style={{ marginBottom: 'var(--space-sm)', color: 'var(--muted)', fontSize: 'var(--fs-xs)' }}>
          Project agents live in this repo&apos;s <code>.claude/agents/</code>. Universal agents
          live in <code>~/.claude/agents/</code> and appear in every project. Promote a project
          agent to universal, or pull a universal one into this project.
        </div>
        {projectAgents.length > 0 && (
          <>
            <div style={{ fontWeight: 600, fontSize: 'var(--fs-xs)', textTransform: 'uppercase', color: 'var(--muted)', margin: '0.5em 0 0.3em' }}>
              <FiFolder size={11} style={{ marginRight: 4 }} />Project
            </div>
            <div className="card" style={{ padding: 0, overflow: 'hidden', marginBottom: 'var(--space-md)' }}>
              {projectAgents.map(agentRow)}
            </div>
          </>
        )}
        {globalAgents.length > 0 && (
          <>
            <div style={{ fontWeight: 600, fontSize: 'var(--fs-xs)', textTransform: 'uppercase', color: 'var(--muted)', margin: '0.5em 0 0.3em' }}>
              <FiGlobe size={11} style={{ marginRight: 4 }} />Universal
            </div>
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              {globalAgents.map(agentRow)}
            </div>
          </>
        )}
      </div>
    );
  };

  const renderTasksTab = (project) => {
    if (tasksLoading && agentTasksLoading) {
      return <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: 'var(--space-md)' }}>Loading tasks…</div>;
    }

    const stageBadgeStyle = (stage) => {
      const map = {
        draft: { background: 'var(--surface2)', color: 'var(--muted)' },
        clarifying: { background: '#e8d4f0', color: '#6b21a8' },
        planned: { background: '#dbeafe', color: '#1d4ed8' },
        executing: { background: '#fef3c7', color: '#92400e' },
        done: { background: '#d1fae5', color: '#065f46' },
        archived: { background: 'var(--surface2)', color: 'var(--muted)' },
      };
      return map[stage] || map.draft;
    };

    const priorityBadgeStyle = (priority) => {
      const map = {
        p1: { background: '#fee2e2', color: '#991b1b' },
        p2: { background: '#fef3c7', color: '#92400e' },
        p3: { background: 'var(--surface2)', color: 'var(--muted)' },
      };
      return map[priority] || map.p2;
    };

    // Determine which advance action is available for a given stage.
    const advanceAction = (stage) => {
      if (stage === 'draft') return { label: 'Clarify', target: 'clarifying' };
      if (stage === 'clarifying') return { label: 'Plan', target: 'planned' };
      if (stage === 'planned') return { label: 'Execute', target: 'executing' };
      return null;
    };

    const agentStatusBadge = (s) => {
      if (s === 'completed') return 'badge-ok';
      if (s === 'in_progress') return 'badge-warn';
      return '';
    };

    const taskActionBtnStyle = (extra = {}) => ({
      background: 'none', border: 'none', cursor: 'pointer',
      display: 'flex', alignItems: 'center', gap: '0.2em', fontSize: '0.7em',
      ...extra,
    });

    return (
      <div>
        {/* Backlog section — user TODOs */}
        <h4 style={{ fontSize: 'var(--fs-sm)', fontWeight: 600, margin: '0 0 0.5em' }}>Backlog</h4>
        {projectTasks.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', padding: 'var(--space-sm) 0' }}>
            No TODO items for this project yet. Create one from the TODO page.
          </div>
        ) : (
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            {projectTasks.map((t, i) => {
              const stage = t.stage || 'draft';
              const priority = t.priority || 'p2';
              const action = advanceAction(stage);
              return (
                <div
                  key={t.id}
                  style={{
                    padding: '0.7em 1em',
                    borderBottom: i < projectTasks.length - 1 ? '1px solid var(--border)' : 'none',
                    opacity: stage === 'done' ? 0.7 : 1,
                    display: 'flex', justifyContent: 'space-between', gap: '1em', alignItems: 'flex-start',
                  }}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em', flexWrap: 'wrap' }}>
                      <span style={{
                        display: 'inline-block', fontSize: '0.65em', fontWeight: 600,
                        padding: '1px 6px', borderRadius: '999px',
                        ...stageBadgeStyle(stage),
                      }}>{stage}</span>
                      <span style={{
                        display: 'inline-block', fontSize: '0.65em', fontWeight: 600,
                        padding: '1px 6px', borderRadius: '999px',
                        ...priorityBadgeStyle(priority),
                      }}>{priority.toUpperCase()}</span>
                      <span style={{ fontWeight: 600, fontSize: '0.9em', textDecoration: stage === 'done' ? 'line-through' : 'none' }}>
                        {t.subject || `Task ${t.id}`}
                      </span>
                    </div>
                    {t.description && (
                      <div style={{ fontSize: '0.78em', color: 'var(--muted)', marginTop: '0.2em', lineHeight: 1.5 }}>{t.description}</div>
                    )}
                  </div>
                  <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap' }}>
                    {action && stage !== 'done' && (
                      <button
                        onClick={() => advanceProjectTask(project, t, action.target)}
                        title={`Advance to ${action.target}`}
                        style={taskActionBtnStyle({ color: 'var(--accent)', fontWeight: 600 })}
                      >
                        {action.label} &rarr;
                      </button>
                    )}
                    {stage !== 'done' && (
                      <button
                        onClick={() => advanceProjectTask(project, t, 'done')}
                        title="Mark done"
                        style={taskActionBtnStyle({ color: 'var(--muted)' })}
                      >
                        <FiCheckCircle size={11} /> Done
                      </button>
                    )}
                    <button onClick={() => triggerGoal(t)} title="Trigger a /goal in this project" style={taskActionBtnStyle({ color: 'var(--accent)' })}>
                      <FiZap size={11} /> goal
                    </button>
                    <button onClick={() => deleteProjectTask(project, t)} title="Delete task" style={taskActionBtnStyle({ color: 'var(--danger, #d9534f)' })}>
                      <FiTrash2 size={11} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Visual separator */}
        <div style={{ borderTop: '1px solid var(--border)', margin: '1.2em 0' }} />

        {/* Agent Tasks section — CLI tasks, read-only */}
        <h4 style={{ fontSize: 'var(--fs-sm)', fontWeight: 400, color: 'var(--muted)', margin: '0 0 0.5em' }}>
          Agent Tasks (from sessions)
        </h4>
        {agentTasksLoading && agentTasks.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', padding: 'var(--space-sm) 0' }}>Loading agent tasks…</div>
        ) : agentTasks.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', padding: 'var(--space-sm) 0' }}>No agent tasks</div>
        ) : (
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            {agentTasks.map((t, i) => (
              <div
                key={`${t._sessionId || 'cli'}-${t.id}`}
                style={{
                  padding: '0.7em 1em',
                  borderBottom: i < agentTasks.length - 1 ? '1px solid var(--border)' : 'none',
                  opacity: t.status === 'completed' ? 0.7 : 1,
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
                <div style={{ flexShrink: 0 }}>
                  <span className={`badge ${agentStatusBadge(t.status)}`}>{t.status || 'pending'}</span>
                </div>
              </div>
            ))}
          </div>
        )}
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
        {activeTab === 'Proposals' && renderDesignTab(project)}
        {activeTab === 'Agents' && renderAgentsTab(project)}
        {activeTab === 'README' && renderReadmeTab(project)}
        {activeTab === 'CLAUDE.md' && renderClaudeMdTab(project)}
        {activeTab === 'Memory' && renderMemoryTab(project)}
        {activeTab === 'Docs' && renderSopsTab(project)}
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
