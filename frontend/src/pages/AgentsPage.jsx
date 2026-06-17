import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { FiUser, FiCpu, FiTool, FiBookOpen, FiAlertTriangle, FiGlobe, FiFolder } from 'react-icons/fi';
import AgentContextPanel from '../components/AgentContextPanel';

// Static scope filter options — fixed set, not derived from agent data, so the
// control is stable regardless of which agents a project happens to expose.
const SCOPE_OPTIONS = [
  ['all', 'All'],
  ['universal', 'Universal'],
  ['project', 'Project'],
];

// Strip YAML frontmatter from an agent .md for cleaner body rendering — the
// parsed fields (name/description/model/tools) are shown as chips above.
function stripFrontmatter(content) {
  if (!content) return '';
  return content.replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, '').trim();
}

// Per-project role agents (native .claude/agents/*.md), merged with global
// ("universal") agents. Pick a project, list its agents, view a role's
// definition and — via the same entry — its append-only context + review.
export default function AgentsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(searchParams.get('project') || '');
  const [scope, setScope] = useState(searchParams.get('scope') || 'all'); // 'all' | 'universal' | 'project'
  const [agents, setAgents] = useState([]);
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [selected, setSelected] = useState(null); // role slug (filename stem)
  const [detail, setDetail] = useState(null);
  const [view, setView] = useState('definition'); // 'definition' | 'context'

  // Load the project list once; default to the first project that has agents,
  // else the first project, unless one is already pinned in the URL.
  useEffect(() => {
    fetch('/api/projects')
      .then(r => r.json())
      .then(list => {
        setProjects(list);
        if (!projectId && list.length) {
          const withAgents = list.find(p => p.agentCount > 0);
          setProjectId((withAgents || list[0]).id);
        }
      })
      .catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the URL in sync with both project and scope so either can be deep-linked
  // and neither clobbers the other when one changes.
  useEffect(() => {
    const next = {};
    if (projectId) next.project = projectId;
    if (scope && scope !== 'all') next.scope = scope;
    setSearchParams(next, { replace: true });
  }, [projectId, scope, setSearchParams]);

  // Load agents whenever the selected project changes.
  useEffect(() => {
    if (!projectId) return;
    setLoadingAgents(true);
    setSelected(null);
    setDetail(null);
    fetch(`/api/projects/${encodeURIComponent(projectId)}/agents`)
      .then(r => (r.ok ? r.json() : []))
      .then(list => setAgents(Array.isArray(list) ? list : []))
      .catch(() => setAgents([]))
      .finally(() => setLoadingAgents(false));
  }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  // `slug` is the filename stem (what the endpoints key on); `name` is the
  // frontmatter display name. They're usually equal but we key on slug.
  const selectAgent = async (slug) => {
    setSelected(slug);
    setDetail(null);
    setView('definition');
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(slug)}`);
      setDetail(await res.json());
    } catch {
      setDetail({ content: 'Failed to load agent.' });
    }
  };

  // Client-side scope filter over the loaded agents state:
  //   all       → everything
  //   universal → global-scope agents (available in every project)
  //   project   → project-scoped agents
  const visibleAgents = agents.filter(a => {
    if (scope === 'universal') return a.scope === 'global';
    if (scope === 'project') return a.scope === 'project';
    return true;
  });

  const refreshAgents = () => {
    if (!projectId) return;
    fetch(`/api/projects/${encodeURIComponent(projectId)}/agents`)
      .then(r => (r.ok ? r.json() : []))
      .then(list => setAgents(Array.isArray(list) ? list : []))
      .catch(() => {});
  };

  return (
    <div>
      <div className="page-header">
        <h2>Agents</h2>
        <div
          role="radiogroup"
          aria-label="Scope filter"
          style={{ display: 'inline-flex', border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden' }}
        >
          {SCOPE_OPTIONS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={scope === value}
              aria-label={label}
              onClick={() => setScope(value)}
              style={{
                padding: '0.35em 0.85em', fontSize: 'var(--fs-sm)', cursor: 'pointer',
                fontWeight: scope === value ? 600 : 400,
                color: scope === value ? 'var(--accent)' : 'var(--muted)',
                background: scope === value ? 'var(--user-bg)' : 'transparent',
                border: 'none', borderRight: '1px solid var(--border)',
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <select
          className="form-input"
          value={projectId}
          onChange={e => setProjectId(e.target.value)}
          style={{ maxWidth: 260 }}
          aria-label="Project"
        >
          {projects.map(p => (
            <option key={p.id} value={p.id}>
              {p.name}{p.agentCount > 0 ? ` (${p.agentCount})` : ''}
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(240px, 300px) 1fr', gap: '1em', minHeight: '500px' }}>
        {/* Left — agent list */}
        <div className="card" style={{ overflowY: 'auto', maxHeight: '70vh' }}>
          {loadingAgents ? (
            <div style={{ color: 'var(--muted)', fontSize: '0.85em', padding: '0.5em' }}>Loading…</div>
          ) : agents.length === 0 ? (
            <div className="empty-state">
              <h3>No agents</h3>
              <p style={{ color: 'var(--muted)' }}>
                This project has no role agents yet. Add <code>.claude/agents/&lt;role&gt;.md</code>
                files (native Claude Code subagents) to give it a team.
              </p>
            </div>
          ) : visibleAgents.length === 0 ? (
            <div className="empty-state">
              <h3>No {scope === 'universal' ? 'universal' : 'project'} agents</h3>
              <p style={{ color: 'var(--muted)' }}>
                No agents match the <strong>{scope}</strong> scope. Switch the filter to <strong>All</strong> to see every agent.
              </p>
            </div>
          ) : (
            visibleAgents.map(a => {
              const slug = a.slug || a.name;
              return (
              <div
                key={slug}
                onClick={() => selectAgent(slug)}
                style={{
                  padding: '0.6em 0.75em', cursor: 'pointer', borderRadius: 'var(--radius)',
                  background: selected === slug ? 'var(--user-bg)' : 'transparent',
                  marginBottom: '4px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em', flexWrap: 'wrap' }}>
                  <FiUser size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
                  <span style={{ fontSize: '0.85em', fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {a.name}
                  </span>
                  <span
                    className="badge"
                    title={a.scope === 'global' ? 'Universal — available in every project' : 'Project-level'}
                    style={{ flexShrink: 0 }}
                  >
                    {a.scope === 'global'
                      ? <><FiGlobe size={9} style={{ marginRight: 3 }} />universal</>
                      : <><FiFolder size={9} style={{ marginRight: 3 }} />project</>}
                  </span>
                  {a.newEntryCount > 0 && (
                    <span className="badge badge-warn" title={`${a.newEntryCount} new context entr(ies) since last review`} style={{ flexShrink: 0 }}>
                      {a.newEntryCount} new
                    </span>
                  )}
                  {a.conflictClusterCount > 0 && (
                    <span className="badge" title={`${a.conflictClusterCount} conflict/redundancy cluster(s)`} style={{ flexShrink: 0 }}>
                      <FiAlertTriangle size={9} style={{ marginRight: 3 }} />{a.conflictClusterCount}
                    </span>
                  )}
                  {a.oversized && (
                    <span
                      className="badge badge-warn"
                      title="Context file over the size ceiling (~6KB/400 lines) — reconcile to keep it lean"
                      style={{ flexShrink: 0, color: 'var(--warning)' }}
                    >
                      <FiAlertTriangle size={9} style={{ marginRight: 3 }} />oversized
                    </span>
                  )}
                  {a.contextExists && a.newEntryCount === 0 && a.conflictClusterCount === 0 && (
                    <span className="badge" title="Append-only context entries" style={{ flexShrink: 0 }}>
                      <FiBookOpen size={9} style={{ marginRight: 3 }} />{a.contextEntryCount} {a.contextEntryCount === 1 ? 'entry' : 'entries'}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: '0.72em', color: 'var(--muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: '2px' }}>
                  {a.description || 'No description'}
                </div>
              </div>
              );
            })
          )}
        </div>

        {/* Right — agent detail */}
        <div className="card" style={{ minWidth: 0, overflow: 'hidden' }}>
          {!selected ? (
            <div className="empty-state"><h3>Select an agent</h3><p>Choose a role to view its definition.</p></div>
          ) : !detail ? (
            <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading…</div>
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.6em', flexWrap: 'wrap', marginBottom: '0.5em' }}>
                <span style={{ fontWeight: 700, fontSize: 'var(--fs-lg)' }}>{detail.name}</span>
                {detail.scope && (
                  <span className="badge" title={detail.scope === 'global' ? 'Universal — available in every project' : 'Project-level'}>
                    {detail.scope === 'global'
                      ? <><FiGlobe size={10} style={{ marginRight: 3 }} />universal</>
                      : <><FiFolder size={10} style={{ marginRight: 3 }} />project</>}
                  </span>
                )}
                {detail.model && (
                  <span className="badge" title="Model"><FiCpu size={10} style={{ marginRight: 3 }} />{detail.model}</span>
                )}
                {detail.oversized && (
                  <span
                    className="badge badge-warn"
                    title="Context file over the size ceiling (~6KB/400 lines) — reconcile to keep it lean"
                    style={{ color: 'var(--warning)' }}
                  >
                    <FiAlertTriangle size={10} style={{ marginRight: 3 }} />oversized — needs reconciliation
                  </span>
                )}
              </div>

              {/* Definition / Context view toggle */}
              <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid var(--border)', marginBottom: 'var(--space-md)' }}>
                {[['definition', 'Definition'], ['context', 'Context']].map(([key, label]) => (
                  <button
                    key={key}
                    onClick={() => setView(key)}
                    style={{
                      padding: '0.4em 0.9em', fontSize: 'var(--fs-sm)',
                      fontWeight: view === key ? 600 : 400,
                      color: view === key ? 'var(--accent)' : 'var(--muted)',
                      background: 'none', border: 'none',
                      borderBottom: view === key ? '2px solid var(--accent)' : '2px solid transparent',
                      cursor: 'pointer',
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {view === 'definition' ? (
                <>
                  {detail.description && (
                    <div style={{ fontSize: '0.85em', color: 'var(--muted)', marginBottom: '0.6em', lineHeight: 1.5 }}>
                      {detail.description}
                    </div>
                  )}
                  {detail.tools && detail.tools.length > 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em', flexWrap: 'wrap', marginBottom: '0.8em' }}>
                      <FiTool size={11} style={{ color: 'var(--muted)' }} />
                      {detail.tools.map(t => (
                        <span key={t} className="badge" style={{ fontSize: '0.7em' }}>{t}</span>
                      ))}
                    </div>
                  )}
                  <div className="markdown-body" style={{ fontSize: '0.92em', lineHeight: 1.6 }}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                      {stripFrontmatter(detail.content)}
                    </ReactMarkdown>
                  </div>
                </>
              ) : (
                <AgentContextPanel
                  projectId={projectId}
                  slug={selected}
                  onChanged={refreshAgents}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
