import { useState, useMemo } from 'react';
import { FiFile, FiPlus, FiEdit3, FiChevronDown, FiChevronRight } from 'react-icons/fi';

function extractFileChanges(transcript) {
  const files = new Map();

  for (const msg of transcript) {
    if (msg.type !== 'assistant') continue;
    const content = msg.message?.content;
    if (!Array.isArray(content)) continue;

    for (const block of content) {
      if (block.type !== 'tool_use') continue;
      const { name, input } = block;
      if (!input) continue;

      if (name === 'Write' || name === 'write') {
        const path = input.file_path || input.path || '';
        if (!path) continue;
        if (!files.has(path)) files.set(path, { path, ops: [] });
        files.get(path).ops.push({ type: 'write', content: input.content || '' });
      } else if (name === 'Edit' || name === 'edit') {
        const path = input.file_path || input.path || '';
        if (!path) continue;
        if (!files.has(path)) files.set(path, { path, ops: [] });
        files.get(path).ops.push({
          type: 'edit',
          old: input.old_string || input.old || '',
          new: input.new_string || input.new || '',
        });
      }
    }
  }

  return Array.from(files.values());
}

function FileEntry({ file }) {
  const [expanded, setExpanded] = useState(false);
  const isNew = file.ops.length > 0 && file.ops[0].type === 'write';
  const editCount = file.ops.filter(o => o.type === 'edit').length;
  const writeCount = file.ops.filter(o => o.type === 'write').length;

  return (
    <div style={{ borderBottom: '1px solid var(--border)', padding: '0.5em 0.75em' }}>
      <div
        onClick={() => setExpanded(!expanded)}
        style={{ display: 'flex', alignItems: 'center', gap: '0.5em', cursor: 'pointer' }}
      >
        {expanded ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
        {isNew ? <FiPlus size={12} style={{ color: 'var(--accent2)' }} /> : <FiEdit3 size={12} style={{ color: 'var(--warning)' }} />}
        <span style={{ fontFamily: 'monospace', fontSize: '0.82em', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {file.path}
        </span>
        <span style={{ fontSize: '0.7em', color: 'var(--muted)' }}>
          {writeCount > 0 && `${writeCount} write`}{writeCount > 0 && editCount > 0 && ', '}{editCount > 0 && `${editCount} edit${editCount > 1 ? 's' : ''}`}
        </span>
      </div>
      {expanded && (
        <div style={{ marginTop: '0.5em', marginLeft: '1.5em' }}>
          {file.ops.map((op, i) => (
            <div key={i} style={{ marginBottom: '0.5em' }}>
              {op.type === 'write' && (
                <pre style={{
                  fontSize: '0.75em',
                  background: 'var(--bg)',
                  padding: '0.5em',
                  borderRadius: 'var(--radius)',
                  overflow: 'auto',
                  maxHeight: 200,
                  whiteSpace: 'pre-wrap',
                  color: 'var(--accent2)',
                  borderLeft: '3px solid var(--accent2)',
                }}>
                  {op.content.length > 1000 ? op.content.slice(0, 1000) + '\n…(truncated)' : op.content}
                </pre>
              )}
              {op.type === 'edit' && (
                <div style={{ fontSize: '0.75em', borderRadius: 'var(--radius)', overflow: 'hidden', border: '1px solid var(--border)' }}>
                  {op.old && (
                    <pre style={{
                      background: 'color-mix(in srgb, var(--error) 8%, var(--bg))',
                      padding: '0.4em 0.6em',
                      margin: 0,
                      whiteSpace: 'pre-wrap',
                      maxHeight: 120,
                      overflow: 'auto',
                      color: 'var(--error)',
                    }}>
                      {op.old.split('\n').map(l => '- ' + l).join('\n')}
                    </pre>
                  )}
                  {op.new && (
                    <pre style={{
                      background: 'color-mix(in srgb, var(--accent2) 8%, var(--bg))',
                      padding: '0.4em 0.6em',
                      margin: 0,
                      whiteSpace: 'pre-wrap',
                      maxHeight: 120,
                      overflow: 'auto',
                      color: 'var(--accent2)',
                    }}>
                      {op.new.split('\n').map(l => '+ ' + l).join('\n')}
                    </pre>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function SessionDiff({ transcript }) {
  const files = useMemo(() => extractFileChanges(transcript || []), [transcript]);

  if (files.length === 0) {
    return (
      <div style={{ padding: '1em', textAlign: 'center', color: 'var(--muted)', fontSize: '0.85em' }}>
        No file changes detected in this session.
      </div>
    );
  }

  const newFiles = files.filter(f => f.ops[0]?.type === 'write');
  const editedFiles = files.filter(f => f.ops[0]?.type !== 'write');

  return (
    <div>
      <div style={{ fontSize: '0.8em', color: 'var(--muted)', marginBottom: '0.5em', padding: '0 0.75em' }}>
        <FiFile size={11} style={{ verticalAlign: '-1px', marginRight: 3 }} />
        {files.length} file{files.length === 1 ? '' : 's'} touched
        {newFiles.length > 0 && <span style={{ color: 'var(--accent2)' }}> · {newFiles.length} created</span>}
        {editedFiles.length > 0 && <span style={{ color: 'var(--warning)' }}> · {editedFiles.length} modified</span>}
      </div>
      <div style={{ maxHeight: 400, overflowY: 'auto' }}>
        {files.map(f => <FileEntry key={f.path} file={f} />)}
      </div>
    </div>
  );
}
