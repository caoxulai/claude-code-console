import { useEffect, useState, useCallback, useRef, useMemo } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { FiMessageSquare, FiTerminal, FiChevronDown, FiChevronRight, FiPlay, FiTrash2, FiRefreshCw, FiCode } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { SkeletonLine } from '../components/Skeleton';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { useConfigStore } from '../stores/configStore';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
import SessionDiff from '../components/SessionDiff';

function StatusBadge({ status }) {
  const cls = status === 'busy' ? 'badge-warn' : 'badge-ok';
  return <span className={`badge ${cls}`}>{status}</span>;
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

  // Tool result — shows the output that came back from a tool invocation.
  if (msg.type === 'result') {
    const content = msg.content || msg.result || '';
    const text = typeof content === 'string'
      ? content
      : Array.isArray(content)
        ? content.filter(c => c.type === 'text').map(c => c.text).join('\n')
        : JSON.stringify(content, null, 2);
    if (!text) return null;
    const truncated = text.length > 800 ? text.slice(0, 800) + '\n…(truncated)' : text;
    return (
      <div style={{ marginLeft: '1.5em', marginBottom: '0.5em' }}>
        <div style={{
          background: 'var(--tool-bg)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          padding: '0.5em 0.75em',
          fontSize: '0.78em',
          fontFamily: 'monospace',
          whiteSpace: 'pre-wrap',
          maxHeight: '200px',
          overflow: 'auto',
          color: 'var(--muted)',
        }}>
          {truncated}
        </div>
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
  const [titleSearch, setTitleSearch] = useState('');
  const [transcriptView, setTranscriptView] = useState('messages');
  const transcriptRef = useRef(null);

  const LIMIT = 50;

  // Environment config (home slug + workspace project slugs) from the server,
  // so the project filter isn't tied to one user's hardcoded paths.
  const config = useConfigStore(s => s.config);
  const ensureConfig = useConfigStore(s => s.ensure);
  useEffect(() => { ensureConfig(); }, [ensureConfig]);
  const HOME_SLUG = config?.homeSlug || '';
  // Map a workspace project name -> its session-dir slug. Prefer the exact slug
  // from /api/config; fall back to constructing it from workspaceDir so a
  // project that still has sessions but no longer exists on disk is filterable.
  const slugForProject = (name) => {
    const known = config?.projects?.find(p => p.name === name)?.slug;
    if (known) return known;
    if (config?.workspaceDir) {
      return '-' + `${config.workspaceDir}/${name}`.replace(/^\//, '').replace(/\//g, '-');
    }
    return '';
  };

  // Filter options derived from config (which lists all workspace projects).
  const filterOptions = (config?.projects || []).map(p => p.name).sort();

  // Current filter value derived from searchParams
  const currentFilter = (() => {
    const p = searchParams.get('project');
    if (!p) return 'all';
    if (HOME_SLUG && p === HOME_SLUG) return 'Global';
    // Prefer an exact match against a known project slug from config.
    const known = config?.projects?.find(proj => proj.slug === p);
    if (known) return known.name;
    // Fall back to extracting the trailing segment of a workspace slug.
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
      // Convert project name to slug using config (portable across users).
      const slug = slugForProject(val);
      if (slug) setSearchParams({ project: slug });
    }
  };

  const fetchHistory = useCallback(async (currentOffset = 0, append = false) => {
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
  }, [searchParams]);

  const fetchLive = useCallback(async () => {
    try {
      const res = await fetch('/api/sessions/live');
      const json = await res.json();
      setLiveSessions(json || []);
    } catch { /* ignore */ }
    setLoading(false);
  }, []);

  // Refetch every time we navigate to this page (location changes) or tab switches
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loading/offset reset before async fetch is intentional
    setLoading(true);
    setOffset(0);
    if (tab === 'history') {
      fetchHistory(0);
    } else {
      fetchLive();
    }
  }, [tab, location.key, searchParams, fetchHistory, fetchLive]);

  const loadMore = () => {
    const next = offset + LIMIT;
    setOffset(next);
    fetchHistory(next, true);
  };

  // Re-read a session's transcript from disk. The CLI and console share the
  // same .jsonl files, so this surfaces anything typed in the CLI. `silent`
  // skips the loading spinner for background refreshes (focus/poll) so the view
  // doesn't flicker.
  const loadTranscript = useCallback(async (id, { silent = false } = {}) => {
    if (!silent) setLoadingTranscript(true);
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(id)}/transcript?limit=200&offset=0&tail=true`);
      const json = await res.json();
      setTranscript(json.messages || []);
    } catch {
      if (!silent) setTranscript([]);
    }
    if (!silent) setLoadingTranscript(false);
  }, []);

  const viewTranscript = (id) => {
    if (selectedId === id) {
      setSelectedId(null);
      setTranscript([]);
      return;
    }
    setSelectedId(id);
    loadTranscript(id);
  };

  // Sync with the CLI on tab focus: re-read the open transcript and the
  // sessions list (both read the same files on disk). Fires only on focus, so
  // it adds no background load.
  useEffect(() => {
    const onFocus = () => {
      if (selectedId) loadTranscript(selectedId, { silent: true });
      if (tab === 'history') fetchHistory(0);
      else fetchLive();
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [selectedId, tab, loadTranscript, fetchHistory, fetchLive]);

  // Push-based liveness: the server now broadcasts `session_changed` when the
  // newest transcript on disk advances (the CLI appends to the live session's
  // .jsonl). The broadcast carries no payload, so we simply re-read the OPEN
  // transcript on the event — silent:true skips the spinner so there's no
  // flicker. This is the primary freshness path; the interval below is a slow
  // backstop in case an event is missed.
  useLiveUpdates(['session_changed'], () => {
    if (selectedId) loadTranscript(selectedId, { silent: true });
  });

  // Backstop poll: while a transcript is open AND the tab is visible, re-read it
  // so CLI activity shows even if a `session_changed` push is missed (WS drop /
  // reconnect). Paused when the tab is hidden to avoid pointless requests. Since
  // the WS push above now freshens the open transcript on CLI activity, this is
  // a 30s safety net, not the primary liveness mechanism.
  useEffect(() => {
    if (!selectedId) return undefined;
    const POLL_MS = 30000;
    const tick = () => {
      if (!document.hidden) loadTranscript(selectedId, { silent: true });
    };
    const id = setInterval(tick, POLL_MS);
    return () => clearInterval(id);
  }, [selectedId, loadTranscript]);

  // After a transcript loads, jump to the bottom so the latest message is in
  // view (we fetch the tail, but the scroll position still starts at the top).
  useEffect(() => {
    if (!loadingTranscript && transcript.length && transcriptRef.current) {
      const el = transcriptRef.current;
      el.scrollTop = el.scrollHeight;
    }
  }, [transcript, loadingTranscript]);

  const tabStyle = (active) => ({
    padding: '0.5em 1.5em',
    cursor: 'pointer',
    fontWeight: active ? 600 : 400,
    color: active ? 'var(--text)' : 'var(--muted)',
    background: 'none',
    border: 'none',
    borderBottom: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
    fontSize: '0.9em',
    transition: 'color 0.12s, border-color 0.12s',
  });

  // Client-side title filter over the loaded page of sessions. (Server-side
  // scope filtering still applies via the dropdown / ?project=.) Declared before
  // any early return so the hook order stays stable across renders.
  const visibleSessions = useMemo(() => {
    const q = titleSearch.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(s =>
      (s.title || '').toLowerCase().includes(q) || s.id.toLowerCase().includes(q));
  }, [sessions, titleSearch]);

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
        <div style={{ marginBottom: '0.75em', display: 'flex', gap: '0.5em', alignItems: 'center', flexWrap: 'wrap' }}>
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
          <input
            className="form-input"
            value={titleSearch}
            onChange={e => setTitleSearch(e.target.value)}
            placeholder="Search titles…"
            style={{ flex: 1, minWidth: '160px', fontSize: 'var(--fs-sm)' }}
          />
          {titleSearch && (
            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
              {visibleSessions.length} match{visibleSessions.length === 1 ? '' : 'es'}
            </span>
          )}
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
                      <th style={{ textAlign: 'left', width: '35%' }}>Title</th>
                      <th style={{ width: '15%' }}>Scope</th>
                      <th style={{ width: '20%' }}>Last Activity</th>
                      <th style={{ width: '10%' }}>Size</th>
                      <th style={{ textAlign: 'right', width: '20%' }}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleSessions.map(s => (
                      <tr
                        key={s.id}
                        onClick={() => viewTranscript(s.id)}
                        style={{
                          cursor: 'pointer',
                          background: selectedId === s.id ? 'var(--user-bg, rgba(59,130,246,0.1))' : undefined,
                        }}
                      >
                        <td style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {s.title || s.id}
                        </td>
                        <td style={{ textAlign: 'center' }}>
                          <span className={`badge ${getScopeName(s) !== 'Global' ? 'badge-ok' : ''}`}>
                            {getScopeName(s)}
                          </span>
                        </td>
                        <td style={{ fontSize: '0.85em', color: 'var(--muted)', whiteSpace: 'nowrap', textAlign: 'center' }}>
                          {s.date}
                        </td>
                        <td style={{ fontSize: '0.85em', color: 'var(--muted)', textAlign: 'center' }}>
                          {s.size}
                        </td>
                        <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5em' }}>
                            <button
                              className="btn"
                              style={{ padding: '0.25em 0.6em', fontSize: '0.78em' }}
                              onClick={(e) => { e.stopPropagation(); navigate(`/chat?resume=${s.id}`); }}
                              title="Continue this session"
                            >
                              <FiPlay size={12} /> Continue
                            </button>
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
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {visibleSessions.length === 0 && titleSearch && (
                  <div style={{ padding: '1em', textAlign: 'center', color: 'var(--muted)', fontSize: 'var(--fs-sm)' }}>
                    No loaded sessions match “{titleSearch}”.
                    {sessions.length < total && ' Try “Load more” to search older sessions.'}
                  </div>
                )}
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
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75em' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75em' }}>
                  <button
                    className="btn"
                    onClick={() => setTranscriptView('messages')}
                    style={{
                      fontSize: '0.8em', padding: '0.2em 0.6em',
                      background: transcriptView === 'messages' ? 'var(--accent)' : undefined,
                      color: transcriptView === 'messages' ? '#fff' : undefined,
                      borderColor: transcriptView === 'messages' ? 'var(--accent)' : undefined,
                    }}
                  >
                    <FiMessageSquare size={11} style={{ marginRight: 3, verticalAlign: '-1px' }} />
                    Messages
                  </button>
                  <button
                    className="btn"
                    onClick={() => setTranscriptView('changes')}
                    style={{
                      fontSize: '0.8em', padding: '0.2em 0.6em',
                      background: transcriptView === 'changes' ? 'var(--accent)' : undefined,
                      color: transcriptView === 'changes' ? '#fff' : undefined,
                      borderColor: transcriptView === 'changes' ? 'var(--accent)' : undefined,
                    }}
                  >
                    <FiCode size={11} style={{ marginRight: 3, verticalAlign: '-1px' }} />
                    Changes
                  </button>
                </div>
                <button
                  className="btn"
                  title="Re-read from disk (picks up CLI activity)"
                  onClick={() => loadTranscript(selectedId)}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3em', fontSize: '0.8em', padding: '0.2em 0.6em' }}
                >
                  <FiRefreshCw size={12} />
                  Refresh
                </button>
              </div>
              {loadingTranscript ? (
                <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>Loading transcript...</div>
              ) : transcript.length === 0 ? (
                <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>No messages in this session.</div>
              ) : transcriptView === 'changes' ? (
                <SessionDiff transcript={transcript} />
              ) : (
                <ErrorBoundary label="Failed to render this transcript.">
                  <div ref={transcriptRef} style={{ maxHeight: '500px', overflowY: 'auto', padding: '0.5em' }}>
                    {transcript.map((msg, i) => (
                      <TranscriptMessage key={i} msg={msg} />
                    ))}
                  </div>
                </ErrorBoundary>
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
                    <th style={{ textAlign: 'left' }}>Name</th>
                    <th style={{ textAlign: 'left' }}>CWD</th>
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
                      <td style={{ textAlign: 'center' }}><StatusBadge status={s.status} /></td>
                      <td style={{ fontSize: '0.85em', textAlign: 'center' }}>{s.kind || '-'}</td>
                      <td style={{ fontSize: '0.85em', color: 'var(--muted)', textAlign: 'center' }}>{s.version || '-'}</td>
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
