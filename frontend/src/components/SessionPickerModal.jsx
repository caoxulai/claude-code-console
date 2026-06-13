// Modal shown when "trigger goal" targets a project with more than one session,
// so the user picks which session the goal should resume into. Used by both the
// Tasks page and the Projects page (via useTriggerGoal's onPickSession).
export default function SessionPickerModal({ picker, onClose }) {
  const { task, sessions, resume } = picker;
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
      }}
    >
      <div className="card" onClick={e => e.stopPropagation()} style={{ maxWidth: 480, width: '90%', maxHeight: '70vh', overflowY: 'auto' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75em' }}>
          <h3 style={{ margin: 0, fontSize: '1em' }}>Pick a session for the goal</h3>
          <button onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: '1.2em', lineHeight: 1 }}>×</button>
        </div>
        <p style={{ fontSize: '0.82em', color: 'var(--muted)', marginTop: 0 }}>
          <strong>{task.project}</strong> has multiple sessions. Choose which one to open in Chat with the goal pre-filled.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4em', marginTop: '0.75em' }}>
          {sessions.map(s => (
            <button
              key={s.id}
              className="btn"
              onClick={() => { resume(s.id); onClose(); }}
              style={{ justifyContent: 'space-between', display: 'flex', alignItems: 'center', textAlign: 'left' }}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title || s.id.slice(0, 8)}</span>
              <span style={{ fontSize: '0.72em', color: 'var(--muted)', marginLeft: '0.6em', flexShrink: 0 }}>{s.date}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
