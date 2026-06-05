import { useEffect, useState, useMemo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { FiChevronDown, FiChevronRight, FiEdit3, FiSave, FiPlus } from 'react-icons/fi';
import { SkeletonCard } from '../components/Skeleton';

const TABS = ['Overview', 'CLAUDE.md', 'Memory', 'Skills/SOPs'];

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
  const [searchParams, setSearchParams] = useSearchParams();
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

  const fetchProjects = async () => {
    try {
      const res = await fetch('/api/projects');
      const json = await res.json();
      setProjects(json);
    } catch { /* ignore */ }
    setLoading(false);
  };

  useEffect(() => { fetchProjects(); }, []);

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
            {project.appUrl && (
              <a
                href={project.appUrl}
                target="_blank"
                rel="noopener noreferrer"
                title={project.appUrl}
                onClick={(e) => e.stopPropagation()}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '0.3em',
                  background: '#1a2f2a',
                  color: '#6dab8a',
                  fontSize: '0.72em',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: '999px',
                  border: '1px solid #2a4a3a',
                  textDecoration: 'none',
                }}
              >
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#6dab8a', display: 'inline-block' }} />
                Running
              </a>
            )}
          </div>
          <div style={{
            fontSize: 'var(--fs-xs)',
            color: 'var(--muted)',
            fontFamily: 'monospace',
            marginTop: '0.2em',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}>
            {project.path}
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
          {project.hasSettings && (
            <span className="badge badge-ok">has settings</span>
          )}
          <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
            {project.lastActivity || 'No activity'}
          </span>
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
        <div style={{ ...statCardStyle, cursor: 'default' }}>
          <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{project.lastActivity || '—'}</div>
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
        {activeTab === 'CLAUDE.md' && renderClaudeMdTab(project)}
        {activeTab === 'Memory' && renderMemoryTab(project)}
        {activeTab === 'Skills/SOPs' && renderSopsTab(project)}
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
    </div>
  );
}
