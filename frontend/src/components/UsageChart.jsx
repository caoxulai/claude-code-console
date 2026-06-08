import { useState, useRef, useEffect } from 'react';

// Track an element's content width so the chart can fill its container instead
// of rendering at a fixed width and hugging the left edge.
function useElementWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

// Dependency-free inline-SVG daily-trend chart. Renders one bar per day for the
// selected metric, with a hover tooltip showing the exact breakdown. No
// charting library — keeps the bundle small and avoids npm-mirror constraints.

function fmtTokens(n) {
  if (n == null) return '0';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${n}`;
}

function fmtCost(n) {
  return `$${(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function dayTokens(d) {
  return d.inputTokens + d.outputTokens + d.cacheReadTokens + d.cacheWriteTokens;
}

const METRICS = {
  tokens: { label: 'Total tokens', get: dayTokens, fmt: fmtTokens },
  cost: { label: 'Est. cost', get: d => d.cost, fmt: fmtCost },
  messages: { label: 'Messages', get: d => d.messages, fmt: n => n.toLocaleString() },
};

// Short axis-label form of an ISO date: "Jun 07".
function shortDate(iso) {
  const [, m, d] = iso.split('-');
  const months = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${months[parseInt(m, 10)] || m} ${d}`;
}

export default function UsageChart({ daily }) {
  const [metric, setMetric] = useState('tokens');
  const [hover, setHover] = useState(null);
  const [containerRef, containerWidth] = useElementWidth();

  if (!daily || daily.length === 0) {
    return <div style={{ color: 'var(--muted)', fontSize: '0.85em', padding: 'var(--space-md)' }}>No daily activity yet.</div>;
  }

  const m = METRICS[metric];
  const values = daily.map(m.get);
  const max = Math.max(...values, 0) || 1;

  // SVG geometry. The chart fills the measured container width so it stays
  // balanced (no left-hugging dead space) and auto-adapts to the number of
  // days. Bars are evenly distributed across the full width; bar thickness is
  // derived from the available space and capped so a handful of days don't
  // render as absurdly wide columns. Fall back to a sensible width before the
  // first measurement lands.
  const padLeft = 8;
  const padRight = 8;
  const W = Math.max(containerWidth || 640, 280);
  const H = 200;
  const padTop = 12;
  const padBottom = 28;
  const chartH = H - padTop - padBottom;
  const innerW = W - padLeft - padRight;
  const slot = innerW / daily.length;          // horizontal space per day
  const barW = Math.max(3, Math.min(48, slot * 0.7)); // thickness, capped both ways
  // Show every Nth x-label so date labels (~38px wide) never collide; with
  // wide slots every day is labelled, with narrow slots we thin them out.
  const labelEvery = Math.max(1, Math.ceil(38 / Math.max(slot, 1)));

  // A few horizontal gridlines with value labels.
  const ticks = 4;
  const gridlines = Array.from({ length: ticks + 1 }, (_, i) => {
    const frac = i / ticks;
    return { y: padTop + chartH * (1 - frac), value: max * frac };
  });

  return (
    <div>
      <div style={{ display: 'flex', gap: '0.4em', marginBottom: '0.75em' }}>
        {Object.entries(METRICS).map(([key, def]) => (
          <button
            key={key}
            className="btn"
            onClick={() => setMetric(key)}
            style={{
              fontSize: '0.78em',
              padding: '0.25em 0.7em',
              background: metric === key ? 'var(--accent)' : undefined,
              color: metric === key ? '#fff' : undefined,
              borderColor: metric === key ? 'var(--accent)' : undefined,
            }}
          >
            {def.label}
          </button>
        ))}
      </div>

      <div ref={containerRef} style={{ position: 'relative', width: '100%' }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          preserveAspectRatio="none"
          style={{ display: 'block' }}
        >
          {/* gridlines + y labels */}
          {gridlines.map((g, i) => (
            <g key={i}>
              <line x1={0} y1={g.y} x2={W} y2={g.y} stroke="var(--border)" strokeWidth="1" />
              <text x={2} y={g.y - 2} fontSize="9" fill="var(--muted)">{m.fmt(g.value)}</text>
            </g>
          ))}
          {/* bars */}
          {daily.map((d, i) => {
            const v = m.get(d);
            const h = max > 0 ? (v / max) * chartH : 0;
            // Center each bar within its evenly-sized slot across the full width.
            const cx = padLeft + slot * (i + 0.5);
            const x = cx - barW / 2;
            const y = padTop + (chartH - h);
            const isHover = hover === i;
            return (
              <g key={d.date}>
                <rect
                  x={x}
                  y={y}
                  width={barW}
                  height={Math.max(h, v > 0 ? 1 : 0)}
                  rx={2}
                  fill={isHover ? 'var(--text)' : 'var(--accent)'}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                  style={{ cursor: 'pointer' }}
                />
                {/* x label: show every Nth so labels never overlap. Density is
                    derived from how many ~38px-wide date labels fit the slot. */}
                {i % labelEvery === 0 && (
                  <text
                    x={x + barW / 2}
                    y={H - padBottom + 14}
                    fontSize="9"
                    fill="var(--muted)"
                    textAnchor="middle"
                  >
                    {shortDate(d.date)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {hover != null && (
          <div style={{
            position: 'absolute',
            top: 0,
            right: 0,
            background: 'var(--surface2)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            padding: '0.5em 0.75em',
            fontSize: '0.78em',
            lineHeight: 1.5,
            pointerEvents: 'none',
            boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
          }}>
            <div style={{ fontWeight: 700, marginBottom: '0.25em' }}>{daily[hover].date}</div>
            <div>Tokens: {fmtTokens(dayTokens(daily[hover]))}</div>
            <div style={{ color: 'var(--muted)' }}>
              in {fmtTokens(daily[hover].inputTokens)} · out {fmtTokens(daily[hover].outputTokens)}
            </div>
            <div style={{ color: 'var(--muted)' }}>
              cache r {fmtTokens(daily[hover].cacheReadTokens)} · w {fmtTokens(daily[hover].cacheWriteTokens)}
            </div>
            <div>Msgs: {daily[hover].messages.toLocaleString()}</div>
            <div>Cost: {fmtCost(daily[hover].cost)}</div>
          </div>
        )}
      </div>
    </div>
  );
}
