import { NavLink } from 'react-router-dom';
import {
  FiHome, FiMessageSquare, FiSettings, FiBookOpen, FiZap,
  FiGitBranch, FiServer, FiClock, FiList, FiCheckSquare, FiPackage, FiFolder, FiBarChart2, FiUsers
} from 'react-icons/fi';

const NAV_ITEMS = [
  { group: 'Overview', items: [
    { to: '/', icon: FiHome, label: 'Dashboard' },
    { to: '/usage', icon: FiBarChart2, label: 'Usage' },
  ]},
  { group: 'Work', items: [
    { to: '/projects', icon: FiFolder, label: 'Projects' },
    { to: '/chat', icon: FiMessageSquare, label: 'Chat' },
    { to: '/sessions', icon: FiList, label: 'Sessions' },
    { to: '/tasks', icon: FiCheckSquare, label: 'Tasks' },
  ]},
  { group: 'Extend', items: [
    { to: '/agents', icon: FiUsers, label: 'Agents' },
    { to: '/skills', icon: FiZap, label: 'Skills' },
    { to: '/mcp', icon: FiServer, label: 'MCP Servers' },
    { to: '/plugins', icon: FiPackage, label: 'Plugins' },
    { to: '/hooks', icon: FiGitBranch, label: 'Hooks' },
  ]},
  { group: 'Configure', items: [
    { to: '/memory', icon: FiBookOpen, label: 'Memory' },
    { to: '/crons', icon: FiClock, label: 'Cron Jobs' },
    { to: '/settings', icon: FiSettings, label: 'Settings' },
  ]},
];

export default function NavBar({ onClose }) {
  return (
    <nav className="navbar" style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{ flex: 1 }}>
        {NAV_ITEMS.map(group => (
          <div key={group.group} className="nav-group">
            <div className="nav-group-label">{group.group}</div>
            {group.items.map(item => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                onClick={() => window.innerWidth <= 768 && onClose()}
              >
                <item.icon size={16} />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </div>
      <div style={{
        padding: '0.75em var(--space-md)',
        borderTop: '1px solid var(--border)',
        fontSize: 'var(--fs-xs)',
        color: 'var(--muted)',
        opacity: 0.6,
      }}>
        Claude Code Console v0.1
      </div>
    </nav>
  );
}
