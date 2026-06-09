import { useState } from 'react';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function opacityForValue(value, max) {
  if (!value || !max) return 0.08;
  return 0.2 + (value / max) * 0.8;
}

function buildCalendarWeeks(days) {
  const today = new Date();
  const weeks = [];
  const start = new Date(today);
  start.setDate(start.getDate() - 83); // ~12 weeks back
  // Align to Monday
  const dayOfWeek = start.getDay();
  const mondayOffset = dayOfWeek === 0 ? -6 : 1 - dayOfWeek;
  start.setDate(start.getDate() + mondayOffset);

  let current = new Date(start);
  while (current <= today) {
    const week = [];
    for (let d = 0; d < 7; d++) {
      const dateStr = current.toISOString().slice(0, 10);
      week.push({
        date: dateStr,
        dow: d,
        count: days[dateStr] || 0,
        future: current > today,
      });
      current.setDate(current.getDate() + 1);
    }
    weeks.push(week);
  }
  return weeks;
}

function getMonthLabels(weeks) {
  const labels = [];
  let lastMonth = null;
  for (let w = 0; w < weeks.length; w++) {
    const firstDay = weeks[w][0];
    const month = parseInt(firstDay.date.split('-')[1], 10) - 1;
    if (month !== lastMonth) {
      labels.push({ weekIdx: w, label: MONTHS[month] });
      lastMonth = month;
    }
  }
  return labels;
}

export default function ActivityHeatmap({ grid, days }) {
  const [hover, setHover] = useState(null);

  if (!days || Object.keys(days).length === 0) {
    return <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>No activity data.</div>;
  }

  const weeks = buildCalendarWeeks(days);
  const allCounts = weeks.flat().map(c => c.count);
  const max = Math.max(...allCounts, 1);
  const monthLabels = getMonthLabels(weeks);

  const cellSize = 14;
  const gap = 2;
  const labelW = 28;
  const headerH = 16;

  return (
    <div style={{ overflowX: 'auto' }}>
      <div style={{ position: 'relative', display: 'inline-block' }}>
        {/* Month labels */}
        <div style={{ display: 'flex', marginLeft: labelW, marginBottom: 2, height: headerH }}>
          {weeks.map((_, w) => {
            const label = monthLabels.find(l => l.weekIdx === w);
            return (
              <div key={w} style={{
                width: cellSize + gap,
                fontSize: '0.62em',
                color: 'var(--muted)',
                fontWeight: 500,
              }}>
                {label ? label.label : ''}
              </div>
            );
          })}
        </div>

        {/* Grid rows (Mon–Sun) */}
        {DAYS.map((day, dow) => (
          <div key={day} style={{ display: 'flex', alignItems: 'center', marginBottom: gap }}>
            <div style={{ width: labelW, fontSize: '0.62em', color: 'var(--muted)', textAlign: 'right', paddingRight: 4 }}>
              {dow % 2 === 0 ? day : ''}
            </div>
            {weeks.map((week, w) => {
              const cell = week[dow];
              if (!cell || cell.future) {
                return <div key={w} style={{ width: cellSize, height: cellSize, marginRight: gap }} />;
              }
              const isHover = hover?.date === cell.date;
              return (
                <div
                  key={w}
                  onMouseEnter={() => setHover(cell)}
                  onMouseLeave={() => setHover(null)}
                  style={{
                    width: cellSize,
                    height: cellSize,
                    marginRight: gap,
                    borderRadius: 2,
                    background: 'var(--accent)',
                    opacity: opacityForValue(cell.count, max),
                    border: isHover ? '1px solid var(--text)' : '1px solid transparent',
                    cursor: 'default',
                    transition: 'opacity 0.1s',
                  }}
                  title={`${cell.date} — ${cell.count} message${cell.count === 1 ? '' : 's'}`}
                />
              );
            })}
          </div>
        ))}

        {/* Footer: legend + hover detail */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginLeft: labelW, marginTop: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ fontSize: '0.62em', color: 'var(--muted)' }}>Less</span>
            {[0, 0.25, 0.5, 0.75, 1].map((ratio, i) => (
              <div key={i} style={{
                width: 10,
                height: 10,
                borderRadius: 2,
                background: 'var(--accent)',
                opacity: 0.08 + ratio * 0.8,
              }} />
            ))}
            <span style={{ fontSize: '0.62em', color: 'var(--muted)' }}>More</span>
          </div>
          {hover && (
            <div style={{ fontSize: '0.72em', color: 'var(--text)' }}>
              {hover.date} — <strong>{hover.count}</strong> message{hover.count === 1 ? '' : 's'}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
