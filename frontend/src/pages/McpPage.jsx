import { useEffect, useState, Fragment } from 'react';
import { FiPlus, FiEdit3, FiTrash2, FiChevronDown, FiChevronRight, FiServer } from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

const SENSITIVE_KEYS = /SECRET|TOKEN|KEY|PASSWORD/i;

function redactEnv(env) {
  if (!env || typeof env !== 'object') return {};
  const redacted = {};
  for (const [k, v] of Object.entries(env)) {
    redacted[k] = SENSITIVE_KEYS.test(k) ? '***' : v;
  }
  return redacted;
}

function truncate(str, max = 60) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '…' : str;
}

function TypeBadge({ type }) {
  const cls = type === 'stdio' ? 'badge-ok' : 'badge-warn';
  return <span className={`badge ${cls}`}>{type || 'stdio'}</span>;
}

function ServerForm({ initial, onSubmit, onCancel }) {
  const [name, setName] = useState(initial?.name || '');
  const [command, setCommand] = useState(initial?.command || '');
  const [args, setArgs] = useState(initial?.args?.join(', ') || '');
  const [type, setType] = useState(initial?.type || 'stdio');
  const [envPairs, setEnvPairs] = useState(
    initial?.env ? Object.entries(initial.env).map(([k, v]) => `${k}=${v}`).join('\n') : ''
  );

  const handleSubmit = (e) => {
    e.preventDefault();
    const env = {};
    envPairs.split('\n').filter(Boolean).forEach(line => {
      const [k, ...rest] = line.split('=');
      if (k) env[k.trim()] = rest.join('=');
    });
    onSubmit({
      name,
      config: {
        command,
        args: args ? args.split(',').map(s => s.trim()).filter(Boolean) : [],
        type,
        env: Object.keys(env).length ? env : undefined,
      },
    });
  };

  return (
    <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '0.75em', padding: '1em 0' }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr 1fr', gap: '0.75em' }}>
        <div>
          <label style={{ fontSize: '0.78em', color: 'var(--muted)' }}>Name</label>
          <input
            className="form-input"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="server-name"
            required
            disabled={!!initial?.name}
          />
        </div>
        <div>
          <label style={{ fontSize: '0.78em', color: 'var(--muted)' }}>Command</label>
          <input
            className="form-input"
            value={command}
            onChange={e => setCommand(e.target.value)}
            placeholder="/usr/bin/mcp-server"
            required
          />
        </div>
        <div>
          <label style={{ fontSize: '0.78em', color: 'var(--muted)' }}>Type</label>
          <select className="form-input" value={type} onChange={e => setType(e.target.value)}>
            <option value="stdio">stdio</option>
            <option value="http">http</option>
            <option value="sse">sse</option>
          </select>
        </div>
      </div>
      <div>
        <label style={{ fontSize: '0.78em', color: 'var(--muted)' }}>Args (comma-separated)</label>
        <input
          className="form-input"
          value={args}
          onChange={e => setArgs(e.target.value)}
          placeholder="--port, 3000, --verbose"
        />
      </div>
      <div>
        <label style={{ fontSize: '0.78em', color: 'var(--muted)' }}>Environment Variables (KEY=value, one per line)</label>
        <textarea
          className="form-input"
          value={envPairs}
          onChange={e => setEnvPairs(e.target.value)}
          placeholder={'API_KEY=abc123\nDEBUG=true'}
          rows={3}
          style={{ fontFamily: 'monospace', fontSize: '0.85em' }}
        />
      </div>
      <div style={{ display: 'flex', gap: '0.5em' }}>
        <button type="submit" className="btn btn-primary">{initial ? 'Update' : 'Add'}</button>
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export default function McpPage() {
  const [servers, setServers] = useState([]);
  const [etag, setEtag] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(false);
  const [conflict, setConflict] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try {
      const res = await fetch('/api/mcp');
      const json = await res.json();
      setServers(json.servers || []);
      setEtag(json.etag);
      setConflict(null);
    } catch { /* ignore */ }
    setLoading(false);
  };

  useEffect(() => { refresh(); }, []);
  useLiveUpdates(['mcp_changed', 'mcp_deleted'], refresh);

  const handleAdd = async ({ name, config }) => {
    const res = await fetch('/api/mcp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, config, etag }),
    });
    if (res.status === 409) {
      setConflict('Conflict: server config was modified externally. Please reload.');
      return;
    }
    const json = await res.json();
    setEtag(json.etag);
    setAdding(false);
    refresh();
  };

  const handleEdit = async ({ name, config }) => {
    const res = await fetch(`/api/mcp/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config, etag }),
    });
    if (res.status === 409) {
      setConflict('Conflict: server config was modified externally. Please reload.');
      return;
    }
    const json = await res.json();
    setEtag(json.etag);
    setEditing(null);
    refresh();
  };

  const handleDelete = async (name) => {
    if (!confirm(`Delete server "${name}"?`)) return;
    const res = await fetch(`/api/mcp/${encodeURIComponent(name)}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ etag }),
    });
    if (res.status === 409) {
      setConflict('Conflict: server config was modified externally. Please reload.');
      return;
    }
    const json = await res.json();
    setEtag(json.etag);
    if (expanded === name) setExpanded(null);
    refresh();
  };

  const toggleExpand = (name) => {
    setExpanded(expanded === name ? null : name);
  };

  if (loading) return <div className="loading">Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <h2>MCP Servers</h2>
        <button className="btn btn-primary" onClick={() => { setAdding(true); setEditing(null); }}>
          <FiPlus size={14} /> Add Server
        </button>
      </div>

      {conflict && (
        <div className="conflict-banner" style={{ marginBottom: '1em' }}>
          <span>{conflict}</span>
          <button className="btn" onClick={refresh}>Reload</button>
        </div>
      )}

      {adding && (
        <div className="card" style={{ marginBottom: '1em' }}>
          <ServerForm onSubmit={handleAdd} onCancel={() => setAdding(false)} />
        </div>
      )}

      {servers.length === 0 && !adding ? (
        <div className="card">
          <div className="empty-state">
            <FiServer size={32} style={{ color: 'var(--muted)', marginBottom: '0.5em' }} />
            <h3>No MCP servers configured</h3>
            <p>Add a server to get started.</p>
          </div>
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: '2em' }}></th>
                <th>Name</th>
                <th>Command</th>
                <th>Type</th>
                <th style={{ width: '6em' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {servers.map(s => (
                <Fragment key={s.name}>
                  <tr
                    onClick={() => toggleExpand(s.name)}
                    style={{ cursor: 'pointer' }}
                  >
                    <td>
                      {expanded === s.name ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
                    </td>
                    <td style={{ fontWeight: 500 }}>{s.name}</td>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.85em' }}>
                      {truncate(s.command)}
                    </td>
                    <td><TypeBadge type={s.type} /></td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.4em' }} onClick={e => e.stopPropagation()}>
                        <button
                          className="btn"
                          style={{ padding: '0.25em 0.5em', fontSize: '0.8em' }}
                          onClick={() => { setEditing(s.name); setAdding(false); }}
                          title="Edit"
                        >
                          <FiEdit3 size={13} />
                        </button>
                        <button
                          className="btn"
                          style={{ padding: '0.25em 0.5em', fontSize: '0.8em' }}
                          onClick={() => handleDelete(s.name)}
                          title="Delete"
                        >
                          <FiTrash2 size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                  {expanded === s.name && editing !== s.name && (
                    <tr>
                      <td colSpan={5} style={{ background: 'var(--surface)', padding: '1em 1.5em' }}>
                        <div style={{ display: 'grid', gap: '0.5em', fontSize: '0.85em' }}>
                          <div>
                            <strong>Command:</strong>{' '}
                            <code style={{ fontFamily: 'monospace' }}>{s.command}</code>
                          </div>
                          {s.args && s.args.length > 0 && (
                            <div>
                              <strong>Args:</strong>{' '}
                              <code style={{ fontFamily: 'monospace' }}>{JSON.stringify(s.args)}</code>
                            </div>
                          )}
                          {s.env && Object.keys(s.env).length > 0 && (
                            <div>
                              <strong>Environment:</strong>
                              <pre style={{ margin: '0.3em 0', fontFamily: 'monospace', fontSize: '0.9em', whiteSpace: 'pre-wrap' }}>
                                {Object.entries(redactEnv(s.env)).map(([k, v]) => `${k}=${v}`).join('\n')}
                              </pre>
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                  {editing === s.name && (
                    <tr>
                      <td colSpan={5} style={{ padding: '0.5em 1.5em' }}>
                        <ServerForm
                          initial={{ name: s.name, command: s.command, args: s.args, type: s.type, env: s.env }}
                          onSubmit={handleEdit}
                          onCancel={() => setEditing(null)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
