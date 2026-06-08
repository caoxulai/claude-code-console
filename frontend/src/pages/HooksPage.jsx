import { useState, useEffect } from 'react';
import { FiPlus, FiEdit3, FiTrash2, FiPlay, FiChevronDown, FiChevronRight } from 'react-icons/fi';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

const EVENT_TYPES = [
  'Stop',
  'PreToolUse',
  'PostToolUse',
  'Notification',
  'SessionStart',
  'UserPromptSubmit',
];

export default function HooksPage() {
  const [hooks, setHooks] = useState(null);
  const [etag, setEtag] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [conflict, setConflict] = useState(false);
  const [collapsed, setCollapsed] = useState({});
  const [addingTo, setAddingTo] = useState(null);
  const [editingKey, setEditingKey] = useState(null);
  const [formCommand, setFormCommand] = useState('');
  const [formMatcher, setFormMatcher] = useState('');
  const [testingKey, setTestingKey] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [testInput, setTestInput] = useState('{}');

  const fetchHooks = async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    setError(null);
    setConflict(false);
    try {
      const res = await fetch('/api/hooks');
      if (!res.ok) throw new Error(`Failed to load hooks: ${res.status}`);
      const data = await res.json();
      setHooks(data.hooks || {});
      setEtag(data.etag);
    } catch (e) {
      setError(e.message);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => { fetchHooks(); }, []);
  // Live-sync hooks when settings.json changes, unless mid-edit.
  useLiveUpdates(['hooks_changed', 'settings_changed'], () => {
    if (!addingTo && !editingKey) fetchHooks({ silent: true });
  });

  const saveHooks = async (newHooks) => {
    setConflict(false);
    try {
      const res = await fetch('/api/hooks', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hooks: newHooks, etag }),
      });
      if (res.status === 409) {
        setConflict(true);
        return;
      }
      if (!res.ok) throw new Error(`Save failed: ${res.status}`);
      const data = await res.json();
      setHooks(data.hooks);
      setEtag(data.etag);
    } catch (e) {
      setError(e.message);
    }
  };

  const toggleCollapse = (event) => {
    setCollapsed((prev) => ({ ...prev, [event]: !prev[event] }));
  };

  const startAdd = (event) => {
    setAddingTo(event);
    setEditingKey(null);
    setFormCommand('');
    setFormMatcher('');
  };

  const startEdit = (event, index, entry) => {
    const cmd = entry.hooks?.[0]?.command || '';
    setEditingKey(`${event}-${index}`);
    setAddingTo(null);
    setFormCommand(cmd);
    setFormMatcher(entry.matcher || '');
  };

  const cancelForm = () => {
    setAddingTo(null);
    setEditingKey(null);
    setFormCommand('');
    setFormMatcher('');
  };

  const handleSaveAdd = (event) => {
    if (!formCommand.trim()) return;
    const entry = { hooks: [{ type: 'command', command: formCommand.trim() }] };
    if (formMatcher.trim()) entry.matcher = formMatcher.trim();
    const updated = { ...hooks };
    if (!updated[event]) updated[event] = [];
    updated[event] = [...updated[event], entry];
    saveHooks(updated);
    cancelForm();
  };

  const handleSaveEdit = (event, index) => {
    if (!formCommand.trim()) return;
    const entry = { hooks: [{ type: 'command', command: formCommand.trim() }] };
    if (formMatcher.trim()) entry.matcher = formMatcher.trim();
    const updated = { ...hooks };
    updated[event] = [...updated[event]];
    updated[event][index] = entry;
    saveHooks(updated);
    cancelForm();
  };

  const handleDelete = (event, index) => {
    const updated = { ...hooks };
    updated[event] = updated[event].filter((_, i) => i !== index);
    if (updated[event].length === 0) delete updated[event];
    saveHooks(updated);
  };

  const handleTest = async (command) => {
    setTestResult(null);
    try {
      const res = await fetch('/api/hooks/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command, input: testInput }),
      });
      if (!res.ok) throw new Error(`Test failed: ${res.status}`);
      const data = await res.json();
      setTestResult(data);
    } catch (e) {
      setTestResult({ exit_code: -1, stdout: '', stderr: e.message });
    }
  };

  if (loading && !hooks) return <div className="loading">Loading hooks...</div>;

  return (
    <div>
      <div className="page-header">
        <h2>Hooks</h2>
        <span className="badge">{etag ? 'synced' : 'loading'}</span>
      </div>

      {conflict && (
        <div className="conflict-banner">
          <span>Conflict: hooks were modified elsewhere. Reload to get the latest version.</span>
          <button className="btn" onClick={fetchHooks}>Reload</button>
        </div>
      )}

      {error && !conflict && (
        <div className="conflict-banner">
          <span>{error}</span>
          <button className="btn" onClick={fetchHooks}>Reload</button>
        </div>
      )}

      {EVENT_TYPES.map((event) => {
        const entries = hooks?.[event] || [];
        const isCollapsed = collapsed[event];
        const hasMatcher = event === 'PreToolUse' || event === 'PostToolUse';

        return (
          <div className="card" key={event}>
            <div
              className="card-title"
              style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.5em', userSelect: 'none' }}
              onClick={() => toggleCollapse(event)}
            >
              {isCollapsed ? <FiChevronRight /> : <FiChevronDown />}
              <span>{event}</span>
              <span className="badge" style={{ marginLeft: 'auto' }}>{entries.length}</span>
            </div>

            {!isCollapsed && (
              <div style={{ marginTop: '0.75em' }}>
                {entries.length === 0 && (
                  <div style={{ color: 'var(--text-muted, #888)', fontSize: '0.85em', marginBottom: '0.5em' }}>
                    No hooks configured for this event.
                  </div>
                )}

                {entries.map((entry, index) => {
                  const key = `${event}-${index}`;
                  const cmd = entry.hooks?.[0]?.command || '';
                  const isEditing = editingKey === key;
                  const isTesting = testingKey === key;

                  return (
                    <div key={key} style={{ marginBottom: '0.75em', paddingBottom: '0.75em', borderBottom: '1px solid var(--border, #333)' }}>
                      {isEditing ? (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5em' }}>
                          <input
                            className="form-input"
                            value={formCommand}
                            onChange={(e) => setFormCommand(e.target.value)}
                            placeholder="Command"
                            autoFocus
                          />
                          {hasMatcher && (
                            <input
                              className="form-input"
                              value={formMatcher}
                              onChange={(e) => setFormMatcher(e.target.value)}
                              placeholder="Matcher (regex pattern for tool names)"
                            />
                          )}
                          <div style={{ display: 'flex', gap: '0.5em' }}>
                            <button className="btn btn-primary" onClick={() => handleSaveEdit(event, index)}>Save</button>
                            <button className="btn" onClick={cancelForm}>Cancel</button>
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                            <code style={{ flex: 1, fontSize: '0.85em', fontFamily: 'monospace' }}>{cmd}</code>
                            <button className="icon-btn" title="Edit" onClick={() => startEdit(event, index, entry)}>
                              <FiEdit3 />
                            </button>
                            <button className="icon-btn btn-danger" title="Delete" onClick={() => handleDelete(event, index)}>
                              <FiTrash2 />
                            </button>
                            <button className="icon-btn" title="Test" onClick={() => { setTestingKey(isTesting ? null : key); setTestResult(null); setTestInput('{}'); }}>
                              <FiPlay />
                            </button>
                          </div>
                          {entry.matcher && (
                            <div style={{ marginTop: '0.25em', fontSize: '0.8em', color: 'var(--text-muted, #888)' }}>
                              matcher: <code>{entry.matcher}</code>
                            </div>
                          )}
                          {isTesting && (
                            <div style={{ marginTop: '0.5em', display: 'flex', flexDirection: 'column', gap: '0.5em' }}>
                              <textarea
                                className="form-textarea"
                                value={testInput}
                                onChange={(e) => setTestInput(e.target.value)}
                                placeholder='Mock input JSON (e.g. {"tool_name": "Bash"})'
                                rows={3}
                              />
                              <button className="btn btn-primary" onClick={() => {
                                if (!confirm(`This will execute the command on your machine:\n\n${cmd}\n\nProceed?`)) return;
                                handleTest(cmd);
                              }} style={{ alignSelf: 'flex-start' }}>
                                Run Test
                              </button>
                              {testResult && (
                                <pre style={{ background: 'var(--bg)', padding: '0.75em', borderRadius: 'var(--radius)', fontSize: '0.8em', overflow: 'auto', margin: 0 }}>
                                  <div>exit_code: {testResult.exit_code}</div>
                                  {testResult.stdout && <div><strong>stdout:</strong>{'\n'}{testResult.stdout}</div>}
                                  {testResult.stderr && <div><strong>stderr:</strong>{'\n'}{testResult.stderr}</div>}
                                </pre>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}

                {addingTo === event ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5em', marginTop: '0.5em' }}>
                    <input
                      className="form-input"
                      value={formCommand}
                      onChange={(e) => setFormCommand(e.target.value)}
                      placeholder="Command (e.g. /path/to/script.sh)"
                      autoFocus
                    />
                    {hasMatcher && (
                      <input
                        className="form-input"
                        value={formMatcher}
                        onChange={(e) => setFormMatcher(e.target.value)}
                        placeholder="Matcher (regex pattern for tool names, optional)"
                      />
                    )}
                    <div style={{ display: 'flex', gap: '0.5em' }}>
                      <button className="btn btn-primary" onClick={() => handleSaveAdd(event)}>Save</button>
                      <button className="btn" onClick={cancelForm}>Cancel</button>
                    </div>
                  </div>
                ) : (
                  <button className="btn" onClick={() => startAdd(event)} style={{ marginTop: '0.5em', display: 'flex', alignItems: 'center', gap: '0.4em' }}>
                    <FiPlus /> Add Hook
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
