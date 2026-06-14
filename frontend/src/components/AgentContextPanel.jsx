import { useEffect, useState, useCallback } from 'react';
import { FiAlertTriangle, FiCheck } from 'react-icons/fi';

// Self-contained append-only context + conflict-review panel for a single agent.
// Owns its own fetching (context + conflicts) and the reconcile/mark-reviewed
// actions, so it can be mounted INLINE under an agent entry anywhere — the
// standalone Agents page and the in-project Agents tab share this one
// implementation. `onChanged` lets the host refresh its agent list badges after
// a reconcile/review.
//
// Append-only is preserved here: the only entry-removing action is reconcile
// (keep/merge/dismiss), and it is always the user's explicit click.
export default function AgentContextPanel({ projectId, slug, onChanged }) {
  const [context, setContext] = useState(null);
  const [conflicts, setConflicts] = useState([]);
  const [busy, setBusy] = useState(false);
  const [mergeFor, setMergeFor] = useState(null);
  const [mergeText, setMergeText] = useState('');

  const base = useCallback(
    () => `/api/projects/${encodeURIComponent(projectId)}/agents/${encodeURIComponent(slug)}/context`,
    [projectId, slug],
  );

  const load = useCallback(async () => {
    try {
      const [ctxRes, confRes] = await Promise.all([fetch(base()), fetch(`${base()}/conflicts`)]);
      setContext(await ctxRes.json());
      setConflicts((await confRes.json()).clusters || []);
    } catch {
      setContext({ exists: false, entries: [] });
      setConflicts([]);
    }
  }, [base]);

  useEffect(() => { load(); }, [load]);

  const reconcile = async (cluster, action, merged) => {
    setBusy(true);
    try {
      const res = await fetch(`${base()}/reconcile`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, entryIndices: cluster.entryIndices, mergedText: merged, etag: context?.etag }),
      });
      if (res.status === 409) alert('Context changed externally. Reloading.');
    } catch { /* ignore */ }
    setBusy(false);
    await load();
    onChanged?.();
  };

  const markReviewed = async () => {
    setBusy(true);
    try { await fetch(`${base()}/mark-reviewed`, { method: 'POST' }); } catch { /* ignore */ }
    setBusy(false);
    await load();
    onChanged?.();
  };

  if (!context) return <div style={{ color: 'var(--muted)', fontSize: '0.85em', padding: '0.5em' }}>Loading context…</div>;

  const entries = context.entries || [];
  if (!context.exists || entries.length === 0) {
    return (
      <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: '0.5em 0', lineHeight: 1.5 }}>
        No context yet. This agent accumulates append-only dated entries as it works;
        conflicts and redundancies surface here for you to reconcile.
      </div>
    );
  }

  const reviewedCount = context.lastReviewedCount || 0;
  const newCount = context.newEntryCount || 0;
  const isNew = (i) => i >= reviewedCount;

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: 'var(--space-sm)', flexWrap: 'wrap' }}>
        <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
          {entries.length} {entries.length === 1 ? 'entry' : 'entries'}
        </span>
        {newCount > 0 && <span className="badge badge-warn">{newCount} new</span>}
        {conflicts.length > 0 && (
          <span className="badge"><FiAlertTriangle size={9} style={{ marginRight: 3 }} />{conflicts.length} to reconcile</span>
        )}
        {newCount > 0 && (
          <button className="btn" onClick={markReviewed} disabled={busy} style={{ fontSize: 'var(--fs-xs)', marginLeft: 'auto' }}>
            <FiCheck size={12} /> Mark reviewed
          </button>
        )}
      </div>

      {conflicts.map((c, ci) => (
        <div key={ci} className="card" style={{ padding: 'var(--space-sm)', marginBottom: 'var(--space-sm)', borderColor: 'var(--warning, #d9a400)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em', marginBottom: '0.4em' }}>
            <FiAlertTriangle size={12} style={{ color: 'var(--warning, #d9a400)' }} />
            <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)' }}>
              {c.reason === 'superseded' ? 'Superseded / corrected' : 'Possibly redundant'}
            </span>
            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>{c.entryIndices.length} entries</span>
          </div>
          <ul style={{ margin: '0 0 0.5em', paddingLeft: '1.1em', fontSize: 'var(--fs-sm)', lineHeight: 1.5 }}>
            {c.entries.map(e => (
              <li key={e.index}>
                {e.date && <span style={{ color: 'var(--muted)', fontFamily: 'monospace', fontSize: '0.85em', marginRight: '0.4em' }}>{e.date}</span>}
                {e.text}
              </li>
            ))}
          </ul>
          {mergeFor === ci ? (
            <div>
              <textarea
                className="form-textarea"
                value={mergeText}
                onChange={e => setMergeText(e.target.value)}
                placeholder="Merged entry text"
                style={{ width: '100%', minHeight: 60, fontSize: 'var(--fs-sm)' }}
              />
              <div style={{ display: 'flex', gap: '0.4em', marginTop: '0.4em' }}>
                <button className="btn btn-primary" disabled={busy || !mergeText.trim()}
                  onClick={() => { reconcile(c, 'merge', mergeText.trim()); setMergeFor(null); setMergeText(''); }}>
                  Save merged
                </button>
                <button className="btn" onClick={() => { setMergeFor(null); setMergeText(''); }}>Cancel</button>
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: '0.4em', flexWrap: 'wrap' }}>
              <button className="btn" disabled={busy} title="Keep the oldest entry in this cluster, drop the others"
                onClick={() => reconcile(c, 'keep')}>Keep oldest</button>
              <button className="btn" disabled={busy} title="Replace the cluster with one entry you edit (pre-filled with the newest)"
                onClick={() => { setMergeFor(ci); setMergeText(c.entries[c.entries.length - 1].text); }}>Merge…</button>
              <button className="btn" disabled={busy} title="Not actually a conflict — keep all and stop flagging this cluster"
                onClick={() => reconcile(c, 'dismiss')}>Keep all</button>
            </div>
          )}
        </div>
      ))}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {[...entries].reverse().map((e, i, arr) => (
          <div key={e.index} style={{
            padding: '0.5em 0.8em',
            borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 'none',
            background: isNew(e.index) ? 'var(--user-bg)' : 'transparent',
            fontSize: 'var(--fs-sm)', lineHeight: 1.5,
          }}>
            {e.date && <span style={{ color: 'var(--muted)', fontFamily: 'monospace', fontSize: '0.85em', marginRight: '0.5em' }}>{e.date}</span>}
            {e.text}
          </div>
        ))}
      </div>
    </div>
  );
}
