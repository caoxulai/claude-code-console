import { useState, useEffect, useRef, forwardRef, useImperativeHandle } from 'react';
import { FiZap } from 'react-icons/fi';

// Rendered as a real component (<SlashCommandMenu ref={...} />) so its hooks
// live in their own fiber — NOT called as a plain function inside the parent's
// render, which made the parent's hook count change when the menu opened/closed
// (a Rules-of-Hooks crash). The parent drives keyboard nav via the ref:
// `ref.current.handleKeyDown(e)` returns true when the menu consumed the key.
const SlashCommandMenu = forwardRef(function SlashCommandMenu(
  { commands, input, onSelect, visible }, ref,
) {
  const [selected, setSelected] = useState(0);
  const listRef = useRef(null);

  const query = (() => {
    if (!visible || !input.startsWith('/')) return '';
    const afterSlash = input.slice(1).split(/\s/)[0];
    return afterSlash.toLowerCase();
  })();

  const filtered = commands.filter(cmd => cmd.toLowerCase().includes(query)).slice(0, 12);
  const active = visible && filtered.length > 0;

  useEffect(() => { setSelected(0); }, [query]);

  useEffect(() => {
    if (!active) return;
    const el = listRef.current?.children[selected];
    if (el) el.scrollIntoView?.({ block: 'nearest' });
  }, [selected, active]);

  // Expose keyboard handling to the parent's textarea onKeyDown. Returns true
  // when the key was consumed (so the parent skips its own Enter-to-send).
  useImperativeHandle(ref, () => ({
    handleKeyDown(e) {
      if (!active) return false;
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
    },
  }), [active, filtered, selected, onSelect]);

  if (!active) return null;

  return (
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
  );
});

export default SlashCommandMenu;
