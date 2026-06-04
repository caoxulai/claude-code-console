import { useState } from 'react';
import { FiFolder } from 'react-icons/fi';

const PRESETS = [
  { label: 'Home', path: '/home/xulaicao' },
  { label: 'oncall-kpi', path: '$HOME/workspace/projects/oncall-kpi' },
  { label: 'claude-web', path: '$HOME/workspace/projects/claude-web' },
];

export function CwdSelector({ cwd, onChange }) {
  const [editing, setEditing] = useState(false);
  const [custom, setCustom] = useState(cwd);

  const handleSubmit = (e) => {
    e.preventDefault();
    onChange(custom);
    setEditing(false);
  };

  return (
    <div className="cwd-selector">
      <FiFolder size={14} />
      {!editing ? (
        <span className="cwd-value" onClick={() => setEditing(true)} title="Click to change">
          {cwd || '(default)'}
        </span>
      ) : (
        <form onSubmit={handleSubmit} className="cwd-form">
          <input
            value={custom}
            onChange={e => setCustom(e.target.value)}
            placeholder="/path/to/project"
            autoFocus
            onBlur={() => setEditing(false)}
          />
        </form>
      )}
      <div className="cwd-presets">
        {PRESETS.map(p => (
          <button
            key={p.path}
            className={cwd === p.path ? 'active' : ''}
            onClick={() => { onChange(p.path); setEditing(false); }}
          >
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}
