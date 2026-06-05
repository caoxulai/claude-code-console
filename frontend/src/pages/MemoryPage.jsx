import { useEffect, useState } from 'react';
import { FiPlus, FiTrash2, FiEdit3, FiSave } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

export default function MemoryPage() {
  const [files, setFiles] = useState([]);
  const [selected, setSelected] = useState(null);
  const [content, setContent] = useState('');
  const [etag, setEtag] = useState(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState(null);

  const refresh = () => fetch('/api/memory/files').then(r => r.json()).then(setFiles);
  useEffect(() => { refresh(); }, []);

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

  return (
    <div>
      <div className="page-header">
        <h2>Memory</h2>
        <button className="btn btn-primary"><FiPlus size={14} /> New File</button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: '1em', minHeight: '500px' }}>
        <div className="card" style={{ overflowY: 'auto', maxHeight: '70vh' }}>
          {files.map(f => (
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
                <div style={{ fontSize: '0.85em', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</div>
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

        <div className="card">
          {!selected ? (
            <div className="empty-state"><h3>Select a memory file</h3></div>
          ) : (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75em' }}>
                <span style={{ fontWeight: 600 }}>{selected}</span>
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
                <div className="markdown-body" style={{ fontSize: '0.9em' }}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{content}</ReactMarkdown>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
