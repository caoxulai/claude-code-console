import { useState, useEffect } from 'react';
import { listCronJobs, deleteCronJob } from '../api/client';
import { FiClock, FiTrash2, FiRefreshCw } from 'react-icons/fi';

export function CronPanel() {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const data = await listCronJobs();
      setJobs(data);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const handleDelete = async (id) => {
    if (!confirm('Delete this scheduled job?')) return;
    await deleteCronJob(id);
    refresh();
  };

  return (
    <div className="cron-panel">
      <div className="cron-header">
        <FiClock size={14} />
        <span>Scheduled Jobs</span>
        <button onClick={refresh} disabled={loading} title="Refresh">
          <FiRefreshCw size={14} className={loading ? 'spin' : ''} />
        </button>
      </div>
      <div className="cron-list">
        {jobs.map(j => (
          <div key={j.id} className="cron-item">
            <div className="cron-info">
              <div className="cron-name">{j.name || j.id.slice(0, 8)}</div>
              <div className="cron-schedule">{j.schedule}</div>
              {j.prompt && <div className="cron-prompt">{j.prompt.slice(0, 100)}</div>}
            </div>
            <button className="cron-delete" onClick={() => handleDelete(j.id)} title="Delete">
              <FiTrash2 size={12} />
            </button>
          </div>
        ))}
        {jobs.length === 0 && !loading && (
          <div className="session-empty">No scheduled jobs</div>
        )}
      </div>
    </div>
  );
}
