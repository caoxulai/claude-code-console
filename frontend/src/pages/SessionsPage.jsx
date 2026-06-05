import { useEffect, useState } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { FiMessageSquare, FiTerminal, FiChevronDown, FiChevronRight, FiPlay, FiTrash2 } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { SkeletonLine } from '../components/Skeleton';

function StatusBadge({ status }) {
  const cls = status === 'busy' ? 'badge-warn' : 'badge-ok';
  return <span className={`badge ${cls}`}>{status}</span>;
}

function formatSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) +
    ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function extractUserText(msg) {
  const m = msg.message;
  if (!m) return msg.content || msg.text || '';
  if (typeof m.content === 'string') return m.content;
  if (Array.isArray(m.content)) {
    return m.content
      .filter(c => c.type === 'text')
      .map(c => c.text)
      .join('\n');
  }
  return '';
}

function extractAssistantParts(msg) {
  const m = msg.message;
  if (!m || !Array.isArray(m.content)) return [];
  return m.content;
}

function TranscriptMessage({ msg }) {
  const [expanded, setExpanded] = useState(false);

  // Skip metadata entries
  const skip = ['mode', 'permission-mode', 'file-history-snapshot', 'attachment',
    'last-prompt', 'ai-title', 'custom-title', 'agent-name', 'queue-operation', 'system'];
  if (skip.includes(msg.type)) return null;

  // User message
  if (msg.type === 'user') {
    const text = extractUserText(msg);
    if (!text) return null;
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '0.75em' }}>
        <div style={{
          background: 'var(--user-bg)',
          padding: '0.6em 1em',
          borderRadius: '1em 1em 0.2em 1em',
          maxWidth: '75%',
          fontSize: '0.9em',
          whiteSpace: 'pre-wrap',
        }}>
          {text}
        </div>
      </div>
    );
  }

  // Assistant message — may contain text + tool_use blocks
  if (msg.type === 'assistant') {
    const parts = extractAssistantParts(msg);
    if (!parts.length) return null;
    return (
      <div style={{ marginBottom: '0.75em' }}>
        {parts.map((part, i) => {
          if (part.type === 'text' && part.text) {
            return (
              <div key={i} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: '0.3em' }}>
                <div className="markdown-body" style={{
                  background: 'var(--surface)',
                  border: '1px solid var(--border)',
                  padding: '0.6em 1em',
                  borderRadius: '1em 1em 1em 0.2em',
                  maxWidth: '75%',
                  fontSize: '0.9em',
                }}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                    {part.text}
                  </ReactMarkdown>
                </div>
              </div>
            );
          }
          if (part.type === 'tool_use') {
            return (
              <div key={i} style={{ marginLeft: '0.5em', marginBottom: '0.3em' }}>
                <div
                  onClick={() => setExpanded(expanded === i ? false : i)}
                  style={{
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.4em',
                    fontSize: '0.8em',
                    color: 'var(--accent2)',
                    padding: '0.3em 0',
                  }}
                >
                  {expanded === i ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
                  <FiTerminal size={12} />
                  <span style={{ fontWeight: 600 }}>{part.name}</span>
                  <span style={{ color: 'var(--muted)', fontSize: '0.85em' }}>
                    {part.input ? JSON.stringify(part.input).slice(0, 80) : ''}
                  </span>
                </div>
                {expanded === i && (
                  <pre style={{
                    fontFamily: 'monospace',
                    fontSize: '0.78em',
                    background: 'var(--bg)',
                    padding: '0.6em',
                    borderRadius: 'var(--radius)',
                    overflow: 'auto',
                    maxHeight: '250px',
                    whiteSpace: 'pre-wrap',
                    marginLeft: '1.5em',
                  }}>
                    {JSON.stringify(part.input, null, 2)}
                  </pre>
                )}
              </div>
            );
          }
          return null;
        })}
      </div>
    );
  }

  // Anything else we don't know how to render — skip
  return null;
}

function getScopeName(session) {
  const pp = session.projectPath;
  if (!pp) return 'Global';
  if (pp.match(/^\/(?:local\/)?home\/[^/]+$/)) return 'Global';
  const match = pp.match(/\/workspace\/projects\/([^/]+)/);
  if (match) return match[1];
  return 'Global';
}

