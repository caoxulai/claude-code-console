import { useEffect, useState } from 'react';
import { FiBarChart2, FiCpu, FiFolder, FiUsers, FiTrendingUp, FiGrid, FiTool } from 'react-icons/fi';
import { SkeletonLine } from '../components/Skeleton';
import ActivityHeatmap from '../components/ActivityHeatmap';
import BudgetAlert, { BudgetSetupButton } from '../components/BudgetAlert';

function fmtTokens(n) {
  if (n == null) return '0';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${n}`;
}

function fmtCost(n) {
  if (n == null) return '$0.00';
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function shortDate(iso) {
  const [, m, d] = iso.split('-');
  const months = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${months[parseInt(m, 10)] || m} ${d}`;
}

function HeroCard({ label, value, sub }) {
  return (
    <div style={{
      padding: 'var(--space-lg) var(--space-md)',
      textAlign: 'center',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      borderRight: '1px solid var(--border)',
    }}>
      <div style={{ fontSize: '1.8em', fontWeight: 700, color: 'var(--text)', lineHeight: 1.1 }}>{value}</div>
      <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', fontWeight: 600, textTransform: 'uppercase', marginTop: '0.4em', letterSpacing: '0.03em' }}>{label}</div>
      {sub && <div style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', marginTop: '0.15em' }}>{sub}</div>}
    </div>
  );
}

function DailyBarChart({ daily, metric, setMetric }) {
  const [hover, setHover] = useState(null);

  if (!daily || daily.length === 0) return null;

  const getValue = (d) => {
    if (metric === 'cost') return d.cost;
    if (metric === 'messages') return d.messages;
    return d.inputTokens + d.outputTokens + d.cacheReadTokens + d.cacheWriteTokens;
  };
  const fmtVal = metric === 'cost' ? fmtCost : metric === 'messages' ? (n => n.toLocaleString()) : fmtTokens;

  const values = daily.map(getValue);
  const max = Math.max(...values, 1);

  const W = 800;
  const H = 200;
  const padTop = 12;
  const padBottom = 28;
  const padLeft = 8;
  const padRight = 8;
  const chartH = H - padTop - padBottom;
  const innerW = W - padLeft - padRight;
  const slot = innerW / daily.length;
  const barW = Math.max(3, Math.min(48, slot * 0.7));
  const labelEvery = Math.max(1, Math.ceil(38 / Math.max(slot, 1)));

  const ticks = 4;
  const gridlines = Array.from({ length: ticks + 1 }, (_, i) => {
    const frac = i / ticks;
    return { y: padTop + chartH * (1 - frac), value: max * frac };
  });

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.5em' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
          <FiTrendingUp size={15} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 600 }}>Daily trend</span>
          <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
            {daily.length} active day{daily.length === 1 ? '' : 's'}
          </span>
        </div>
        <div style={{ display: 'flex', gap: '0.3em' }}>
          {['tokens', 'cost', 'messages'].map(m => (
            <button
              key={m}
              className="btn"
              onClick={() => setMetric(m)}
              style={{
                fontSize: '0.72em', padding: '0.2em 0.6em',
                background: metric === m ? 'var(--accent)' : undefined,
                color: metric === m ? '#fff' : undefined,
                borderColor: metric === m ? 'var(--accent)' : undefined,
              }}
            >
              {m === 'tokens' ? 'Tokens' : m === 'cost' ? 'Cost' : 'Messages'}
            </button>
          ))}
        </div>
      </div>

      <div className="card" style={{ padding: 'var(--space-md) var(--space-md) var(--space-sm)', position: 'relative' }}>
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="xMidYMid meet" style={{ display: 'block' }}>
          {gridlines.map((g, i) => (
            <g key={i}>
              <line x1={0} y1={g.y} x2={W} y2={g.y} stroke="var(--border)" strokeWidth="0.5" />
              <text x={2} y={g.y - 2} fontSize="9" fill="var(--muted)">{fmtVal(g.value)}</text>
            </g>
          ))}
          {daily.map((d, i) => {
            const v = getValue(d);
            const h = max > 0 ? (v / max) * chartH : 0;
            const cx = padLeft + slot * (i + 0.5);
            const x = cx - barW / 2;
            const y = padTop + (chartH - h);
            const isHover = hover === i;
            return (
              <g key={d.date}>
                <rect
                  x={x} y={y} width={barW} height={Math.max(h, v > 0 ? 1 : 0)}
                  rx={2}
                  fill={isHover ? 'var(--text)' : 'var(--accent)'}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                  style={{ cursor: 'pointer', transition: 'fill 0.1s' }}
                />
                {i % labelEvery === 0 && (
                  <text x={cx} y={H - padBottom + 14} fontSize="9" fill="var(--muted)" textAnchor="middle">
                    {shortDate(d.date)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {hover != null && (
          <div style={{
            position: 'absolute', top: 0, right: 0,
            background: 'var(--surface2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', padding: '0.5em 0.75em',
            fontSize: 'var(--fs-xs)', lineHeight: 1.5, pointerEvents: 'none',
            boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
          }}>
            <div style={{ fontWeight: 700, marginBottom: '0.2em' }}>{daily[hover].date}</div>
            <div>Tokens: {fmtTokens(daily[hover].inputTokens + daily[hover].outputTokens + daily[hover].cacheReadTokens + daily[hover].cacheWriteTokens)}</div>
            <div style={{ color: 'var(--muted)' }}>in {fmtTokens(daily[hover].inputTokens)} · out {fmtTokens(daily[hover].outputTokens)}</div>
            <div>Msgs: {daily[hover].messages.toLocaleString()}</div>
            <div>Cost: {fmtCost(daily[hover].cost)}</div>
          </div>
        )}
      </div>
    </div>
  );
}

function BreakdownRow({ label, cost, maxCost }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', padding: '0.2em 0' }}>
      <span style={{
        fontSize: 'var(--fs-xs)',
        color: 'var(--text)',
        width: '45%',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        fontWeight: 500,
        flexShrink: 0,
      }} title={label}>
        {label}
      </span>
      <div style={{ flex: 1, height: 5, background: 'var(--surface2)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${(cost / maxCost) * 100}%`, height: '100%', background: 'var(--accent)', borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', width: 56, textAlign: 'right', flexShrink: 0 }}>
        {fmtCost(cost)}
      </span>
    </div>
  );
}

function ViewAllModal({ title, rows, onClose }) {
  const maxCost = Math.max(...rows.map(r => r.cost), 0.0001);
  return (
    <div
      style={{ position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(2px)' }}
      onClick={onClose}
    >
      <div onClick={e => e.stopPropagation()} style={{ width: '100%', maxWidth: 480, maxHeight: '70vh', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', boxShadow: '0 16px 48px rgba(0,0,0,0.4)', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '0.75em 1em', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)' }}>{title}</span>
          <button onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: '1.2em', lineHeight: 1 }}>×</button>
        </div>
        <div style={{ padding: '0.75em 1em', overflowY: 'auto', flex: 1 }}>
          {rows.map(r => <BreakdownRow key={r.label} label={r.label} cost={r.cost} maxCost={maxCost} />)}
        </div>
      </div>
    </div>
  );
}

function BreakdownPanel({ icon: Icon, title, rows, modalOpen, onOpenModal }) {
  if (!rows || rows.length === 0) return null;
  const maxCost = Math.max(...rows.map(r => r.cost), 0.0001);
  const top = rows.slice(0, 5);

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.5em' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.4em' }}>
          <Icon size={13} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 600 }}>{title}</span>
        </div>
        {rows.length > 5 && (
          <button
            onClick={onOpenModal}
            style={{ background: 'none', border: 'none', color: 'var(--accent)', fontSize: 'var(--fs-xs)', cursor: 'pointer', fontWeight: 500 }}
          >
            View all {rows.length}
          </button>
        )}
      </div>
      <div className="card" style={{ padding: 'var(--space-md)' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.15em' }}>
          {top.map(r => <BreakdownRow key={r.label} label={r.label} cost={r.cost} maxCost={maxCost} />)}
        </div>
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
  const [heatmap, setHeatmap] = useState(null);
  const [tools, setTools] = useState(null);
  const [error, setError] = useState(null);
  const [chartMetric, setChartMetric] = useState('tokens');
  const [modal, setModal] = useState(null);

  useEffect(() => {
    fetch('/api/usage')
      .then(r => r.json())
      .then(setData)
      .catch(() => setError('Failed to load usage data.'));
    fetch('/api/usage/heatmap')
      .then(r => r.json())
      .then(setHeatmap)
      .catch(() => {});
    fetch('/api/usage/tools')
      .then(r => r.json())
      .then(setTools)
      .catch(() => {});
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
  const daily = data.daily || [];
  const activeDays = daily.length;
  const windowCost = daily.reduce((s, d) => s + d.cost, 0);
  const avgCostPerDay = activeDays > 0 ? windowCost / activeDays : 0;
  const todayCost = daily.length > 0 ? daily[daily.length - 1].cost : 0;

  return (
    <div>
      <div className="page-header">
        <h2>Token Usage</h2>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75em' }}>
          <BudgetSetupButton />
        </div>
      </div>

      <BudgetAlert dailyCost={todayCost} />

      {/* Hero stats — 4 equal cards in a centered grid */}
      <div className="hero-grid" style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(4, 1fr)',
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius)',
        marginBottom: 'var(--space-lg)',
        overflow: 'hidden',
      }}>
        <HeroCard label="Total cost" value={fmtCost(t.cost)} sub="all-time" />
        <HeroCard label="Total tokens" value={fmtTokens(totalTokens)} sub={`${t.messages.toLocaleString()} messages`} />
        <HeroCard label="Avg / day" value={fmtCost(avgCostPerDay)} sub={`${activeDays} active day${activeDays === 1 ? '' : 's'}`} />
        <HeroCard
          label="Output tokens"
          value={fmtTokens(t.outputTokens)}
          sub={`${fmtTokens(t.inputTokens)} input · ${fmtTokens(t.cacheReadTokens)} cache`}
        />
      </div>

      {/* Daily trend chart */}
      {daily.length > 0 && (
        <div style={{ marginBottom: 'var(--space-lg)' }}>
          <DailyBarChart daily={daily} metric={chartMetric} setMetric={setChartMetric} />
        </div>
      )}

      {/* Activity heatmap */}
      {heatmap?.days && Object.keys(heatmap.days).length > 0 && (
        <div style={{ marginBottom: 'var(--space-lg)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', marginBottom: '0.5em' }}>
            <FiGrid size={13} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 600 }}>Activity heatmap</span>
            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>last 12 weeks · Pacific Time</span>
          </div>
          <div className="card" style={{ padding: 'var(--space-md)' }}>
            <ActivityHeatmap grid={heatmap.grid} days={heatmap.days} dailyHours={heatmap.dailyHours} />
          </div>
        </div>
      )}

      {/* 3-column breakdown — each with title outside, card inside */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
        gap: 'var(--space-md)',
        marginBottom: 'var(--space-lg)',
      }}>
        <BreakdownPanel icon={FiCpu} title="By model" rows={toRows(data.byModel)} onOpenModal={() => setModal({ title: 'By model', rows: toRows(data.byModel) })} />
        <BreakdownPanel icon={FiFolder} title="By project" rows={toRows(data.byProject)} onOpenModal={() => setModal({ title: 'By project', rows: toRows(data.byProject) })} />
        <BreakdownPanel icon={FiUsers} title="By agent" rows={toRows(data.byAgent)} onOpenModal={() => setModal({ title: 'By agent', rows: toRows(data.byAgent) })} />
      </div>

      {/* Tool leaderboard — clean ranked table */}
      {tools?.tools?.length > 0 && (
        <div style={{ marginBottom: 'var(--space-lg)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.5em' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
              <FiTool size={13} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 600 }}>Tool usage</span>
              <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>top 10</span>
            </div>
            {tools.tools.length > 10 && (
              <button
                onClick={() => setModal({ title: 'All tools', tools: tools.tools })}
                style={{ background: 'none', border: 'none', color: 'var(--accent)', fontSize: 'var(--fs-xs)', cursor: 'pointer', fontWeight: 500 }}
              >
                View all {tools.tools.length}
              </button>
            )}
          </div>
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th style={{ textAlign: 'left', width: '4%' }}>#</th>
                  <th style={{ textAlign: 'left' }}>Tool</th>
                  <th style={{ textAlign: 'right' }}>Calls</th>
                  <th style={{ textAlign: 'right', width: '35%' }}>Share</th>
                </tr>
              </thead>
              <tbody>
                {(() => {
                  const totalCalls = tools.tools.reduce((s, x) => s + x.calls, 0);
                  return tools.tools.slice(0, 10).map((tool, i) => {
                    const pct = totalCalls > 0 ? (tool.calls / totalCalls) * 100 : 0;
                    return (
                      <tr key={tool.name}>
                        <td style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)' }}>{i + 1}</td>
                        <td style={{ fontWeight: 500, fontFamily: 'monospace', fontSize: 'var(--fs-sm)' }}>{tool.name}</td>
                        <td style={{ textAlign: 'right', fontSize: 'var(--fs-sm)' }}>{tool.calls.toLocaleString()}</td>
                        <td style={{ textAlign: 'right' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em', justifyContent: 'flex-end' }}>
                            <div style={{ flex: 1, height: 5, background: 'var(--surface2)', borderRadius: 3, overflow: 'hidden', maxWidth: 120 }}>
                              <div style={{ width: `${pct}%`, height: '100%', background: 'var(--accent)', borderRadius: 3 }} />
                            </div>
                            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', minWidth: 36, textAlign: 'right' }}>{pct.toFixed(1)}%</span>
                          </div>
                        </td>
                      </tr>
                    );
                  });
                })()}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Modal popup for View All */}
      {modal && !modal.tools && <ViewAllModal title={modal.title} rows={modal.rows} onClose={() => setModal(null)} />}
      {modal?.tools && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(2px)' }}
          onClick={() => setModal(null)}
        >
          <div onClick={e => e.stopPropagation()} style={{ width: '100%', maxWidth: 520, maxHeight: '70vh', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', boxShadow: '0 16px 48px rgba(0,0,0,0.4)', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            <div style={{ padding: '0.75em 1em', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 600, fontSize: 'var(--fs-sm)' }}>{modal.title}</span>
              <button onClick={() => setModal(null)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: '1.2em', lineHeight: 1 }}>×</button>
            </div>
            <div style={{ overflowY: 'auto', flex: 1 }}>
              <table className="data-table" style={{ fontSize: 'var(--fs-sm)' }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: 'left', width: '4%' }}>#</th>
                    <th style={{ textAlign: 'left' }}>Tool</th>
                    <th style={{ textAlign: 'right' }}>Calls</th>
                    <th style={{ textAlign: 'right' }}>Share</th>
                  </tr>
                </thead>
                <tbody>
                  {(() => {
                    const totalCalls = modal.tools.reduce((s, x) => s + x.calls, 0);
                    return modal.tools.map((tool, i) => {
                      const pct = totalCalls > 0 ? (tool.calls / totalCalls) * 100 : 0;
                      return (
                        <tr key={tool.name}>
                          <td style={{ color: 'var(--muted)' }}>{i + 1}</td>
                          <td style={{ fontFamily: 'monospace', fontWeight: 500 }}>{tool.name}</td>
                          <td style={{ textAlign: 'right' }}>{tool.calls.toLocaleString()}</td>
                          <td style={{ textAlign: 'right', color: 'var(--muted)' }}>{pct.toFixed(1)}%</td>
                        </tr>
                      );
                    });
                  })()}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Footer disclaimer */}
      <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', lineHeight: 1.5, marginTop: 'var(--space-sm)' }}>
        <FiBarChart2 size={10} style={{ verticalAlign: '-1px', marginRight: 3 }} />
        Estimates based on public per-token prices. Cache reads at 0.1×, writes at 1.25× input rate. Includes subagent and workflow turns.
      </div>
    </div>
  );
}
