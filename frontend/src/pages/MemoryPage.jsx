import { useEffect, useState, useMemo } from 'react';
import { FiPlus, FiTrash2, FiEdit3, FiSave } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

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

function MemoryHeader({ meta }) {
  if (!meta) return null;
  const description = meta.description;
  if (!description) return null;
  return (
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
  );
}

function MemoryContentView({ content }) {
  const { meta, body } = useMemo(() => parseFrontmatter(content), [content]);
  return (
    <div>
      <MemoryHeader meta={meta} />
      <div className="markdown-body" style={{ fontSize: '0.92em', lineHeight: 1.6 }}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{body}</ReactMarkdown>
      </div>
    </div>
  );
}

const NEW_FILE_TEMPLATE = `---
name:
description:
metadata:
  type: project
---

`;

export default function MemoryPage() {
  const [files, setFiles] = useState([]);
  const [selected, setSelected] = useState(null);
  const [content, setContent] = useState('');
  const [etag, setEtag] = useState(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');

  const refresh = () => fetch('/api/memory/files').then(r => r.json()).then(setFiles);
  useEffect(() => { refresh(); }, []);

  // Stay in sync when memory files change on disk (CLI, another tab, agents).
  useLiveUpdates(['memory_changed', 'memory_deleted'], refresh);

  // Distinct types present in the current file set, for the filter dropdown.
  const types = useMemo(() => {
    const s = new Set(files.map(f => f.type).filter(t => t && t !== 'unknown'));
    return Array.from(s).sort();
  }, [files]);

  // Client-side filter over the already-fetched list: matches filename or
  // description (case-insensitive) and the selected type.
  const visibleFiles = useMemo(() => {
    const q = search.trim().toLowerCase();
    return files.filter(f => {
      if (typeFilter !== 'all' && f.type !== typeFilter) return false;
      if (!q) return true;
      return f.name.toLowerCase().includes(q)
        || (f.description || '').toLowerCase().includes(q);
    });
  }, [files, search, typeFilter]);

  const selectFile = async (name) => {
    const res = await fetch(`/api/memory/files/${name}`);
    const json = await res.json();
    setSelected(name);
    setContent(json.content);
    setEtag(json.etag);
    setEditing(false);
    setError(null);
  };

  const saveFile = async () => {
    const res = await fetch(`/api/memory/files/${selected}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, etag }),
    });
    if (res.status === 409) {
      setError('File changed externally. Reload and retry.');
      return;
    }
    const json = await res.json();
    setEtag(json.etag);
    setEditing(false);
    setError(null);
    refresh();
  };

  const deleteFile = async (name) => {
    if (!confirm(`Delete ${name}?`)) return;
    await fetch(`/api/memory/files/${name}`, { method: 'DELETE' });
    if (selected === name) { setSelected(null); setContent(''); }
    refresh();
  };

  const startCreate = () => {
    setCreating(true);
    setSelected(null);
    setNewName('');
    setContent(NEW_FILE_TEMPLATE);
    setEditing(true);
    setError(null);
  };

  const createFile = async () => {
    const name = newName.trim();
    if (!name) { setError('Enter a file name.'); return; }
    const res = await fetch('/api/memory/files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, content }),
    });
    if (res.status === 409) {
      setError('A file with that name already exists.');
      return;
    }
    if (!res.ok) {
      setError('Failed to create file.');
      return;
    }
    const json = await res.json();
    setCreating(false);
    setEditing(false);
    setError(null);
    await refresh();
    // Select the freshly created file so the user sees it rendered.
    selectFile(json.name);
  };

  return (
    <div>
      <div className="page-header">
        <h2>Memory</h2>
        <button className="btn btn-primary" onClick={startCreate}><FiPlus size={14} /> New File</button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(250px, 300px) 1fr', gap: '1em', minHeight: '500px' }}>
        <div className="card" style={{ display: 'flex', flexDirection: 'column', maxHeight: '70vh', padding: 0 }}>
          {/* Search + type filter (fixed; list scrolls below) */}
          <div style={{ padding: '0.6em', borderBottom: '1px solid var(--border)', display: 'flex', flexDirection: 'column', gap: '0.4em' }}>
            <input
              className="form-input"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search name or description…"
              style={{ fontSize: '0.82em', padding: '0.35em 0.6em' }}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
              <select
                className="form-input"
                value={typeFilter}
                onChange={e => setTypeFilter(e.target.value)}
                style={{ fontSize: '0.8em', padding: '0.3em 0.4em', flex: 1 }}
              >
                <option value="all">All types</option>
                {types.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
              <span style={{ fontSize: '0.72em', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                {visibleFiles.length}/{files.length}
              </span>
            </div>
          </div>
          <div style={{ overflowY: 'auto', padding: '0.4em' }}>
          {visibleFiles.length === 0 ? (
            <div style={{ padding: '1em 0.6em', fontSize: '0.8em', color: 'var(--muted)', textAlign: 'center' }}>
              {files.length === 0 ? 'No memory files.' : 'No files match.'}
            </div>
          ) : visibleFiles.map(f => (
            <div
              key={f.name}
              onClick={() => selectFile(f.name)}
              style={{
                padding: '0.4em 0.6em', cursor: 'pointer', borderRadius: 'var(--radius)',
                background: selected === f.name ? 'var(--user-bg)' : 'transparent',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                marginBottom: '2px',
              }}
            >
              <div style={{ overflow: 'hidden' }}>
                <div style={{ fontSize: '0.85em', fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</div>
                <div style={{ fontSize: '0.72em', color: 'var(--muted)' }}>{f.type}</div>
              </div>
              {f.name !== 'MEMORY.md' && (
                <button className="icon-btn" onClick={e => { e.stopPropagation(); deleteFile(f.name); }} title="Delete">
                  <FiTrash2 size={12} />
                </button>
              )}
            </div>
          ))}
          </div>
        </div>

        <div className="card">
          {creating ? (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75em', gap: '0.5em' }}>
                <input
                  className="form-input"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  placeholder="file-name (.md added automatically)"
                  autoFocus
                  style={{ flex: 1 }}
                />
                <div style={{ display: 'flex', gap: '0.5em' }}>
                  <button className="btn btn-primary" onClick={createFile}><FiSave size={14} /> Create</button>
                  <button className="btn" onClick={() => { setCreating(false); setContent(''); setError(null); }}>Cancel</button>
                </div>
              </div>
              {error && <div className="conflict-banner"><span>{error}</span></div>}
              <textarea className="form-textarea" value={content} onChange={e => setContent(e.target.value)} style={{ minHeight: '400px', fontFamily: 'monospace', fontSize: '0.85em' }} />
            </>
          ) : !selected ? (
            <div className="empty-state"><h3>Select a memory file</h3></div>
          ) : (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75em' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                  <span style={{ fontWeight: 600 }}>{selected}</span>
                  {(() => {
                    const { meta } = parseFrontmatter(content);
                    const t = meta?.metadata?.type || meta?.type;
                    return t ? <span className={`badge ${typeBadgeClass(t)}`}>{t}</span> : null;
                  })()}
                </div>
                <div style={{ display: 'flex', gap: '0.5em' }}>
                  {editing ? (
                    <button className="btn btn-primary" onClick={saveFile}><FiSave size={14} /> Save</button>
                  ) : (
                    <button className="btn" onClick={() => setEditing(true)}><FiEdit3 size={14} /> Edit</button>
                  )}
                </div>
              </div>
              {error && <div className="conflict-banner"><span>{error}</span></div>}
              {editing ? (
                <textarea className="form-textarea" value={content} onChange={e => setContent(e.target.value)} style={{ minHeight: '400px', fontFamily: 'monospace', fontSize: '0.85em' }} />
              ) : (
                <MemoryContentView content={content} />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