export default function SessionsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState('history');
  const [sessions, setSessions] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [liveSessions, setLiveSessions] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [transcript, setTranscript] = useState([]);
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const [loading, setLoading] = useState(true);

  const LIMIT = 50;
  const HOME_SLUG = '-local-home-xulaicao';

  // Fixed filter options — loaded ONCE on mount from unfiltered "All" data, never changes
  const [filterOptions, setFilterOptions] = useState([]);

  // Load filter options once on mount (fetch ALL sessions to discover scopes)
  useEffect(() => {
    fetch('/api/sessions?limit=200')
      .then(r => r.json())
      .then(data => {
        const names = new Set();
        (data.sessions || []).forEach(s => {
          const name = getScopeName(s);
          if (name !== 'Global') names.add(name);
        });
        setFilterOptions(Array.from(names).sort());
      })
      .catch(() => {});
  }, []);

  // Current filter value derived from searchParams
  const currentFilter = (() => {
    const p = searchParams.get('project');
    if (!p) return 'all';
    if (p === HOME_SLUG) return 'Global';
    // Convert slug back to project name for display
    const match = p.match(/workspace-projects-(.+)$/);
    return match ? match[1] : p;
  })();

  const handleFilterChange = (e) => {
    const val = e.target.value;
    if (val === 'all') {
      setSearchParams({});
    } else if (val === 'Global') {
      setSearchParams({ project: HOME_SLUG });
    } else {
      // Convert project name to slug
      const slug = `-local-home-xulaicao-workspace-projects-${val}`;
      setSearchParams({ project: slug });
    }
  };

  const fetchHistory = async (currentOffset = 0, append = false) => {
    try {
      const projectParam = searchParams.get('project');
      let url = `/api/sessions?limit=${LIMIT}&offset=${currentOffset}`;
      if (projectParam) url += `&project=${encodeURIComponent(projectParam)}`;
      const res = await fetch(url);
      const json = await res.json();
      if (append) {
        setSessions(prev => [...prev, ...(json.sessions || [])]);
      } else {
        setSessions(json.sessions || []);
      }
      setTotal(json.total || 0);
    } catch { /* ignore */ }
    setLoading(false);
  };

  const fetchLive = async () => {
    try {
      const res = await fetch('/api/sessions/live');
      const json = await res.json();
      setLiveSessions(json || []);
    } catch { /* ignore */ }
    setLoading(false);
  };

  // Refetch every time we navigate to this page (location changes) or tab switches
  useEffect(() => {
    if (tab === 'history') {
      setLoading(true);
      setOffset(0);
      fetchHistory(0);
    } else {
      setLoading(true);
      fetchLive();
    }
  }, [tab, location.key, searchParams]);

  const loadMore = () => {
    const next = offset + LIMIT;
    setOffset(next);
    fetchHistory(next, true);
  };

  const viewTranscript = async (id) => {
    if (selectedId === id) {
      setSelectedId(null);
      setTranscript([]);
      return;
    }
    setSelectedId(id);
    setLoadingTranscript(true);
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(id)}/transcript?limit=200&offset=0`);
      const json = await res.json();
      setTranscript(json.messages || []);
    } catch {
      setTranscript([]);
    }
    setLoadingTranscript(false);
  };

  const tabStyle = (active) => ({
    padding: '0.5em 1.5em',
    cursor: 'pointer',
    borderBottom: active ? '2px solid var(--accent)' : '2px solid transparent',
    fontWeight: active ? 600 : 400,
    color: active ? 'var(--text)' : 'var(--muted)',
    background: 'none',
    border: 'none',
    borderBottomWidth: '2px',
    borderBottomStyle: 'solid',
    borderBottomColor: active ? 'var(--accent)' : 'transparent',
    fontSize: '0.9em',
  });

  if (loading && sessions.length === 0 && liveSessions.length === 0) {
    return (
      <div>
        <div className="page-header"><h2>Sessions</h2></div>
        {Array.from({ length: 5 }).map((_, i) => (
          <SkeletonLine key={i} width="100%" height="40px" />
        ))}
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <h2>Sessions</h2>
      </div>

      <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid var(--border)', marginBottom: '1em' }}>
        <button style={tabStyle(tab === 'history')} onClick={() => setTab('history')}>
          <FiMessageSquare size={14} style={{ marginRight: '0.4em', verticalAlign: '-2px' }} />
          History
        </button>
        <button style={tabStyle(tab === 'live')} onClick={() => setTab('live')}>
          <FiTerminal size={14} style={{ marginRight: '0.4em', verticalAlign: '-2px' }} />
          Live
        </button>
      </div>

      {tab === 'history' && (
        <div style={{ marginBottom: '0.75em' }}>
          <select
            className="form-select"
            style={{ width: '180px', fontSize: 'var(--fs-sm)' }}
            value={currentFilter}
            onChange={handleFilterChange}
          >
            <option value="all">All</option>
            <option value="Global">Global</option>
            {filterOptions.map(name => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </div>
      )}

      {tab === 'history' && (
        <>
          {sessions.length === 0 ? (
            <div className="card">
              <div className="empty-state">
                <h3>No sessions yet</h3>
                <p>Session history will appear here.</p>
              </div>
            </div>
          ) : (
            <>
              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Title</th>
                      <th>Scope</th>
                      <th>Last Activity</th>
                      <th>Size</th>
                      <th></th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {sessions.map(s => (
                      <tr
                        key={s.id}
                        onClick={() => viewTranscript(s.id)}
                        style={{
                          cursor: 'pointer',
                          background: selectedId === s.id ? 'var(--user-bg, rgba(59,130,246,0.1))' : undefined,
                        }}
                      >
                        <td style={{ fontWeight: 500, maxWidth: '400px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {s.title || s.id}
                        </td>
                        <td>
                          <span className={`badge ${getScopeName(s) !== 'Global' ? 'badge-ok' : ''}`}>
                            {getScopeName(s)}
                          </span>
                        </td>
                        <td style={{ fontSize: '0.85em', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                          {s.date}
                        </td>
                        <td style={{ fontSize: '0.85em', color: 'var(--muted)' }}>
                          {s.size}
                        </td>
                        <td>
                          <button
                            className="btn"
                            style={{ padding: '0.25em 0.6em', fontSize: '0.78em' }}
                            onClick={(e) => { e.stopPropagation(); navigate(`/chat?resume=${s.id}`); }}
                            title="Continue this session"
                          >
                            <FiPlay size={12} /> Continue
                          </button>
                        </td>
                        <td>
                          <button
                            className="icon-btn"
                            style={{ color: 'var(--muted)' }}
                            onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--error)'; }}
                            onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--muted)'; }}
                            onClick={async (e) => {
                              e.stopPropagation();
                              if (!confirm(`Delete session "${s.title}"? This cannot be undone.`)) return;
                              await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
                              fetchHistory(0);
                            }}
                            title="Delete session"
                          >
                            <FiTrash2 size={14} />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {sessions.length < total && (
                <div style={{ textAlign: 'center', marginTop: '1em' }}>
                  <button className="btn" onClick={loadMore}>Load more</button>
                </div>
              )}
            </>
          )}

          {selectedId && (
            <div className="card" style={{ marginTop: '1em' }}>
              <div style={{ fontWeight: 600, marginBottom: '0.75em', fontSize: '0.9em', color: 'var(--muted)' }}>
                Transcript
              </div>
              {loadingTranscript ? (
                <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading transcript...</div>
              ) : transcript.length === 0 ? (
                <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>No messages in this session.</div>
              ) : (
                <div style={{ maxHeight: '500px', overflowY: 'auto', padding: '0.5em' }}>
                  {transcript.map((msg, i) => (
                    <TranscriptMessage key={i} msg={msg} />
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {tab === 'live' && (
        <>
          {liveSessions.length === 0 ? (
            <div className="card">
              <div className="empty-state">
                <h3>No live sessions</h3>
                <p>Active Claude sessions will appear here.</p>
              </div>
            </div>
          ) : (
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>CWD</th>
                    <th>Status</th>
                    <th>Kind</th>
                    <th>Version</th>
                  </tr>
                </thead>
                <tbody>
                  {liveSessions.map(s => (
                    <tr key={s.pid || s.sessionId}>
                      <td style={{ fontWeight: 500 }}>{s.name || s.sessionId}</td>
                      <td style={{ fontFamily: 'monospace', fontSize: '0.83em', maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {s.cwd}
                      </td>
                      <td><StatusBadge status={s.status} /></td>
                      <td style={{ fontSize: '0.85em' }}>{s.kind || '-'}</td>
                      <td style={{ fontSize: '0.85em', color: 'var(--muted)' }}>{s.version || '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
