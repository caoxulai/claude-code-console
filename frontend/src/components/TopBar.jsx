import { FiMenu } from 'react-icons/fi';

export default function TopBar({ onToggleNav }) {
  return (
    <header className="topbar">
      <button className="icon-btn" onClick={onToggleNav} title="Toggle navigation">
        <FiMenu size={18} />
      </button>
      <h1 className="topbar-title">Claude Code Console</h1>
    </header>
  );
}
