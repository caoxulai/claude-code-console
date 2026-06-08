import { useState, useEffect } from 'react';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const HOURS = Array.from({ length: 24 }, (_, i) => i);

function intensityColor(value, max) {
  if (!value || !max) return 'var(--surface2)';
  const ratio = value / max;
  if (ratio < 0.25) return 'var(--accent)';
  if (ratio < 0.5) return 'color-mix(in srgb, var(--accent) 70%, var(--text) 30%)';
  if (ratio < 0.75) return 'color-mix(in srgb, var(--accent) 50%, var(--text) 50%)';
  return 'color-mix(in srgb, var(--accent) 30%, var(--text) 70%)';
}

function opacityForValue(value, max) {
  if (!value || !max) return 0.08;
  return 0.2 + (value / max) * 0.8;
}

export default function ActivityHeatmap({ grid }) {
  const [hover, setHover] = useState(null);

  if (!grid || grid.length !== 7) {
    return <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>No activity data.</div>;
  }

  const max = Math.max(...grid.flat(), 1);
  const cellSize = 18;
  const gap = 2;
  const labelW = 32;
  const headerH = 20;

  return (
    <div style={{ overflowX: 'auto' }}>
      <div style={{ position: 'relative', display: 'inline-block' }}>
        {/* Hour labels */}
        <div style={{ display: 'flex', marginLeft: labelW, marginBottom: 2 }}>
          {HOURS.map(h => (
            <div key={h} style={{
              width: cellSize + gap,
              textAlign: 'center',
              fontSize: '0.65em',
              color: 'var(--muted)',
            }}>
              {h % 3 === 0 ? `${h}` : ''}
            </div>
          ))}
        </div>

        {/* Grid rows */}
        {DAYS.map((day, dow) => (
          <div key={day} style={{ display: 'flex', alignItems: 'center', marginBottom: gap }}>
            <div style={{ width: labelW, fontSize: '0.7em', color: 'var(--muted)', textAlign: 'right', paddingRight: 6 }}>
              {day}
            </div>
            {HOURS.map(h => {
              const val = grid[dow][h];
              const isHover = hover && hover.dow === dow && hover.h === h;
              return (
                <div
                  key={h}
                  onMouseEnter={() => setHover({ dow, h, val })}
                  onMouseLeave={() => setHover(null)}
                  style={{
                    width: cellSize,
                    height: cellSize,
                    marginRight: gap,
                    borderRadius: 3,
                    background: 'var(--accent)',
                    opacity: opacityForValue(val, max),
                    border: isHover ? '1px solid var(--text)' : '1px solid transparent',
                    cursor: 'default',
                    transition: 'opacity 0.1s',
                  }}
                  title={`${day} ${h}:00 — ${val} message${val === 1 ? '' : 's'}`}
                />
              );
            })}
          </div>
        ))}

        {/* Legend */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: labelW, marginTop: 6 }}>
          <span style={{ fontSize: '0.65em', color: 'var(--muted)' }}>Less</span>
          {[0, 0.25, 0.5, 0.75, 1].map((ratio, i) => (
            <div key={i} style={{
              width: 12,
              height: 12,
              borderRadius: 2,
              background: 'var(--accent)',
              opacity: 0.08 + ratio * 0.8,
            }} />
          ))}
          <span style={{ fontSize: '0.65em', color: 'var(--muted)' }}>More</span>
        </div>
      </div>

      {hover && (
        <div style={{
          fontSize: '0.78em',
          color: 'var(--text)',
          marginTop: '0.5em',
        }}>
          {DAYS[hover.dow]} {hover.h}:00–{hover.h + 1}:00 — <strong>{hover.val}</strong> message{hover.val === 1 ? '' : 's'}
        </div>
      )}
    </div>
  );
}
