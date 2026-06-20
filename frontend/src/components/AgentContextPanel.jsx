import { useEffect, useMemo, useState, useCallback } from 'react';
import { FiAlertTriangle, FiCheck, FiInfo, FiScissors, FiTrash2 } from 'react-icons/fi';
import { decideReconcileOutcome, entryKey } from './reconcileOutcome';

// Self-contained append-only context + conflict-review panel for a single agent.
// Owns its own fetching (context + conflicts + ephemeral classification) and the
// reconcile/mark-reviewed actions, so it can be mounted INLINE under an agent
// entry anywhere — the standalone Agents page and the in-project Agents tab share
// this one implementation. `onChanged` lets the host refresh its agent list
// badges after a reconcile/review.
//
// Append-only is preserved here: the only entry-removing/rewriting actions are
// reconcile (keep/merge/compact/dismiss/sweep), and each is always the user's
// explicit click. Every removing/rewriting POST is etag-guarded; on a 409 we do
// a real re-review (show what arrived, require re-confirm) — NEVER a blind retry.

// `entryKey` (content-based, position-independent) and the reconcile-outcome
// decision logic live in ./reconcileOutcome so the component and its Node
// branch-table test exercise the exact same code path.

export default function AgentContextPanel({ projectId, slug, onChanged }) {
  const [context, setContext] = useState(null);
  const [conflicts, setConflicts] = useState([]);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState('conflicts'); // 'conflicts' | 'ephemeral'

  // Cluster merge editor.
  const [mergeFor, setMergeFor] = useState(null);
  const [mergeText, setMergeText] = useState('');
  // Per-cluster chosen survivor index (for supersede "keep newest / correction").
  const [keepChoice, setKeepChoice] = useState({});
  // Per-entry compact editor.
  const [compactFor, setCompactFor] = useState(null);
  const [compactText, setCompactText] = useState('');

  // Ephemeral-sweep mode state.
  const [ephemeral, setEphemeral] = useState(null); // { ephemeralIndices, durableIndices } | null
  const [sweepSel, setSweepSel] = useState(() => new Set());

  // 409 re-review notice: number of new entries that arrived since this panel
  // loaded. Non-null ⇒ show the warn banner and require the user to act again.
  const [conflictArrivals, setConflictArrivals] = useState(null);
  // Network/server-error notice: set when a reconcile POST threw or returned a
  // non-409 failure, so the click is never a silent no-op. The user's typed
  // text is preserved; the button re-enables for another try.
  const [actionError, setActionError] = useState(null);
  // Which button group the user last acted on, so the inline 409/error feedback
  // renders next to THAT button (e.g. 'cluster:2', 'compact:5', 'sweep') instead
  // of repeating in every cluster card. Cleared on a clean success.
  const [actedKey, setActedKey] = useState(null);

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

  // Fetch the durable-vs-ephemeral classification when entering that mode.
  const loadEphemeral = useCallback(async () => {
    try {
      const res = await fetch(`${base()}/ephemeral`);
      const data = await res.json();
      const eph = Array.isArray(data.ephemeralIndices) ? data.ephemeralIndices : [];
      setEphemeral({
        ephemeralIndices: eph,
        durableIndices: Array.isArray(data.durableIndices) ? data.durableIndices : [],
      });
      setSweepSel(new Set(eph)); // default: all ephemeral selected
    } catch {
      setEphemeral({ ephemeralIndices: [], durableIndices: [] });
      setSweepSel(new Set());
    }
  }, [base]);

  useEffect(() => {
    if (mode === 'ephemeral') loadEphemeral();
  }, [mode, loadEphemeral]);

  // Centralized reconcile POST. `payload` is the body (without etag, which we
  // attach from the loaded context). Returns a structured outcome string:
  //   'ok'       — the write applied; caller may clear its open editor.
  //   'conflict' — 409, the file moved under us; the panel reloaded, adopted the
  //                fresh etag, and shows the re-review banner. Caller KEEPS its
  //                editor open so the typed text survives for the second click.
  //   'error'    — network/throw or a non-409 failure; the panel shows the
  //                error banner and re-enables. Caller KEEPS its editor open.
  // The branch decision is delegated to the pure decideReconcileOutcome() so the
  // same logic is unit-tested in reconcileOutcome.test.mjs.
  const postReconcile = useCallback(async (payload, key = null) => {
    const loadedKeys = new Set((context?.entries || []).map(entryKey));
    setBusy(true);
    setActionError(null);
    setActedKey(key);
    // httpStatus stays null on a thrown/network fetch → the pure function maps
    // that to outcome 'error' (never a silent no-op).
    let httpStatus = null;
    let body = {};
    try {
      try {
        const res = await fetch(`${base()}/reconcile`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...payload, etag: context?.etag }),
        });
        httpStatus = res.status;
        try { body = await res.json(); } catch { body = {}; }
      } catch { /* network failure / thrown fetch: httpStatus stays null */ }

      if (httpStatus === 409) {
        // Real re-review: the file moved under us. Reload current state so the
        // banner counts what actually arrived, then let the pure function decide.
        let fresh = { entries: [] };
        try {
          const [ctxRes, confRes] = await Promise.all([fetch(base()), fetch(`${base()}/conflicts`)]);
          fresh = await ctxRes.json();
          setContext(() => {
            const next = { ...fresh };
            if (body && body.etag) next.etag = body.etag;
            return next;
          });
          setConflicts((await confRes.json()).clusters || []);
          if (mode === 'ephemeral') await loadEphemeral();
        } catch { /* reload best-effort; the decision below still fires */ }
        const decision = decideReconcileOutcome({
          httpStatus, body, loadedKeys, freshEntries: fresh.entries || [],
        });
        setConflictArrivals(decision.arrivedCount);
        return decision.outcome; // 'conflict'
      }

      const decision = decideReconcileOutcome({ httpStatus, body, loadedKeys });
      if (decision.outcome === 'ok') {
        setConflictArrivals(null);
        setActionError(null);
        setActedKey(null);
        await load();
        if (mode === 'ephemeral') await loadEphemeral();
        onChanged?.();
      } else {
        // Non-409 failure or network throw: never a silent no-op — show a notice
        // and keep the user's typed text. The button re-enables in finally.
        setActionError('Could not reach the server — your text is kept; try again.');
      }
      return decision.outcome; // 'ok' | 'error'
    } finally {
      // setBusy(false) is finally-guarded so the button can NEVER stick disabled,
      // on success, 409, a non-409 status, or any throw above.
      setBusy(false);
    }
  }, [base, context, load, loadEphemeral, mode, onChanged]);

  // Mark-reviewed routes through the SAME etag/409 re-review contract as
  // reconcile (see postReconcile), NOT a blind retry: if an agent appended a new
  // entry between load and this click, the stale etag 409s, we reload to reveal
  // the arrival, show the "N entries arrived… click again" banner, and require a
  // SECOND deliberate click. So an entry the user never saw is never silently
  // cleared from the "N new" signal. The unchanged-file happy path clears the
  // badge in one click (etag matches → 200).
  const markReviewed = useCallback(async () => {
    const loadedKeys = new Set((context?.entries || []).map(entryKey));
    setBusy(true);
    setActionError(null);
    setActedKey('mark-reviewed');
    let httpStatus = null;
    let body = {};
    try {
      try {
        const res = await fetch(`${base()}/mark-reviewed`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ etag: context?.etag }),
        });
        httpStatus = res.status;
        try { body = await res.json(); } catch { body = {}; }
      } catch { /* network failure / thrown fetch: httpStatus stays null */ }

      if (httpStatus === 409) {
        // The file moved under us — an entry arrived since we loaded. Reload so
        // the new entry shows highlighted-new in the list and the count reflects
        // it, adopt the fresh etag for the second click, then surface the banner.
        let fresh = { entries: [] };
        try {
          const [ctxRes, confRes] = await Promise.all([fetch(base()), fetch(`${base()}/conflicts`)]);
          fresh = await ctxRes.json();
          setContext(() => {
            const next = { ...fresh };
            if (body && body.etag) next.etag = body.etag;
            return next;
          });
          setConflicts((await confRes.json()).clusters || []);
          if (mode === 'ephemeral') await loadEphemeral();
        } catch { /* reload best-effort; the decision below still fires */ }
        const decision = decideReconcileOutcome({
          httpStatus, body, loadedKeys, freshEntries: fresh.entries || [],
        });
        setConflictArrivals(decision.arrivedCount);
        return;
      }

      const decision = decideReconcileOutcome({ httpStatus, body, loadedKeys });
      if (decision.outcome === 'ok') {
        setConflictArrivals(null);
        setActionError(null);
        setActedKey(null);
        await load();
        onChanged?.();
      } else {
        // Non-409 failure or network throw: never a silent no-op, and the badge
        // is NOT cleared. Show the notice and re-enable for another try.
        setActionError('Could not reach the server — try again.');
      }
    } finally {
      setBusy(false);
    }
  }, [base, context, load, loadEphemeral, mode, onChanged]);

  // Acknowledge an honestly-large (oversized but fully-reconciled) context file
  // so its size badge stops nagging. Etag-guarded + 409 re-review like the other
  // actions: if the file grew/changed under us the ack is refused and the panel
  // reloads, because a newly-arrived conflict/ephemeral could make it actionable
  // again. The backend gates the ack server-side too (it refuses when the file is
  // still actionable). On success the list + panel refresh so both badge sites
  // update. POST .../context/oversize-ack mirrors the existing context routes.
  const acknowledgeOversize = useCallback(async () => {
    setBusy(true);
    setActionError(null);
    setActedKey('oversize-ack');
    let httpStatus = null;
    try {
      try {
        const res = await fetch(`${base()}/oversize-ack`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ etag: context?.etag }),
        });
        httpStatus = res.status;
        try { await res.json(); } catch { /* body unused — load() re-reads state */ }
      } catch { /* network failure / thrown fetch: httpStatus stays null */ }

      if (httpStatus === 409) {
        // The file changed under us — reconciling/sweeping may now help again,
        // so the ack was refused. Reload (re-reads the fresh etag + oversize
        // fields) and surface the generic re-review banner; no blind retry.
        setConflictArrivals(0);
        await load();
        if (mode === 'ephemeral') await loadEphemeral();
        return;
      }
      if (typeof httpStatus === 'number' && httpStatus >= 200 && httpStatus < 300) {
        setConflictArrivals(null);
        setActionError(null);
        setActedKey(null);
        await load();
        onChanged?.();
      } else {
        setActionError('Could not reach the server — try again.');
      }
    } finally {
      setBusy(false);
    }
  }, [base, context, load, loadEphemeral, mode, onChanged]);

  // ── Derived view state (hooks must run before any early return) ────────────
  const entries = context?.entries || [];

  // Content-based "new" detection: prefer the backend's newEntryIndices set so a
  // mid-file insertion is highlighted correctly; fall back to the count-based
  // tail heuristic only when the backend/marker predates that field.
  const newIndexSet = useMemo(() => {
    if (Array.isArray(context?.newEntryIndices)) return new Set(context.newEntryIndices);
    return null;
  }, [context]);
  const reviewedCount = context?.lastReviewedCount || 0;
  const isNew = useCallback(
    (i) => (newIndexSet ? newIndexSet.has(i) : i >= reviewedCount),
    [newIndexSet, reviewedCount],
  );

  if (!context) {
    return <div style={{ color: 'var(--muted)', fontSize: '0.85em', padding: '0.5em' }}>Loading context…</div>;
  }

  if (!context.exists || entries.length === 0) {
    return (
      <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: '0.5em 0', lineHeight: 1.5 }}>
        No context yet. This agent accumulates append-only dated entries as it works;
        conflicts and redundancies surface here for you to reconcile.
      </div>
    );
  }

  const newCount = newIndexSet ? newIndexSet.size : (context.newEntryCount || 0);
  const oversized = !!context.oversized;
  // Oversize signal, split by the backend's honesty fields (read defensively so
  // a not-yet-updated backend, which only sends `oversized`, degrades to the old
  // warning-only behavior — never crashes):
  //   - actionable: oversized AND reconciling/sweeping would shrink the file
  //     (conflicts or ephemerals remain). A fix exists → keep the WARNING badge,
  //     no Acknowledge button (honesty gate: push the user to reconcile/sweep).
  //   - !actionable: honestly large, nothing left to fix → NEUTRAL informational
  //     badge + an "Acknowledge size" button (until acknowledged).
  // When the backend omits oversizeActionable (old build), treat oversized as
  // actionable so we never hide a possibly-fixable oversize behind an ack.
  const oversizeActionable = oversized && (
    context.oversizeActionable === undefined ? true : !!context.oversizeActionable
  );
  const oversizeAcknowledged = !!context.oversizeAcknowledged;
  const entryByIndex = (i) => entries.find((e) => e.index === i);

  const segBtn = (active) => ({
    cursor: 'pointer',
    fontSize: 'var(--fs-xs)',
    padding: '0.25em 0.7em',
    border: '1px solid var(--border)',
    background: active ? 'var(--accent)' : 'transparent',
    color: active ? '#000' : 'var(--muted)',
    fontWeight: active ? 600 : 400,
  });

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
        {/* Oversize signal — honest about whether action would help:
            actionable → amber warning ("reconcile/sweep would shrink this");
            acknowledged → quiet neutral chip ("large — acknowledged");
            honestly-large/unacknowledged → neutral info badge + Acknowledge. */}
        {oversizeActionable ? (
          <span
            className="badge badge-warn"
            title="This context file exceeds the size ceiling (~400 lines / ~6KB) AND has conflicts or ephemeral entries left to reconcile/sweep — doing so would shrink it."
          >
            <FiAlertTriangle size={9} style={{ marginRight: 3 }} />oversized — needs reconciliation
          </span>
        ) : oversized && oversizeAcknowledged ? (
          <span
            className="badge"
            title="This file is honestly large and you've acknowledged it. It won't nag unless new conflicts or ephemeral entries appear."
          >
            <FiInfo size={9} style={{ marginRight: 3 }} />large — acknowledged
          </span>
        ) : oversized ? (
          <span
            className="badge"
            title="This file is past the size ceiling but everything reconcilable has been handled — it's honestly large. Acknowledge to stop the reminder."
          >
            <FiInfo size={9} style={{ marginRight: 3 }} />large (reviewed)
          </span>
        ) : null}
        {oversized && !oversizeActionable && !oversizeAcknowledged && (
          <button
            className="btn"
            onClick={acknowledgeOversize}
            disabled={busy}
            title="Stop the size reminder for this honestly-large file. It will resurface if new conflicts or ephemeral entries appear."
            style={{ fontSize: 'var(--fs-xs)' }}
          >
            <FiCheck size={11} style={{ marginRight: 3 }} /> Acknowledge size
          </button>
        )}
        {newCount > 0 && (
          <button className="btn" onClick={markReviewed} disabled={busy} style={{ fontSize: 'var(--fs-xs)', marginLeft: 'auto' }}>
            <FiCheck size={12} /> Mark reviewed
          </button>
        )}
      </div>

      {/* Mode toggle: conflict clusters vs durable/ephemeral sweep. */}
      <div style={{ display: 'inline-flex', borderRadius: 4, overflow: 'hidden', marginBottom: 'var(--space-sm)' }}>
        <button type="button" style={segBtn(mode === 'conflicts')} onClick={() => setMode('conflicts')}>Conflicts</button>
        <button type="button" style={{ ...segBtn(mode === 'ephemeral'), borderLeft: 'none' }} onClick={() => setMode('ephemeral')}>Durable vs ephemeral</button>
      </div>

      {/* Global notice at the top (covers Mark-reviewed and acts as a summary). */}
      <ReconcileFeedback conflictArrivals={conflictArrivals} actionError={actionError} />

      {mode === 'conflicts' && (
        <>
          {conflicts.length === 0 && (
            <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: '0 0 var(--space-sm)', lineHeight: 1.5 }}>
              No conflicts or near-duplicates detected.
            </div>
          )}

          {conflicts.map((c, ci) => {
            const superseded = c.reason === 'superseded';
            const cIndices = c.entryIndices || [];
            const newestIndex = cIndices.length ? Math.max(...cIndices) : 0;
            const oldestIndex = cIndices.length ? Math.min(...cIndices) : 0;
            const chosen = keepChoice[ci] != null ? keepChoice[ci] : (superseded ? newestIndex : oldestIndex);
            return (
              <div key={ci} className="card" style={{ padding: 'var(--space-sm)', marginBottom: 'var(--space-sm)', borderColor: 'var(--warning)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em', marginBottom: '0.4em' }}>
                  <FiAlertTriangle size={12} style={{ color: 'var(--warning)' }} />
                  <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)' }}>
                    {superseded ? 'Superseded / corrected' : 'Possibly redundant'}
                  </span>
                  <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>{cIndices.length} entries</span>
                </div>
                <ul style={{ margin: '0 0 0.5em', paddingLeft: superseded ? '0.2em' : '1.1em', listStyle: superseded ? 'none' : 'disc', fontSize: 'var(--fs-sm)', lineHeight: 1.5 }}>
                  {c.entries.map((e) => (
                    <li key={e.index} style={{ marginBottom: superseded ? '0.25em' : 0 }}>
                      {superseded && (
                        <label style={{ cursor: 'pointer', marginRight: '0.4em' }} title="Pick the entry to keep as the survivor">
                          <input
                            type="radio"
                            name={`keep-${ci}`}
                            checked={chosen === e.index}
                            onChange={() => setKeepChoice((prev) => ({ ...prev, [ci]: e.index }))}
                            style={{ marginRight: '0.3em', verticalAlign: 'middle' }}
                          />
                        </label>
                      )}
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
                      onChange={(e) => setMergeText(e.target.value)}
                      placeholder="Merged entry text"
                      style={{ width: '100%', minHeight: 60, fontSize: 'var(--fs-sm)' }}
                    />
                    <div style={{ display: 'flex', gap: '0.4em', marginTop: '0.4em' }}>
                      <button
                        className="btn btn-primary"
                        disabled={busy || !mergeText.trim()}
                        onClick={async () => {
                          const outcome = await postReconcile({ action: 'merge', entryIndices: cIndices, mergedText: mergeText.trim() }, `cluster:${ci}`);
                          // Clear the editor ONLY on a clean success; on 'conflict'
                          // or 'error' keep it open so the typed text survives.
                          if (outcome === 'ok') { setMergeFor(null); setMergeText(''); }
                        }}
                      >
                        Save merged
                      </button>
                      <button className="btn" onClick={() => { setMergeFor(null); setMergeText(''); }}>Cancel</button>
                    </div>
                    {/* Feedback inline, right below Save merged, so a 409/error
                        is visible without scrolling away from the editor. */}
                    {actedKey === `cluster:${ci}` && (
                      <ReconcileFeedback conflictArrivals={conflictArrivals} actionError={actionError} compact />
                    )}
                  </div>
                ) : (
                  <>
                    <div style={{ display: 'flex', gap: '0.4em', flexWrap: 'wrap' }}>
                      <button
                        className="btn"
                        disabled={busy}
                        title={superseded
                          ? 'Keep the selected entry (the correction, newest by default) and drop the others'
                          : 'Keep the oldest entry in this cluster, drop the others'}
                        onClick={() => postReconcile({ action: 'keep', entryIndices: cIndices, keepIndex: superseded ? chosen : oldestIndex }, `cluster:${ci}`)}
                      >
                        {superseded ? 'Keep newest / correction' : 'Keep oldest'}
                      </button>
                      <button
                        className="btn"
                        disabled={busy}
                        title="Replace the cluster with one entry you edit (pre-filled with all entries so you can combine them)"
                        onClick={() => { setMergeFor(ci); setMergeText(c.entries.map((e) => e.text).join('\n')); }}
                      >
                        Merge…
                      </button>
                      <button
                        className="btn"
                        disabled={busy}
                        title="Not actually a conflict — keep all and stop flagging this cluster"
                        onClick={() => postReconcile({ action: 'dismiss', entryIndices: cIndices }, `cluster:${ci}`)}
                      >
                        Keep all
                      </button>
                    </div>
                    {/* Feedback for Keep / Keep all, next to those buttons. */}
                    {actedKey === `cluster:${ci}` && (
                      <ReconcileFeedback conflictArrivals={conflictArrivals} actionError={actionError} compact />
                    )}
                  </>
                )}
              </div>
            );
          })}
        </>
      )}

      {mode === 'ephemeral' && (
        <EphemeralMode
          ephemeral={ephemeral}
          entries={entries}
          sweepSel={sweepSel}
          setSweepSel={setSweepSel}
          busy={busy}
          onSweep={() => postReconcile({ action: 'sweep', entryIndices: [...sweepSel] }, 'sweep')}
          conflictArrivals={conflictArrivals}
          actionError={actionError}
          showFeedback={actedKey === 'sweep'}
        />
      )}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {[...entries].reverse().map((e, i, arr) => (
          <div
            key={e.index}
            style={{
              padding: '0.5em 0.8em',
              borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 'none',
              background: isNew(e.index) ? 'var(--user-bg)' : 'transparent',
              fontSize: 'var(--fs-sm)', lineHeight: 1.5,
            }}
          >
            <div>
              {e.date && <span style={{ color: 'var(--muted)', fontFamily: 'monospace', fontSize: '0.85em', marginRight: '0.5em' }}>{e.date}</span>}
              {e.text}
            </div>
            {compactFor === e.index ? (
              <div style={{ marginTop: '0.4em' }}>
                <textarea
                  className="form-textarea"
                  value={compactText}
                  onChange={(ev) => setCompactText(ev.target.value)}
                  placeholder="Shorter replacement text (the date prefix is preserved)"
                  style={{ width: '100%', minHeight: 60, fontSize: 'var(--fs-sm)' }}
                />
                <div style={{ display: 'flex', gap: '0.4em', marginTop: '0.4em' }}>
                  <button
                    className="btn btn-primary"
                    disabled={busy || !compactText.trim()}
                    onClick={async () => {
                      const outcome = await postReconcile({ action: 'compact', entryIndices: [e.index], compactText: compactText.trim() }, `compact:${e.index}`);
                      if (outcome === 'ok') { setCompactFor(null); setCompactText(''); }
                    }}
                  >
                    Save
                  </button>
                  <button className="btn" onClick={() => { setCompactFor(null); setCompactText(''); }}>Cancel</button>
                </div>
                {/* Feedback inline under the compact Save button. */}
                {actedKey === `compact:${e.index}` && (
                  <ReconcileFeedback conflictArrivals={conflictArrivals} actionError={actionError} compact />
                )}
              </div>
            ) : (
              <button
                className="btn"
                disabled={busy}
                title="Replace this single entry with a shorter version (date preserved)"
                style={{ marginTop: '0.35em', fontSize: 'var(--fs-xs)', padding: '0.15em 0.55em' }}
                onClick={() => { setCompactFor(e.index); setCompactText(e.text); }}
              >
                <FiScissors size={11} style={{ marginRight: 3, verticalAlign: '-1px' }} /> Compact…
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// Inline feedback shown right next to whichever button was clicked, so a 409
// re-review or a network/server error is never an invisible no-op at the bottom
// of a scrolled panel. Renders nothing when there's no notice. Reuses the
// existing .badge-warn styling and var(--warning) tokens (comfortable contrast
// in both themes). `compact` shrinks it for use inside an editor block.
function ReconcileFeedback({ conflictArrivals, actionError, compact = false }) {
  if (conflictArrivals == null && !actionError) return null;
  const style = {
    display: 'block',
    padding: compact ? '0.4em 0.6em' : '0.5em 0.7em',
    marginTop: compact ? '0.5em' : 0,
    marginBottom: compact ? 0 : 'var(--space-sm)',
    lineHeight: 1.5,
    whiteSpace: 'normal',
    fontSize: compact ? 'var(--fs-xs)' : undefined,
  };
  return (
    <>
      {actionError && (
        <div className="badge badge-warn" style={style} role="alert">
          <FiAlertTriangle size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          {actionError}
        </div>
      )}
      {conflictArrivals != null && (
        <div className="badge badge-warn" style={style} role="alert">
          <FiAlertTriangle size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          {conflictArrivals > 0
            ? `${conflictArrivals} new ${conflictArrivals === 1 ? 'entry' : 'entries'} arrived since you opened this panel — review the changes below, then click again to re-confirm.`
            : 'The context file changed since you opened this panel — review the changes below, then click again to re-confirm.'}
        </div>
      )}
    </>
  );
}

// Durable-vs-ephemeral review mode: groups likely-ephemeral verification-ceremony
// entries for a single bulk-drop (etag-guarded human action), with durable
// entries shown separately and untouched. Nothing is removed without the click.
function EphemeralMode({ ephemeral, entries, sweepSel, setSweepSel, busy, onSweep, conflictArrivals, actionError, showFeedback }) {
  if (!ephemeral) {
    return <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: '0 0 var(--space-sm)' }}>Classifying entries…</div>;
  }
  const byIndex = (i) => entries.find((e) => e.index === i);
  const ephEntries = ephemeral.ephemeralIndices.map(byIndex).filter(Boolean);
  const durEntries = (ephemeral.durableIndices.length
    ? ephemeral.durableIndices
    : entries.map((e) => e.index).filter((i) => !ephemeral.ephemeralIndices.includes(i))
  ).map(byIndex).filter(Boolean);

  const toggle = (i) => setSweepSel((prev) => {
    const next = new Set(prev);
    if (next.has(i)) next.delete(i); else next.add(i);
    return next;
  });
  const allSelected = ephEntries.length > 0 && ephEntries.every((e) => sweepSel.has(e.index));
  const setAll = (on) => setSweepSel(on ? new Set(ephEntries.map((e) => e.index)) : new Set());

  if (ephEntries.length === 0) {
    return (
      <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-sm)', padding: '0 0 var(--space-sm)', lineHeight: 1.5 }}>
        No likely-ephemeral (point-in-time verification) entries detected — everything looks durable.
      </div>
    );
  }

  return (
    <div className="card" style={{ padding: 'var(--space-sm)', marginBottom: 'var(--space-sm)', borderColor: 'var(--warning)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em', marginBottom: '0.4em', flexWrap: 'wrap' }}>
        <FiTrash2 size={12} style={{ color: 'var(--warning)' }} />
        <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)' }}>Likely ephemeral</span>
        <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
          {ephEntries.length} of {entries.length} entries — point-in-time verification with no future value
        </span>
      </div>
      <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: '0.5em', lineHeight: 1.5 }}>
        Review and uncheck anything worth keeping. Nothing is removed until you click <strong>Drop selected</strong>.
      </div>
      <label style={{ cursor: 'pointer', fontSize: 'var(--fs-xs)', display: 'inline-flex', alignItems: 'center', gap: '0.3em', marginBottom: '0.4em' }}>
        <input type="checkbox" checked={allSelected} onChange={(e) => setAll(e.target.checked)} />
        Select all ({sweepSel.size} selected)
      </label>
      <ul style={{ margin: '0 0 0.5em', paddingLeft: 0, listStyle: 'none', fontSize: 'var(--fs-sm)', lineHeight: 1.5 }}>
        {ephEntries.map((e) => (
          <li key={e.index} style={{ marginBottom: '0.3em' }}>
            <label style={{ cursor: 'pointer', display: 'flex', gap: '0.4em', alignItems: 'flex-start' }}>
              <input
                type="checkbox"
                checked={sweepSel.has(e.index)}
                onChange={() => toggle(e.index)}
                style={{ marginTop: '0.25em' }}
              />
              <span>
                {e.date && <span style={{ color: 'var(--muted)', fontFamily: 'monospace', fontSize: '0.85em', marginRight: '0.4em' }}>{e.date}</span>}
                {e.text}
              </span>
            </label>
          </li>
        ))}
      </ul>
      <div style={{ display: 'flex', gap: '0.4em', flexWrap: 'wrap' }}>
        <button
          className="btn btn-primary"
          disabled={busy || sweepSel.size === 0}
          title="Drop the selected ephemeral entries in one action (etag-guarded)"
          onClick={onSweep}
        >
          <FiTrash2 size={11} style={{ marginRight: 3, verticalAlign: '-1px' }} /> Drop selected ({sweepSel.size})
        </button>
        <button
          className="btn"
          disabled={busy}
          title="Keep everything — clears the selection, removes nothing"
          onClick={() => setAll(false)}
        >
          Keep all
        </button>
      </div>
      {/* Feedback inline under Drop selected, so a 409/error is visible here. */}
      {showFeedback && (
        <ReconcileFeedback conflictArrivals={conflictArrivals} actionError={actionError} compact />
      )}
      {durEntries.length > 0 && (
        <div style={{ marginTop: '0.6em', fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
          {durEntries.length} durable {durEntries.length === 1 ? 'entry is' : 'entries are'} kept untouched (listed below).
        </div>
      )}
    </div>
  );
}
