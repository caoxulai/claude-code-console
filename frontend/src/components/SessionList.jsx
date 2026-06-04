import { useState, useEffect } from 'react';
import { listSessions } from '../api/client';
import { FiMessageSquare, FiRefreshCw } from 'react-icons/fi';

export function SessionList({ onResume, currentSessionId }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const data = await listSessions();
      setSessions(data);
    } catch {
      // API not available yet — silent
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  return (
    <div className="session-list">
      <div className="session-list-header">
        <span>Sessions</span>
        <button onClick={refresh} disabled={loading} title="Refresh">
          <FiRefreshCw size={14} className={loading ? 'spin' : ''} />
        </button>
      </div>
      <div className="session-list-items">
        {sessions.map(s => (
          <div
            key={s.id}
            className={`session-item ${s.id === currentSessionId ? 'active' : ''}`}
            onClick={() => onResume(s.id)}
          >
            <FiMessageSquare size={12} />
            <div className="session-info">
              <div className="session-title">{s.title || s.id.slice(0, 8)}</div>
              <div className="session-meta">{s.date} · {s.size}</div>
            </div>
          </div>
        ))}
        {sessions.length === 0 && !loading && (
          <div className="session-empty">No prior sessions</div>
        )}
      </div>
    </div>
  );
}
