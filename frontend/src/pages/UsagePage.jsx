import { useEffect, useState } from 'react';
import { FiBarChart2, FiCpu, FiFolder, FiUsers, FiTrendingUp } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';
import UsageChart from '../components/UsageChart';

function fmtTokens(n) {
  if (n == null) return '0';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${n}`;
}

function fmtCost(n) {
  if (n == null) return '$0.00';
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const statCardStyle = {
  background: 'var(--surface2)',
  borderRadius: 'var(--radius)',
  padding: 'var(--space-md)',
  border: '1px solid var(--border)',
  textAlign: 'center',
};

function StatCard({ label, value, sub }) {
  return (
    <div style={statCardStyle}>
      <div style={{ fontSize: '1.6em', fontWeight: 700, color: 'var(--text)' }}>{value}</div>
      <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.3em' }}>{label}</div>
      {sub != null && <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', marginTop: '0.2em' }}>{sub}</div>}
    </div>
  );
}

// A breakdown table: rows sorted by cost desc, with a token + cost column and a
// proportional bar so the heavy hitters are obvious at a glance.
function BreakdownTable({ icon: Icon, title, rows, labelHeader }) {
  if (!rows || rows.length === 0) return null;
  const maxCost = Math.max(...rows.map(r => r.cost), 0.0001);
  return (
    <div style={{ marginBottom: 'var(--space-lg)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: '0.5em' }}>
        <Icon size={15} style={{ color: 'var(--accent)' }} />
        <h3 style={{ fontSize: '1em', fontWeight: 600, margin: 0 }}>{title}</h3>
      </div>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>{labelHeader}</th>
              <th style={{ textAlign: 'right' }}>Input</th>
              <th style={{ textAlign: 'right' }}>Output</th>
              <th style={{ textAlign: 'right' }}>Cache R/W</th>
              <th style={{ textAlign: 'right' }}>Msgs</th>
              <th style={{ textAlign: 'right', width: '22%' }}>Est. cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.label}>
                <td style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 0 }} title={r.label}>{r.label}</td>
                <td style={{ textAlign: 'right', fontSize: '0.85em' }}>{fmtTokens(r.inputTokens)}</td>
                <td style={{ textAlign: 'right', fontSize: '0.85em' }}>{fmtTokens(r.outputTokens)}</td>
                <td style={{ textAlign: 'right', fontSize: '0.85em', color: 'var(--muted)' }}>
                  {fmtTokens(r.cacheReadTokens)} / {fmtTokens(r.cacheWriteTokens)}
                </td>
                <td style={{ textAlign: 'right', fontSize: '0.85em' }}>{r.messages.toLocaleString()}</td>
                <td style={{ textAlign: 'right' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', justifyContent: 'flex-end' }}>
                    <div style={{ flex: 1, height: 6, background: 'var(--bg)', borderRadius: 3, overflow: 'hidden', maxWidth: 80 }}>
                      <div style={{ width: `${(r.cost / maxCost) * 100}%`, height: '100%', background: 'var(--accent)' }} />
                    </div>
                    <span style={{ fontWeight: 600, minWidth: 64, textAlign: 'right' }}>{fmtCost(r.cost)}</span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function toRows(map) {
  return Object.entries(map || {})
    .map(([label, v]) => ({ label, ...v }))
    .sort((a, b) => b.cost - a.cost);
}

export default function UsagePage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/usage')
      .then(r => r.json())
      .then(setData)
      .catch(() => setError('Failed to load usage data.'));
  }, []);

  if (error) {
    return (
      <div>
        <div className="page-header"><h2>Token Usage</h2></div>
        <div className="conflict-banner"><span>{error}</span></div>
      </div>
    );
  }

  if (!data) {
    return (
      <div>
        <div className="page-header"><h2>Token Usage</h2></div>
        <div className="card">{Array.from({ length: 5 }).map((_, i) => <SkeletonLine key={i} />)}</div>
      </div>
    );
  }

  const t = data.total;
  const totalTokens = t.inputTokens + t.outputTokens + t.cacheReadTokens + t.cacheWriteTokens;
  // Derived stats — computed directly from the verified totals, no separate source.
  const avgPerMsg = t.messages > 0 ? Math.round(totalTokens / t.messages) : 0;
  // Average cost per active day. Numerator and denominator must come from the
  // SAME window: the endpoint's `daily` series is the most-recent active days,
  // so average that window's own cost over its own day count (not all-time
  // cost, which would mismatch the window).
  const daily = data.daily || [];
  const activeDays = daily.length;
  const windowCost = daily.reduce((s, d) => s + d.cost, 0);
  const avgCostPerDay = activeDays > 0 ? windowCost / activeDays : 0;

  return (
    <div>
      <div className="page-header">
        <h2>Token Usage</h2>
        <span style={{ fontSize: '0.78em', color: 'var(--muted)' }}>
          Estimated from local transcripts · cost is approximate
        </span>
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
        gap: 'var(--space-md)',
        marginBottom: 'var(--space-lg)',
      }}>
        <StatCard label="Est. cost" value={fmtCost(t.cost)} sub="all-time" />
        <StatCard label="Total tokens" value={fmtTokens(totalTokens)} />
        <StatCard label="Input" value={fmtTokens(t.inputTokens)} />
        <StatCard label="Output" value={fmtTokens(t.outputTokens)} />
        <StatCard label="Cache read" value={fmtTokens(t.cacheReadTokens)} />
        <StatCard label="Cache write" value={fmtTokens(t.cacheWriteTokens)} />
        <StatCard label="Messages" value={t.messages.toLocaleString()} />
        <StatCard label="Avg / message" value={fmtTokens(avgPerMsg)} sub="tokens" />
        <StatCard label="Avg / active day" value={fmtCost(avgCostPerDay)} sub={`${activeDays} day${activeDays === 1 ? '' : 's'}`} />
      </div>

      {data.daily && data.daily.length > 0 && (
        <div style={{ marginBottom: 'var(--space-lg)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: '0.5em' }}>
            <FiTrendingUp size={15} style={{ color: 'var(--accent)' }} />
            <h3 style={{ fontSize: '1em', fontWeight: 600, margin: 0 }}>Daily trend</h3>
            <span style={{ fontSize: '0.75em', color: 'var(--muted)' }}>
              last {data.daily.length} active day{data.daily.length === 1 ? '' : 's'}
            </span>
          </div>
          <div className="card">
            <UsageChart daily={data.daily} />
          </div>
        </div>
      )}

      <BreakdownTable icon={FiCpu} title="By model" labelHeader="Model" rows={toRows(data.byModel)} />
      <BreakdownTable icon={FiUsers} title="Main thread vs. subagents" labelHeader="Agent" rows={toRows(data.byAgent)} />
      <BreakdownTable icon={FiFolder} title="By project" labelHeader="Project" rows={toRows(data.byProject)} />

      <div style={{ fontSize: '0.75em', color: 'var(--muted)', marginTop: 'var(--space-md)', lineHeight: 1.5 }}>
        <FiBarChart2 size={11} style={{ verticalAlign: '-1px', marginRight: 4 }} />
        Costs are estimates using public per-token list prices (cache reads at 0.1×, cache writes at 1.25× the input rate).
        Subagent and workflow turns are included. Actual billing may differ.
      </div>
    </div>
  );
}
