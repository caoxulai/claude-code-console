import { useEffect, useState } from 'react';
import { FiPlus, FiEdit3, FiTrash2, FiSave, FiX } from 'react-icons/fi';

function relativeTime(iso) {
  if (!iso) return '--';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function truncate(str, len) {
  if (!str) return '';
  return str.length > len ? str.slice(0, len) + '...' : str;
}

export default function CronsPage() {
  const [jobs, setJobs] = useState([]);
  const [etag, setEtag] = useState(null);
  const [error, setError] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const [editingId, setEditingId] = useState(null);

  // New job form state
  const [newCron, setNewCron] = useState('');
  const [newPrompt, setNewPrompt] = useState('');
  const [newRecurring, setNewRecurring] = useState(true);

  // Edit form state
  const [editCron, setEditCron] = useState('');
  const [editPrompt, setEditPrompt] = useState('');
  const [editRecurring, setEditRecurring] = useState(true);

  const refresh = async () => {
    try {
      const res = await fetch('/api/crons');
      const json = await res.json();
      setJobs(json.jobs || []);
      setEtag(json.etag);
      setError(null);
    } catch (e) {
      setError('Failed to load cron jobs.');
    }
  };

  useEffect(() => { refresh(); }, []);

  const createJob = async () => {
    if (!newCron.trim() || !newPrompt.trim()) return;
    try {
      const res = await fetch('/api/crons', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cron: newCron, prompt: newPrompt, recurring: newRecurring }),
      });
      if (!res.ok) {
        setError('Failed to create job.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag);
      setShowNew(false);
      setNewCron('');
      setNewPrompt('');
      setNewRecurring(true);
      setError(null);
      refresh();
    } catch (e) {
      setError('Failed to create job.');
    }
  };

  const startEdit = (job) => {
    setEditingId(job.id);
    setEditCron(job.cron);
    setEditPrompt(job.prompt);
    setEditRecurring(job.recurring);
    setError(null);
  };

  const cancelEdit = () => {
    setEditingId(null);
  };

  const saveEdit = async (id) => {
    try {
      const res = await fetch(`/api/crons/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cron: editCron, prompt: editPrompt, recurring: editRecurring, etag }),
      });
      if (res.status === 409) {
        setError('Conflict: job was modified externally. Refreshing...');
        refresh();
        return;
      }
      if (!res.ok) {
        setError('Failed to update job.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag);
      setEditingId(null);
      setError(null);
      refresh();
    } catch (e) {
      setError('Failed to update job.');
    }
  };

  const deleteJob = async (id) => {
    if (!confirm('Delete this cron job?')) return;
    try {
      const res = await fetch(`/api/crons/${id}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etag }),
      });
      if (!res.ok) {
        setError('Failed to delete job.');
        return;
      }
      const json = await res.json();
      setEtag(json.etag);
      setError(null);
      refresh();
    } catch (e) {
      setError('Failed to delete job.');
    }
  };

  return (
    <div>
      <div className="page-header">
        <h2>Cron Jobs</h2>
        <button className="btn btn-primary" onClick={() => { setShowNew(!showNew); setError(null); }}>
          <FiPlus size={14} /> New Job
        </button>
      </div>

      {error && <div className="conflict-banner"><span>{error}</span></div>}

      {showNew && (
        <div className="card" style={{ marginBottom: '1em' }}>
          <div className="card-title">New Cron Job</div>
          <div className="form-group">
            <label className="form-label">Cron Expression</label>
            <input
              className="form-input"
              placeholder="*/5 * * * *"
              value={newCron}
              onChange={e => setNewCron(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label">Prompt</label>
            <textarea
              className="form-textarea"
              placeholder="What should Claude do on this schedule?"
              value={newPrompt}
              onChange={e => setNewPrompt(e.target.value)}
            />
          </div>
          <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
            <input
              type="checkbox"
              id="new-recurring"
              checked={newRecurring}
              onChange={e => setNewRecurring(e.target.checked)}
            />
            <label htmlFor="new-recurring" style={{ fontSize: '0.88em' }}>Recurring</label>
          </div>
          <div style={{ display: 'flex', gap: '0.5em' }}>
            <button className="btn btn-primary" onClick={createJob}><FiSave size={14} /> Save</button>
            <button className="btn" onClick={() => { setShowNew(false); setNewCron(''); setNewPrompt(''); setNewRecurring(true); }}>
              <FiX size={14} /> Cancel
            </button>
          </div>
        </div>
      )}

      <div className="card">
        {jobs.length === 0 ? (
          <div className="empty-state">
            <h3>No cron jobs</h3>
            <p>Create a job to run prompts on a schedule.</p>
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Schedule</th>
                <th>Prompt</th>
                <th>Recurring</th>
                <th>Last Fired</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map(job => (
                editingId === job.id ? (
                  <tr key={job.id}>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.82em' }}>{truncate(job.id, 8)}</td>
                    <td>
                      <input
                        className="form-input"
                        value={editCron}
                        onChange={e => setEditCron(e.target.value)}
                        style={{ width: '140px' }}
                      />
                    </td>
                    <td>
                      <textarea
                        className="form-textarea"
                        value={editPrompt}
                        onChange={e => setEditPrompt(e.target.value)}
                        style={{ minHeight: '60px', width: '100%' }}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        checked={editRecurring}
                        onChange={e => setEditRecurring(e.target.checked)}
                      />
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTime(job.lastFiredAt)}</td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.4em' }}>
                        <button className="btn btn-primary" onClick={() => saveEdit(job.id)} title="Save">
                          <FiSave size={13} />
                        </button>
                        <button className="btn" onClick={cancelEdit} title="Cancel">
                          <FiX size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ) : (
                  <tr key={job.id}>
                    <td style={{ fontFamily: 'monospace', fontSize: '0.82em' }}>{truncate(job.id, 8)}</td>
                    <td style={{ fontFamily: 'monospace' }}>{job.cron}</td>
                    <td title={job.prompt}>{truncate(job.prompt, 80)}</td>
                    <td>
                      <span className={`badge ${job.recurring ? 'badge-ok' : ''}`}>
                        {job.recurring ? 'recurring' : 'once'}
                      </span>
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: '0.85em' }}>{relativeTime(job.lastFiredAt)}</td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.4em' }}>
                        <button className="btn" onClick={() => startEdit(job)} title="Edit">
                          <FiEdit3 size={13} />
                        </button>
                        <button className="btn btn-danger" onClick={() => deleteJob(job.id)} title="Delete">
                          <FiTrash2 size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
