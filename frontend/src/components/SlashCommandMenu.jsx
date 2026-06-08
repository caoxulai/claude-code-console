import { useState, useEffect, useRef } from 'react';
import { FiZap } from 'react-icons/fi';

export default function SlashCommandMenu({ commands, input, onSelect, visible }) {
  const [selected, setSelected] = useState(0);
  const listRef = useRef(null);

  const query = (() => {
    if (!visible || !input.startsWith('/')) return '';
    const afterSlash = input.slice(1).split(/\s/)[0];
    return afterSlash.toLowerCase();
  })();

  const filtered = commands.filter(cmd => cmd.toLowerCase().includes(query)).slice(0, 12);

  useEffect(() => { setSelected(0); }, [query]);

  useEffect(() => {
    if (!visible) return;
    const el = listRef.current?.children[selected];
    if (el) el.scrollIntoView?.({ block: 'nearest' });
  }, [selected, visible]);

  if (!visible || filtered.length === 0) return null;

  const handleKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelected(prev => Math.min(prev + 1, filtered.length - 1));
      return true;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelected(prev => Math.max(prev - 1, 0));
      return true;
    }
    if (e.key === 'Tab' || e.key === 'Enter') {
      if (filtered[selected]) {
        e.preventDefault();
        onSelect(filtered[selected]);
        return true;
      }
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      onSelect(null);
      return true;
    }
    return false;
  };

  return {
    element: (
      <div style={{
        position: 'absolute',
        bottom: '100%',
        left: 0,
        right: 0,
        marginBottom: 4,
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius)',
        boxShadow: '0 -4px 16px rgba(0,0,0,0.3)',
        maxHeight: 240,
        overflowY: 'auto',
        zIndex: 10,
      }}>
        <div style={{ padding: '0.4em 0.75em', fontSize: 'var(--fs-xs)', color: 'var(--muted)', borderBottom: '1px solid var(--border)' }}>
          Slash commands · Tab to select
        </div>
        <div ref={listRef}>
          {filtered.map((cmd, i) => (
            <div
              key={cmd}
              onClick={() => onSelect(cmd)}
              onMouseEnter={() => setSelected(i)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5em',
                padding: '0.4em 0.75em',
                cursor: 'pointer',
                background: i === selected ? 'var(--surface2)' : 'transparent',
                fontSize: 'var(--fs-sm)',
                color: i === selected ? 'var(--text)' : 'var(--muted)',
              }}
            >
              <FiZap size={12} style={{ color: 'var(--accent)', flexShrink: 0 }} />
              <span style={{ fontFamily: 'monospace' }}>/{cmd}</span>
            </div>
          ))}
        </div>
      </div>
    ),
    handleKeyDown,
  };
}
