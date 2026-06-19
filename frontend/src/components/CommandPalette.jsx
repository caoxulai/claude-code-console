import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { FiSearch, FiMessageSquare, FiBookOpen, FiZap, FiFolder, FiServer, FiClock, FiSettings, FiBarChart2, FiList, FiPackage, FiGitBranch, FiHome, FiCheckSquare, FiUsers, FiSlack, FiCalendar } from 'react-icons/fi';

const STATIC_ITEMS = [
  { id: 'nav-dashboard', label: 'Dashboard', icon: FiHome, path: '/', section: 'Pages' },
  { id: 'nav-usage', label: 'Usage', icon: FiBarChart2, path: '/usage', section: 'Pages' },
  { id: 'nav-slack', label: 'Slack', icon: FiSlack, path: '/slack', section: 'Pages' },
  { id: 'nav-projects', label: 'Projects', icon: FiFolder, path: '/projects', section: 'Pages' },
  { id: 'nav-chat', label: 'Chat', icon: FiMessageSquare, path: '/chat', section: 'Pages' },
  { id: 'nav-sessions', label: 'Sessions', icon: FiList, path: '/sessions', section: 'Pages' },
  { id: 'nav-tasks', label: 'Tasks', icon: FiCheckSquare, path: '/tasks', section: 'Pages' },
  { id: 'nav-agents', label: 'Agents', icon: FiUsers, path: '/agents', section: 'Pages' },
  { id: 'nav-skills', label: 'Skills', icon: FiZap, path: '/skills', section: 'Pages' },
  { id: 'nav-memory', label: 'Memory', icon: FiBookOpen, path: '/memory', section: 'Pages' },
  { id: 'nav-hooks', label: 'Hooks', icon: FiGitBranch, path: '/hooks', section: 'Pages' },
  { id: 'nav-mcp', label: 'MCP Servers', icon: FiServer, path: '/mcp', section: 'Pages' },
  { id: 'nav-plugins', label: 'Plugins', icon: FiPackage, path: '/plugins', section: 'Pages' },
  { id: 'nav-crons', label: 'Scheduled Jobs', icon: FiClock, path: '/crons', section: 'Pages' },
  { id: 'nav-system-cron', label: 'OS Crons', icon: FiCalendar, path: '/system-cron', section: 'Pages' },
  { id: 'nav-settings', label: 'Settings', icon: FiSettings, path: '/settings', section: 'Pages' },
];

