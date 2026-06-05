import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FiMessageSquare, FiBookOpen, FiZap, FiServer, FiClock, FiList } from 'react-icons/fi';
import { SkeletonCard } from '../components/Skeleton';

export default function DashboardPage() {
  const [stats, setStats] = useState(null);

  useEffect(() => {
    Promise.all([
      fetch('/api/sessions').then(r => r.json()),
      fetch('/api/memory/files').then(r => r.json()),
      fetch('/api/skills').then(r => r.json()),
      fetch('/api/mcp').then(r => r.json()),
      fetch('/api/crons').then(r => r.json()),
      fetch('/api/sessions/live').then(r => r.json()),
    ]).then(([sessions, memory, skills, mcp, crons, live]) => {
      setStats({
        sessions: sessions.total || sessions.sessions?.length || 0,
        memory: memory.length || 0,
        skills: skills.length || 0,
        mcp: mcp.servers?.length || 0,
        crons: crons.jobs?.length || 0,
        live: live.length || 0,
      });
    }).catch(() => {});
  }, []);

  const cards = [
    { icon: FiList, label: 'Sessions', value: stats?.sessions, link: '/sessions' },
    { icon: FiMessageSquare, label: 'Live', value: stats?.live, link: '/sessions' },
    { icon: FiBookOpen, label: 'Memory Files', value: stats?.memory, link: '/memory' },
    { icon: FiZap, label: 'Skills', value: stats?.skills, link: '/skills' },
    { icon: FiServer, label: 'MCP Servers', value: stats?.mcp, link: '/mcp' },
    { icon: FiClock, label: 'Cron Jobs', value: stats?.crons, link: '/crons' },
  ];

  return (
    <div>
      <div className="page-header"><h2>Dashboard</h2></div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '1em' }}>
        {!stats ? (
          Array.from({ length: 6 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))
        ) : (
          cards.map(c => (
            <Link key={c.label} to={c.link} style={{ textDecoration: 'none' }}>
              <div className="card" style={{ textAlign: 'center', cursor: 'pointer' }}>
                <c.icon size={24} style={{ color: 'var(--accent)', marginBottom: '0.5em' }} />
                <div style={{ fontSize: '1.8em', fontWeight: 700, color: 'var(--text)' }}>
                  {c.value}
                </div>
                <div style={{ fontSize: '0.82em', color: 'var(--muted)' }}>{c.label}</div>
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}
