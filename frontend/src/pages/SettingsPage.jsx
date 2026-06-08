import { useEffect } from 'react';
import { useSettingsStore } from '../stores/settingsStore';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

export default function SettingsPage() {
  const { data, etag, loading, error, fetch: fetchSettings, save } = useSettingsStore();

  useEffect(() => { fetchSettings(); }, []);
  // Reflect external settings.json edits (CLI / another tab) live.
  useLiveUpdates(['settings_changed'], fetchSettings);

  if (loading && !data) return <div className="loading">Loading settings...</div>;

  const handleChange = (key, value) => {
    save({ ...data, [key]: value });
  };

  const handlePermissionMode = (mode) => {
    save({ ...data, permissions: { ...data?.permissions, defaultMode: mode } });
  };

  return (
    <div>
      <div className="page-header">
        <h2>Settings</h2>
        <span className="badge">{etag ? 'synced' : 'loading'}</span>
      </div>

      {error && (
        <div className="conflict-banner">
          <span>{error}</span>
          <button className="btn" onClick={fetchSettings}>Reload</button>
        </div>
      )}

      <div className="card">
        <div className="card-title">Model</div>
        <div className="form-group">
          <label className="form-label">Default Model</label>
          <select
            className="form-select"
            value={data?.model || 'opus'}
            onChange={e => handleChange('model', e.target.value)}
          >
            <option value="opus">Opus</option>
            <option value="sonnet">Sonnet</option>
            <option value="haiku">Haiku</option>
          </select>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Permissions</div>
        <div className="form-group">
          <label className="form-label">Default Permission Mode</label>
          <select
            className="form-select"
            value={data?.permissions?.defaultMode || 'default'}
            onChange={e => handlePermissionMode(e.target.value)}
          >
            <option value="default">Default (prompt on unknown)</option>
            <option value="acceptEdits">Accept Edits (auto-accept file edits)</option>
            <option value="auto">Auto (classifier-based)</option>
            <option value="bypassPermissions">Bypass All (dangerous)</option>
            <option value="plan">Plan (read-only)</option>
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">Allowed Tools</label>
          <textarea
            className="form-textarea"
            value={(data?.permissions?.allow || []).join('\n')}
            onChange={e => save({
              ...data,
              permissions: { ...data?.permissions, allow: e.target.value.split('\n').filter(Boolean) }
            })}
            placeholder="One tool pattern per line, e.g. Bash(git *)"
            rows={4}
          />
        </div>
      </div>

      <div className="card">
        <div className="card-title">UI Preferences</div>
        <div className="form-group" style={{ display: 'flex', flexDirection: 'column', gap: '0.75em' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.6em', fontSize: '0.88em', cursor: 'pointer' }}>
            <input type="checkbox" checked={data?.verbose || false} onChange={e => handleChange('verbose', e.target.checked)} style={{ width: 16, height: 16, accentColor: 'var(--accent)' }} />
            <span>Verbose output</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.6em', fontSize: '0.88em', cursor: 'pointer' }}>
            <input type="checkbox" checked={data?.includeCoAuthoredBy || false} onChange={e => handleChange('includeCoAuthoredBy', e.target.checked)} style={{ width: 16, height: 16, accentColor: 'var(--accent)' }} />
            <span>Include Co-authored-by in commits</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.6em', fontSize: '0.88em', cursor: 'pointer' }}>
            <input type="checkbox" checked={data?.skipDangerousModePermissionPrompt || false} onChange={e => handleChange('skipDangerousModePermissionPrompt', e.target.checked)} style={{ width: 16, height: 16, accentColor: 'var(--accent)' }} />
            <span>Skip dangerous-mode prompt</span>
          </label>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Environment Variables</div>
        <div className="form-group">
          <textarea
            className="form-textarea"
            value={Object.entries(data?.env || {}).map(([k, v]) => `${k}=${v}`).join('\n')}
            onChange={e => {
              const env = {};
              e.target.value.split('\n').filter(Boolean).forEach(line => {
                const [k, ...rest] = line.split('=');
                if (k) env[k.trim()] = rest.join('=');
              });
              handleChange('env', env);
            }}
            placeholder="KEY=value (one per line)"
            rows={4}
          />
        </div>
      </div>
    </div>
  );
}
