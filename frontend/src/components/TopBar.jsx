import { FiMenu, FiSearch } from 'react-icons/fi';

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent);
const shortcutLabel = isMac ? '⌘K' : 'Ctrl+K';

export default function TopBar({ onToggleNav }) {
  return (
    <header className="topbar">
      <button className="icon-btn" onClick={onToggleNav} title="Toggle navigation">
        <FiMenu size={18} />
      </button>
      <h1 className="topbar-title">Claude Code Console</h1>
      <button
        className="btn"
        onClick={() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: isMac, ctrlKey: !isMac }))}
        title={`Command palette (${shortcutLabel})`}
        style={{
          marginLeft: 'auto',
          display: 'flex',
          alignItems: 'center',
          gap: '0.4em',
          fontSize: '0.78em',
          padding: '0.3em 0.7em',
          color: 'var(--muted)',
          transition: 'color 0.15s, border-color 0.15s',
        }}
      >
        <FiSearch size={12} />
        <span>Search</span>
        <kbd style={{
          fontSize: '0.8em',
          background: 'var(--surface2)',
          border: '1px solid var(--border)',
          borderRadius: 3,
          padding: '1px 4px',
        }}>{shortcutLabel}</kbd>
      </button>
    </header>
  );
}
