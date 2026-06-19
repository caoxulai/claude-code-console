import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { FiMessageSquare, FiBookOpen, FiZap, FiServer, FiClock, FiList, FiFolder, FiBarChart2, FiCheckSquare, FiCalendar } from 'react-icons/fi';
import { SkeletonCard } from '../components/Skeleton';
import BudgetAlert from '../components/BudgetAlert';

function projectPathToSlug(path) {
  return '-' + path.replace(/^\//,'').replace(/\//g, '-');
}

// Fixed height for the dashboard stat cards so every card is the same size
// regardless of whether it has a sub-line, and so the loading skeletons match.
const STAT_CARD_HEIGHT = '120px';

function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${n}`;
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const [stats, setStats] = useState(null);
  const [projects, setProjects] = useState([]);
  const [error, setError] = useState(null);

  const load = () => {
    setError(null);
    // Fetch each card's data independently so ONE failing endpoint doesn't blank
    // the whole dashboard. A failed/non-OK fetch yields null and that card shows
    // "—"; only if everything fails do we show the error banner.
    const getJson = (url) =>
      fetch(url).then(r => (r.ok ? r.json() : null)).catch(() => null);

    Promise.allSettled([
      getJson('/api/sessions'),
      getJson('/api/memory/files'),
      getJson('/api/skills'),
      getJson('/api/mcp'),
      getJson('/api/crons'),
      getJson('/api/sessions/live'),
      getJson('/api/projects'),
      getJson('/api/tasks'),
      getJson('/api/oscron'),
    ]).then((results) => {
      const [sessions, memory, skills, mcp, crons, live, projectsData, tasksData, oscron] =
        results.map(r => (r.status === 'fulfilled' ? r.value : null));

      if (results.every(r => r.status !== 'fulfilled' || r.value === null)) {
        setError('Could not load dashboard data. Is the server still running?');
      }

      const projectsList = Array.isArray(projectsData) ? projectsData : [];
      setProjects(projectsList);
      const tasksList = Array.isArray(tasksData?.tasks) ? tasksData.tasks : [];
      // null (failed fetch) → undefined value so the card renders "—" rather
      // than a misleading 0.
      const count = (v) => (v == null ? undefined : v);
      setStats({
        sessions: count(sessions && (sessions.total ?? sessions.sessions?.length ?? 0)),
        memory: count(memory && memory.length),
        skills: count(skills && skills.length),
        mcp: count(mcp && (mcp.servers?.length ?? 0)),
        crons: count(crons && (crons.jobs?.length ?? 0)),
        // Only the user's own crontab entries — NOT the platform-provided systemd
        // timers (OS housekeeping + fleet agents). Matches SystemCronPage's
        // "mineJobs" split (source === 'crontab' = the schedules you authored).
        systemCrons: count(oscron && (Array.isArray(oscron.jobs)
          ? oscron.jobs.filter(j => j.source === 'crontab').length
          : 0)),
        live: count(live && live.length),
        projects: projectsData ? projectsList.length : undefined,
        // Open tasks = anything not completed (in_progress + pending + other).
        tasksOpen: tasksData ? tasksList.filter(t => t.status !== 'completed').length : undefined,
        usageTokens: null,  // loaded separately below to not block the dashboard
      });
    });
  };

  useEffect(() => { load(); }, []);

  const [todayCost, setTodayCost] = useState(0);

  // Load usage separately so the slow full-transcript scan doesn't block the
  // other 7 cards. The token card shows "—" until this resolves.
  useEffect(() => {
    fetch('/api/usage').then(r => r.json()).then(usage => {
      if (usage?.total) {
        setStats(prev => prev ? { ...prev, usageTokens:
          usage.total.inputTokens + usage.total.outputTokens
          + usage.total.cacheReadTokens + usage.total.cacheWriteTokens
        } : prev);
      }
      if (usage?.daily?.length) {
        setTodayCost(usage.daily[usage.daily.length - 1].cost);
      }
    }).catch(() => {});
  }, []);

  const cards = [
    { icon: FiList, label: 'Sessions', value: stats?.sessions, link: '/sessions' },
    { icon: FiMessageSquare, label: 'Live', value: stats?.live, link: '/sessions' },
    { icon: FiCheckSquare, label: 'Open Tasks', value: stats?.tasksOpen, link: '/tasks' },
    { icon: FiBookOpen, label: 'Memory Files', value: stats?.memory, link: '/memory' },
    { icon: FiZap, label: 'Skills', value: stats?.skills, link: '/skills' },
    { icon: FiServer, label: 'MCP Servers', value: stats?.mcp, link: '/mcp' },
    { icon: FiClock, label: 'Cron Jobs', value: stats?.crons, link: '/crons' },
    { icon: FiCalendar, label: 'System Cron', value: stats?.systemCrons, link: '/system-cron' },
    { icon: FiFolder, label: 'Projects', value: stats?.projects, link: '/projects' },
    {
      icon: FiBarChart2,
      label: 'Total Tokens',
      // Total token count from the verified /api/usage totals. No cost line —
      // keeps this card structurally identical to the others.
      value: fmtTokens(stats?.usageTokens),
      link: '/usage',
    },
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
      <div className="page-header">
        <div>
          <h2>Dashboard</h2>
          <p style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: '0.1em' }}>
            Overview of your Claude Code environment
          </p>
        </div>
      </div>
      {error && (
        <div className="conflict-banner" style={{ marginBottom: '1em' }}>
          <span>{error}</span>
          <button className="btn" onClick={load}>Retry</button>
        </div>
      )}
      <BudgetAlert dailyCost={todayCost} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '1em' }}>
        {!stats ? (
          Array.from({ length: cards.length }).map((_, i) => (
            <SkeletonCard key={i} style={{ height: STAT_CARD_HEIGHT }} />
          ))
        ) : (
          cards.map(c => (
            <Link key={c.label} to={c.link} style={{ textDecoration: 'none' }}>
              <div
                className="card card-interactive"
                style={{
                  margin: 0,
                  height: STAT_CARD_HEIGHT,
                  boxSizing: 'border-box',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  textAlign: 'center',
                  cursor: 'pointer',
                }}
              >
                <c.icon size={22} style={{ color: 'var(--accent)', marginBottom: '0.5em' }} />
                <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)', lineHeight: 1.1 }}>
                  {c.value == null ? '—' : c.value}
                </div>
                <div style={{ fontSize: '0.82em', color: 'var(--muted)', marginTop: '0.3em' }}>{c.label}</div>
                {c.sub && (
                  <div style={{ fontSize: '0.72em', color: 'var(--muted)', marginTop: '0.15em' }}>{c.sub}</div>
                )}
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
                      transition: 'background 0.1s',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--surface2)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = ''; }}
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
                              background: u.type === 'deployed' ? 'var(--pill-deployed-bg)' : 'var(--pill-local-bg)',
                              color: u.type === 'deployed' ? 'var(--pill-deployed-text)' : 'var(--pill-local-text)',
                              fontSize: '0.72em',
                              fontWeight: 600,
                              padding: '2px 8px',
                              borderRadius: '999px',
                              border: `1px solid ${u.type === 'deployed' ? 'var(--pill-deployed-border)' : 'var(--pill-local-border)'}`,
                              textDecoration: 'none',
                            }}
                          >
                            <span style={{ width: 6, height: 6, borderRadius: '50%', background: u.type === 'deployed' ? 'var(--pill-deployed-text)' : 'var(--pill-local-text)', display: 'inline-block' }} />
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