function fuzzyMatch(query, text) {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (t.includes(q)) return true;
  let qi = 0;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) qi++;
  }
  return qi === q.length;
}

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  const [dynamicItems, setDynamicItems] = useState([]);
  const inputRef = useRef(null);
  const navigate = useNavigate();

  useEffect(() => {
    const handler = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setOpen(prev => !prev);
      }
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  useEffect(() => {
    if (open) {
      setQuery('');
      setSelected(0);
      setTimeout(() => inputRef.current?.focus(), 50);
      // Fetch dynamic items (sessions + memory + projects)
      Promise.all([
        fetch('/api/sessions?limit=20').then(r => r.json()).catch(() => ({})),
        fetch('/api/memory/files').then(r => r.json()).catch(() => []),
        fetch('/api/projects').then(r => r.json()).catch(() => []),
      ]).then(([sessData, memFiles, projects]) => {
        const items = [];
        (sessData.sessions || []).forEach(s => {
          items.push({
            id: `session-${s.id}`,
            label: s.title || s.id,
            icon: FiMessageSquare,
            path: `/sessions`,
            action: () => navigate('/sessions'),
            section: 'Sessions',
          });
        });
        (memFiles || []).forEach(f => {
          const name = typeof f === 'string' ? f : f.name || '';
          if (!name) return;
          items.push({
            id: `memory-${name}`,
            label: name,
            icon: FiBookOpen,
            path: `/memory`,
            section: 'Memory',
          });
        });
        (projects || []).forEach(p => {
          items.push({
            id: `project-${p.id || p.path}`,
            label: p.name || p.path,
            icon: FiFolder,
            path: `/projects?expand=${encodeURIComponent(p.id)}`,
            section: 'Projects',
          });
        });
        setDynamicItems(items);
      });
    }
  }, [open, navigate]);

  const allItems = [...STATIC_ITEMS, ...dynamicItems];
  const filtered = query
    ? allItems.filter(item => fuzzyMatch(query, item.label))
    : allItems;

  const handleSelect = useCallback((item) => {
    setOpen(false);
    if (item.action) {
      item.action();
    } else {
      navigate(item.path);
    }
  }, [navigate]);

  const listRef = useRef(null);

  const handleKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelected(prev => {
        const next = Math.min(prev + 1, filtered.length - 1);
        listRef.current?.children[next + (next > 0 ? Math.ceil(next / 5) : 0)]?.scrollIntoView?.({ block: 'nearest' });
        return next;
      });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelected(prev => {
        const next = Math.max(prev - 1, 0);
        return next;
      });
    } else if (e.key === 'Enter' && filtered[selected]) {
      handleSelect(filtered[selected]);
    }
  };

  if (!open) return null;

  const sections = [];
  let lastSection = null;
  filtered.forEach((item, i) => {
    if (item.section !== lastSection) {
      sections.push({ type: 'header', label: item.section, index: i });
      lastSection = item.section;
    }
    sections.push({ type: 'item', item, index: i });
  });

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 9999,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        paddingTop: '15vh',
        background: 'rgba(0,0,0,0.5)',
        backdropFilter: 'blur(2px)',
      }}
      onClick={() => setOpen(false)}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: 520,
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          boxShadow: '0 16px 48px rgba(0,0,0,0.4)',
          overflow: 'hidden',
        }}
      >
        {/* Search input */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '0.75em 1em', borderBottom: '1px solid var(--border)' }}>
          <FiSearch size={16} style={{ color: 'var(--muted)', marginRight: '0.5em', flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={query}
            onChange={e => { setQuery(e.target.value); setSelected(0); }}
            onKeyDown={handleKeyDown}
            placeholder="Search pages, sessions, projects, memory..."
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--text)',
              fontSize: '0.95em',
            }}
          />
          <kbd style={{
            fontSize: '0.7em',
            color: 'var(--muted)',
            background: 'var(--surface2)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            padding: '2px 6px',
          }}>Esc</kbd>
        </div>

        {/* Results */}
        <div ref={listRef} style={{ maxHeight: 360, overflowY: 'auto', padding: '0.5em 0' }}>
          {filtered.length === 0 && (
            <div style={{ padding: '1em', textAlign: 'center', color: 'var(--muted)', fontSize: '0.85em' }}>
              No results for "{query}"
            </div>
          )}
          {sections.map((entry, i) => {
            if (entry.type === 'header') {
              return (
                <div key={`h-${entry.label}`} style={{
                  padding: '0.4em 1em 0.2em',
                  fontSize: '0.7em',
                  fontWeight: 600,
                  color: 'var(--muted)',
                  textTransform: 'uppercase',
                  letterSpacing: '0.05em',
                }}>
                  {entry.label}
                </div>
              );
            }
            const { item, index } = entry;
            const Icon = item.icon;
            const isSelected = index === selected;
            return (
              <div
                key={item.id}
                onClick={() => handleSelect(item)}
                onMouseEnter={() => setSelected(index)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.6em',
                  padding: '0.5em 1em',
                  cursor: 'pointer',
                  background: isSelected ? 'var(--surface2)' : 'transparent',
                  color: isSelected ? 'var(--text)' : 'var(--muted)',
                  fontSize: '0.88em',
                }}
              >
                <Icon size={14} style={{ flexShrink: 0, color: isSelected ? 'var(--accent)' : 'var(--muted)' }} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {item.label}
                </span>
              </div>
            );
          })}
        </div>

        {/* Footer hint */}
        <div style={{
          padding: '0.5em 1em',
          borderTop: '1px solid var(--border)',
          display: 'flex',
          gap: '1em',
          fontSize: '0.68em',
          color: 'var(--muted)',
        }}>
          <span><kbd style={{ padding: '1px 4px', borderRadius: 3, border: '1px solid var(--border)', background: 'var(--surface2)' }}>↑↓</kbd> navigate</span>
          <span><kbd style={{ padding: '1px 4px', borderRadius: 3, border: '1px solid var(--border)', background: 'var(--surface2)' }}>↵</kbd> open</span>
          <span><kbd style={{ padding: '1px 4px', borderRadius: 3, border: '1px solid var(--border)', background: 'var(--surface2)' }}>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
