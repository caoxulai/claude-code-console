import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { FiMessageSquare, FiBookOpen, FiZap, FiServer, FiClock, FiList, FiFolder } from 'react-icons/fi';
import { SkeletonCard } from '../components/Skeleton';

function projectPathToSlug(path) {
  return '-' + path.replace(/^\//,'').replace(/\//g, '-');
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const [stats, setStats] = useState(null);
  const [projects, setProjects] = useState([]);
  const [error, setError] = useState(null);

  const load = () => {
    setError(null);
    Promise.all([
      fetch('/api/sessions').then(r => r.json()),
      fetch('/api/memory/files').then(r => r.json()),
      fetch('/api/skills').then(r => r.json()),
      fetch('/api/mcp').then(r => r.json()),
      fetch('/api/crons').then(r => r.json()),
      fetch('/api/sessions/live').then(r => r.json()),
      fetch('/api/projects').then(r => r.json()),
    ]).then(([sessions, memory, skills, mcp, crons, live, projectsData]) => {
      const projectsList = Array.isArray(projectsData) ? projectsData : [];
      setProjects(projectsList);
      setStats({
        sessions: sessions.total || sessions.sessions?.length || 0,
        memory: memory.length || 0,
        skills: skills.length || 0,
        mcp: mcp.servers?.length || 0,
        crons: crons.jobs?.length || 0,
        live: live.length || 0,
        projects: projectsList.length,
      });
    }).catch(() => {
      // Surface the failure instead of leaving cards blank with no explanation.
      setError('Could not load dashboard data. Is the server still running?');
    });
  };

  useEffect(() => { load(); }, []);

  const cards = [
    { icon: FiList, label: 'Sessions', value: stats?.sessions, link: '/sessions' },
    { icon: FiMessageSquare, label: 'Live', value: stats?.live, link: '/sessions' },
    { icon: FiBookOpen, label: 'Memory Files', value: stats?.memory, link: '/memory' },
    { icon: FiZap, label: 'Skills', value: stats?.skills, link: '/skills' },
    { icon: FiServer, label: 'MCP Servers', value: stats?.mcp, link: '/mcp' },
    { icon: FiClock, label: 'Cron Jobs', value: stats?.crons, link: '/crons' },
    { icon: FiFolder, label: 'Projects', value: stats?.projects, link: '/projects' },
  ];

  const getProjectDescription = (project) => {
    if (!project.claudeMd) return project.path;
    const lines = project.claudeMd.split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('<!--') || trimmed.startsWith('```')) continue;
      if (trimmed.startsWith('-') || trimmed.startsWith('*')) continue;
      return trimmed.length > 80 ? trimmed.slice(0, 80) + '...' : trimmed;
    }
    return project.path;
  };

  const recentProjects = projects.slice(0, 4);

  return (
    <div>
      <div className="page-header"><h2>Dashboard</h2></div>
      {error && (
        <div className="conflict-banner" style={{ marginBottom: '1em' }}>
          <span>{error}</span>
          <button className="btn" onClick={load}>Retry</button>
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '1em' }}>
        {!stats ? (
          Array.from({ length: 7 }).map((_, i) => (
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

      {stats && (
        <div style={{ marginTop: '2em' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75em' }}>
            <h3 style={{ fontSize: '1.1em', fontWeight: 600, margin: 0 }}>Recent Projects</h3>
            <Link to="/projects" style={{ fontSize: '0.85em', color: 'var(--accent)' }}>View all</Link>
          </div>
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            {recentProjects.length === 0 ? (
              <div style={{ padding: '1.5em', textAlign: 'center', color: 'var(--muted)', fontSize: '0.9em' }}>
                No projects found
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column' }}>
                {recentProjects.map((project, i) => (
                  <div
                    key={project.id || project.path}
                    onClick={() => navigate(`/projects?expand=${encodeURIComponent(project.id)}`)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '0.75em 1em',
                      borderBottom: i < recentProjects.length - 1 ? '1px solid var(--border)' : 'none',
                      cursor: 'pointer',
                    }}
                  >
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div style={{ fontWeight: 700, fontSize: '0.95em', color: 'var(--text)' }}>
                        {project.name}
                      </div>
                      <div style={{ fontSize: '0.78em', color: 'var(--muted)', marginTop: '0.2em', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {getProjectDescription(project)}
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', flexShrink: 0 }}>
                      {project.sessionCount != null && (
                        <span
                          className="badge"
                          onClick={(e) => {
                            e.stopPropagation();
                            navigate(`/sessions?project=${encodeURIComponent(projectPathToSlug(project.path))}`);
                          }}
                          style={{ cursor: 'pointer' }}
                        >
                          {project.sessionCount} sessions
                        </span>
                      )}
                      {(project.appUrls || []).length > 0 ? (
                        (project.appUrls || []).map((u, idx) => (
                          <a
                            key={idx}
                            href={u.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '0.3em',
                              background: u.type === 'deployed' ? '#1a2a3a' : '#1a2f2a',
                              color: u.type === 'deployed' ? '#7ab8e6' : '#6dab8a',
                              fontSize: '0.72em',
                              fontWeight: 600,
                              padding: '2px 8px',
                              borderRadius: '999px',
                              border: `1px solid ${u.type === 'deployed' ? '#2a4a5a' : '#2a4a3a'}`,
                              textDecoration: 'none',
                            }}
                          >
                            <span style={{ width: 6, height: 6, borderRadius: '50%', background: u.type === 'deployed' ? '#7ab8e6' : '#6dab8a', display: 'inline-block' }} />
                            {u.label}
                          </a>
                        ))
                      ) : (
                        <span style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '0.3em',
                          fontSize: '0.72em',
                          fontWeight: 600,
                          padding: '2px 8px',
                          borderRadius: '999px',
                          background: 'var(--surface2)',
                          color: 'var(--muted)',
                        }}>
                          <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--muted)', display: 'inline-block', opacity: 0.5 }} />
                          Idle
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
