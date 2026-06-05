import { NavLink } from 'react-router-dom';
import {
  FiHome, FiMessageSquare, FiSettings, FiBookOpen, FiZap,
  FiGitBranch, FiServer, FiClock, FiList, FiCheckSquare, FiPackage, FiFolder
} from 'react-icons/fi';

const NAV_ITEMS = [
  { group: 'Main', items: [
    { to: '/', icon: FiHome, label: 'Dashboard' },
    { to: '/projects', icon: FiFolder, label: 'Projects' },
    { to: '/chat', icon: FiMessageSquare, label: 'Chat' },
  ]},
  { group: 'Config', items: [
    { to: '/settings', icon: FiSettings, label: 'Settings' },
    { to: '/memory', icon: FiBookOpen, label: 'Memory' },
    { to: '/skills', icon: FiZap, label: 'Skills' },
    { to: '/hooks', icon: FiGitBranch, label: 'Hooks' },
  ]},
  { group: 'System', items: [
    { to: '/mcp', icon: FiServer, label: 'MCP Servers' },
    { to: '/crons', icon: FiClock, label: 'Cron Jobs' },
    { to: '/sessions', icon: FiList, label: 'Sessions' },
    { to: '/tasks', icon: FiCheckSquare, label: 'Tasks' },
    { to: '/plugins', icon: FiPackage, label: 'Plugins' },
  ]},
];

export default function NavBar({ onClose }) {
  return (
    <nav className="navbar">
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
    </nav>
  );
}
