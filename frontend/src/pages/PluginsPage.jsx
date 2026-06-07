import { useEffect, useState } from 'react';
import { FiPackage } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export default function PluginsPage() {
  const [plugins, setPlugins] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/plugins')
      .then(r => r.json())
      .then(data => setPlugins(Array.isArray(data) ? data : []))
      .catch(() => setError('Failed to load plugins.'));
  }, []);

  return (
    <div>
      <div className="page-header">
        <h2>Plugins</h2>
        {plugins && (
          <span style={{ fontSize: '0.82em', color: 'var(--muted)' }}>
            {plugins.length} installed · {plugins.filter(p => p.enabled).length} enabled
          </span>
        )}
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {!plugins ? (
        <div className="card">{Array.from({ length: 3 }).map((_, i) => <SkeletonLine key={i} />)}</div>
      ) : plugins.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <FiPackage size={32} style={{ color: 'var(--muted)', marginBottom: '0.5em' }} />
            <h3>No plugins installed</h3>
            <p>Plugins installed via the Claude Code CLI appear here.</p>
          </div>
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Scope</th>
                <th>Version</th>
                <th>Installed</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {plugins.map((p, i) => (
                <tr key={`${p.name}-${i}`}>
                  <td style={{ fontWeight: 500 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                      <FiPackage size={14} style={{ color: 'var(--muted)', flexShrink: 0 }} />
                      <span title={p.installPath}>{p.name}</span>
                    </div>
                  </td>
                  <td><span className="badge">{p.scope || '—'}</span></td>
                  <td style={{ fontFamily: 'monospace', fontSize: '0.82em' }}>{p.version || '—'}</td>
                  <td style={{ fontSize: '0.85em', color: 'var(--muted)' }}>{formatDate(p.installedAt)}</td>
                  <td>
                    <span className={`badge ${p.enabled ? 'badge-ok' : 'badge-warn'}`}>
                      {p.enabled ? 'enabled' : 'disabled'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
