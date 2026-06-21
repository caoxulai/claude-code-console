import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import {
  FiHome, FiMessageSquare, FiSettings, FiBookOpen, FiZap,
  FiGitBranch, FiServer, FiClock, FiList, FiCheckSquare, FiPackage, FiFolder, FiBarChart2, FiUsers, FiSlack, FiMail, FiCalendar
} from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

const NAV_ITEMS = [
  { group: 'Overview', items: [
    { to: '/', icon: FiHome, label: 'Dashboard' },
    { to: '/usage', icon: FiBarChart2, label: 'Usage' },
  ]},
  { group: 'Connect', items: [
    { to: '/slack', icon: FiSlack, label: 'Slack' },
    { to: '/email', icon: FiMail, label: 'Email' },
  ]},
  { group: 'Work', items: [
    { to: '/projects', icon: FiFolder, label: 'Projects' },
    { to: '/chat', icon: FiMessageSquare, label: 'Chat' },
    { to: '/sessions', icon: FiList, label: 'Sessions' },
    { to: '/tasks', icon: FiCheckSquare, label: 'TODO' },
  ]},
  { group: 'AI Core', items: [
    { to: '/agents', icon: FiUsers, label: 'Agents' },
    { to: '/skills', icon: FiZap, label: 'Skills' },
    { to: '/memory', icon: FiBookOpen, label: 'Memory' },
    { to: '/hooks', icon: FiGitBranch, label: 'Hooks' },
  ]},
  { group: 'Extend', items: [
    { to: '/mcp', icon: FiServer, label: 'MCP Servers' },
    { to: '/plugins', icon: FiPackage, label: 'Plugins' },
  ]},
  { group: 'System', items: [
    { to: '/crons', icon: FiClock, label: 'Scheduled Jobs' },
    { to: '/system-cron', icon: FiCalendar, label: 'OS Crons' },
    { to: '/settings', icon: FiSettings, label: 'Settings' },
  ]},
];

export default function NavBar({ onClose }) {
  const handleNavClick = useCallback(() => {
    if (window.innerWidth <= 768) onClose();
  }, [onClose]);

  // Unreviewed-Slack-items count for the sidebar bubble. The NavBar is always
  // mounted (it lives in the persistent Layout shell), so it — not the lazy
  // SlackPage — owns the count. Fetched once on mount and refetched on every
  // slack_changed / slack_deleted broadcast so the bubble stays live even while
  // the user is on another tab. We hit the lightweight GET /api/slack/queue/count
  // (just {count:N}) rather than the full ~275KB queue payload — the backend
  // applies the SAME actionable filter (countActionable semantics) so the number
  // is identical. The broadcast carries no count, so the WS callback simply
  // re-fires this tiny fetch. Degrades to 0 (bubble hidden) on any failure: a
  // throw, a non-2xx, or a missing/non-numeric count. We never show a stale or
  // guessed number.
  const [slackCount, setSlackCount] = useState(0);

  const refreshSlackCount = useCallback(async () => {
    try {
      const res = await fetch('/api/slack/queue/count');
      if (!res.ok) { setSlackCount(0); return; }
      const json = await res.json();
      if (!json || typeof json.count !== 'number') {
        setSlackCount(0);
        return;
      }
      setSlackCount(json.count);
    } catch {
      setSlackCount(0);
    }
  }, []);

  useEffect(() => { refreshSlackCount(); }, [refreshSlackCount]);
  useLiveUpdates(['slack_changed', 'slack_deleted'], refreshSlackCount);

  // Unreviewed-Email-items count for the sidebar bubble. Same pattern as Slack:
  // hit the lightweight GET /api/email/queue/count on mount and on every
  // email_changed broadcast, instead of the full ~178KB queue. Degrades to hidden
  // on failure.
  const [emailCount, setEmailCount] = useState(0);

  const refreshEmailCount = useCallback(async () => {
    try {
      const res = await fetch('/api/email/queue/count');
      if (!res.ok) { setEmailCount(0); return; }
      const json = await res.json();
      if (!json || typeof json.count !== 'number') {
        setEmailCount(0);
        return;
      }
      setEmailCount(json.count);
    } catch {
      setEmailCount(0);
    }
  }, []);

  useEffect(() => { refreshEmailCount(); }, [refreshEmailCount]);
  useLiveUpdates(['email_changed'], refreshEmailCount);

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
                onClick={handleNavClick}
              >
                <item.icon size={16} />
                <span>{item.label}</span>
                {item.to === '/slack' && slackCount > 0 && (
                  <span
                    className="nav-badge"
                    aria-label={`${slackCount} unreviewed Slack items`}
                  >
                    {slackCount > 99 ? '99+' : slackCount}
                  </span>
                )}
                {item.to === '/email' && emailCount > 0 && (
                  <span
                    className="nav-badge"
                    aria-label={`${emailCount} unreviewed Email items`}
                  >
                    {emailCount > 99 ? '99+' : emailCount}
                  </span>
                )}
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
        Claude Code Console v1.0
      </div>
    </nav>
  );
}
