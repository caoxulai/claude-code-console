import { useState } from 'react';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const HOURS = Array.from({ length: 24 }, (_, i) => i);

function opacityForValue(value, max) {
  if (!value || !max) return 0.08;
  return 0.2 + (value / max) * 0.8;
}

function toDateStr(y, m, d) {
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function addDays(dateStr, n) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d + n);
  return toDateStr(dt.getFullYear(), dt.getMonth() + 1, dt.getDate());
}

function isoWeekday(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  const day = dt.getDay();
  return day === 0 ? 7 : day;
}

function todayStr() {
  const now = new Date();
  return toDateStr(now.getFullYear(), now.getMonth() + 1, now.getDate());
}

function buildCalendarWeeks(days) {
  const today = todayStr();
  const start = addDays(today, -83);
  const startDow = isoWeekday(start);
  const mondayStart = addDays(start, 1 - startDow);

  const weeks = [];
  let current = mondayStart;
  while (current <= today) {
    const week = [];
    for (let d = 0; d < 7; d++) {
      const dateStr = addDays(current, d);
      week.push({
        date: dateStr,
        dow: d,
        count: days[dateStr] || 0,
        future: dateStr > today,
      });
    }
    weeks.push(week);
    current = addDays(current, 7);
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

function formatDateRange(week) {
  const first = week[0].date;
  const last = week[6].date;
  const [y1, m1, d1] = first.split('-').map(Number);
  const [, m2, d2] = last.split('-').map(Number);
  return `${MONTHS[m1 - 1]} ${d1} – ${MONTHS[m2 - 1]} ${d2}, ${y1}`;
}

const CELL = 18;
const GAP = 3;
const LABEL_W = 50;
const HEADER_H = 16;

function MonthGrid({ weeks, max, monthLabels, hover, setHover, selectedWeekIdx, setSelectedWeekIdx }) {
  return (
    <div style={{ display: 'inline-block' }}>
      <div style={{ display: 'flex', marginLeft: LABEL_W, marginBottom: GAP, height: HEADER_H, alignItems: 'flex-end' }}>
        {weeks.map((_, w) => {
          const label = monthLabels.find(l => l.weekIdx === w);
          return (
            <div key={w} style={{
              width: CELL + GAP,
              fontSize: '0.6em',
              color: 'var(--muted)',
              fontWeight: 500,
              lineHeight: 1,
            }}>
              {label ? label.label : ''}
            </div>
          );
        })}
      </div>

      {DAYS.map((day, dow) => (
        <div key={day} style={{ display: 'flex', alignItems: 'center', marginBottom: GAP }}>
          <div style={{ width: LABEL_W, fontSize: '0.6em', color: 'var(--muted)', textAlign: 'right', paddingRight: 6, lineHeight: 1, whiteSpace: 'nowrap' }}>
            {dow % 2 === 0 ? day : ''}
          </div>
          {weeks.map((week, w) => {
            const cell = week[dow];
            if (!cell || cell.future) {
              return <div key={w} style={{ width: CELL, height: CELL, marginRight: GAP }} />;
            }
            const isHover = hover?.date === cell.date;
            const isSelected = selectedWeekIdx === w;
            return (
              <div
                key={w}
                onMouseEnter={() => setHover(cell)}
                onMouseLeave={() => setHover(null)}
                onClick={() => setSelectedWeekIdx(selectedWeekIdx === w ? null : w)}
                style={{
                  width: CELL,
                  height: CELL,
                  marginRight: GAP,
                  borderRadius: 2,
                  background: 'var(--accent)',
                  opacity: isSelected
                    ? Math.max(opacityForValue(cell.count, max), 0.35)
                    : opacityForValue(cell.count, max),
                  border: isHover
                    ? '1px solid var(--text)'
                    : isSelected
                      ? '1px solid color-mix(in srgb, var(--accent) 80%, var(--text) 20%)'
                      : '1px solid transparent',
                  cursor: 'pointer',
                  transition: 'opacity 0.15s, border-color 0.15s',
                }}
                title={`${cell.date} (${DAYS[cell.dow]}) — ${cell.count} message${cell.count === 1 ? '' : 's'}`}
              />
            );
          })}
        </div>
      ))}
    </div>
  );
}

function WeekGrid({ week, dailyHours }) {
  const [hover, setHover] = useState(null);

  let max = 1;
  for (const day of week) {
    if (day.future) continue;
    const hours = dailyHours[day.date] || [];
    for (const v of hours) {
      if (v > max) max = v;
    }
  }

  return (
    <div style={{ display: 'inline-block' }}>
      <div style={{ display: 'flex', marginLeft: LABEL_W, marginBottom: GAP, height: HEADER_H, alignItems: 'flex-end' }}>
        {HOURS.map(h => (
          <div key={h} style={{
            width: CELL + GAP,
            textAlign: 'center',
            fontSize: '0.6em',
            color: 'var(--muted)',
            fontWeight: 500,
            lineHeight: 1,
          }}>
            {h % 3 === 0 ? `${h}` : ''}
          </div>
        ))}
      </div>

      {week.map((day) => {
        if (day.future) return null;
        const hours = dailyHours[day.date] || Array(24).fill(0);
        const [, m, d] = day.date.split('-').map(Number);
        return (
          <div key={day.date} style={{ display: 'flex', alignItems: 'center', marginBottom: GAP }}>
            <div style={{ width: LABEL_W, fontSize: '0.6em', color: 'var(--muted)', textAlign: 'right', paddingRight: 6, lineHeight: 1, whiteSpace: 'nowrap' }}>
              {DAYS[day.dow]} {m}/{d}
            </div>
            {HOURS.map(h => {
              const val = hours[h];
              const isHover = hover?.date === day.date && hover?.h === h;
              return (
                <div
                  key={h}
                  onMouseEnter={() => setHover({ date: day.date, dow: day.dow, h, val })}
                  onMouseLeave={() => setHover(null)}
                  style={{
                    width: CELL,
                    height: CELL,
                    marginRight: GAP,
                    borderRadius: 2,
                    background: 'var(--accent)',
                    opacity: opacityForValue(val, max),
                    border: isHover ? '1px solid var(--text)' : '1px solid transparent',
                    cursor: 'default',
                    transition: 'opacity 0.15s',
                  }}
                  title={`${day.date} ${h}:00 — ${val} message${val === 1 ? '' : 's'}`}
                />
              );
            })}
          </div>
        );
      })}

      {/* Fixed-height hover row */}
      <div style={{ marginLeft: LABEL_W, marginTop: 4, fontSize: '0.65em', color: 'var(--text)', height: 14, lineHeight: '14px' }}>
        {hover ? (
          <>{hover.date} {hover.h}:00 — <strong>{hover.val}</strong> msg{hover.val === 1 ? '' : 's'}</>
        ) : (
          <span style={{ color: 'var(--muted)' }}>Hover for details</span>
        )}
      </div>
    </div>
  );
}

export default function ActivityHeatmap({ grid, days, dailyHours }) {
  // All hooks must run before any early return (Rules of Hooks): if `days`
  // ever flips between empty and populated on a mounted instance, a hook after
  // the early return would change the hook count and crash the subtree.
  const [monthHover, setMonthHover] = useState(null);
  const [selectedWeekIdx, setSelectedWeekIdx] = useState(null);

  if (!days || Object.keys(days).length === 0) {
    return <div style={{ color: 'var(--muted)', fontSize: '0.85em' }}>No activity data.</div>;
  }

  const weeks = buildCalendarWeeks(days);
  const allCounts = weeks.flat().map(c => c.count);
  const max = Math.max(...allCounts, 1);
  const monthLabels = getMonthLabels(weeks);

  // Default to the latest week until the user picks one (null → last week).
  const effectiveWeekIdx = selectedWeekIdx ?? weeks.length - 1;
  const selectedWeek = weeks[effectiveWeekIdx];
  const weekTotal = selectedWeek ? selectedWeek.reduce((s, d) => s + d.count, 0) : 0;

  return (
    <div>
      {/* Two-panel layout: 50/50 grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr auto 1fr',
        alignItems: 'start',
      }}>
        {/* Left: Month overview */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          <div style={{ textAlign: 'center', marginBottom: 10, height: 32, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
            <div style={{ fontSize: 'var(--fs-xs)', fontWeight: 600, color: 'var(--text)' }}>12-week overview</div>
            <div style={{ fontSize: '0.62em', color: 'var(--muted)', marginTop: 1, height: 13, lineHeight: '13px' }}>
              {monthHover
                ? <>{monthHover.date} ({DAYS[monthHover.dow]}) — {monthHover.count} msgs</>
                : 'Click a column to drill down'}
            </div>
          </div>
          <MonthGrid
            weeks={weeks}
            max={max}
            monthLabels={monthLabels}
            hover={monthHover}
            setHover={setMonthHover}
            selectedWeekIdx={effectiveWeekIdx}
            setSelectedWeekIdx={setSelectedWeekIdx}
          />
        </div>

        {/* Divider */}
        <div style={{ width: 1, alignSelf: 'stretch', background: 'var(--border)', margin: '0 var(--space-md)' }} />

        {/* Right: Week hourly view */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', overflow: 'hidden' }}>
          <div style={{ textAlign: 'center', marginBottom: 10, height: 32, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
            <div style={{ fontSize: 'var(--fs-xs)', fontWeight: 600, color: 'var(--text)' }}>
              {selectedWeek ? formatDateRange(selectedWeek) : 'Select a week'}
            </div>
            <div style={{ fontSize: '0.62em', color: 'var(--muted)', marginTop: 1 }}>
              {weekTotal.toLocaleString()} messages · hourly breakdown
            </div>
          </div>
          <div style={{ overflowX: 'auto', maxWidth: '100%' }}>
            {selectedWeek && (
              <WeekGrid week={selectedWeek} dailyHours={dailyHours || {}} />
            )}
          </div>
        </div>
      </div>

      {/* Legend */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 5,
        marginTop: 'var(--space-md)',
        paddingTop: 'var(--space-sm)',
        borderTop: '1px solid var(--border)',
      }}>
        <span style={{ fontSize: '0.6em', color: 'var(--muted)' }}>Less</span>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio, i) => (
          <div key={i} style={{
            width: 10,
            height: 10,
            borderRadius: 2,
            background: 'var(--accent)',
            opacity: 0.08 + ratio * 0.8,
          }} />
        ))}
        <span style={{ fontSize: '0.6em', color: 'var(--muted)' }}>More</span>
        <span style={{ fontSize: '0.6em', color: 'var(--muted)', marginLeft: 12 }}>·</span>
        <span style={{ fontSize: '0.6em', color: 'var(--muted)' }}>All times Pacific (Seattle)</span>
      </div>
    </div>
  );
}
