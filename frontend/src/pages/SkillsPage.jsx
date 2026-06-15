import { useEffect, useState, useMemo } from 'react';
import { FiPlus, FiEdit3, FiTrash2, FiSave, FiEye, FiX } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

const FRONTMATTER_TEMPLATE = `---
name: ""
description: ""
tags: []
---

# Skill Name

Describe what this skill does here.
`;

// Well-known multi-phase skills with default phases
const KNOWN_PHASE_SKILLS = {
  'dev-team': ['Design', 'Implement', 'Verify', 'Fix', 'UAT'],
  'power-detective': ['Fan-out', 'Investigate', 'Converge', 'Verify'],
  'deep-research': ['Search', 'Fetch', 'Verify', 'Synthesize'],
};

/**
 * Parse phases from skill content.
 * Priority: frontmatter phases > content headers > known skill defaults
 */
function extractPhases(content, skillName) {
  if (!content) return null;

  // 1. Try frontmatter: look between --- delimiters for 'phases:' field
  const fmMatch = content.match(/^---\s*\n([\s\S]*?)\n---/);
  if (fmMatch) {
    const frontmatter = fmMatch[1];
    const phasesLineIdx = frontmatter.split('\n').findIndex(l => /^phases\s*:/.test(l));
    if (phasesLineIdx !== -1) {
      const lines = frontmatter.split('\n').slice(phasesLineIdx + 1);
      const phases = [];
      for (const line of lines) {
        const itemMatch = line.match(/^\s*-\s+(.+)/);
        if (itemMatch) {
          phases.push(itemMatch[1].trim());
        } else if (line.trim() && !/^\s*-/.test(line)) {
          break; // End of list
        }
      }
      if (phases.length > 0) return phases;
    }
  }

  // 2. Try content body: look for "## Phase: Name" or numbered step headers
  const phaseHeaders = [];
  const phasePattern = /^##\s+(?:Phase|Step)\s*[:-]\s*(.+)/gm;
  let match;
  while ((match = phasePattern.exec(content)) !== null) {
    phaseHeaders.push(match[1].trim());
  }
  if (phaseHeaders.length >= 2) return phaseHeaders;

  // 3. Also try numbered patterns like "## 1. Design" or "## 1 - Design"
  const numberedPattern = /^##\s+\d+[.)]\s*(.+)/gm;
  const numberedPhases = [];
  while ((match = numberedPattern.exec(content)) !== null) {
    numberedPhases.push(match[1].trim());
  }
  if (numberedPhases.length >= 2) return numberedPhases;

  // 4. Fallback to known skill defaults
  const normalizedName = skillName?.toLowerCase?.() || '';
  if (KNOWN_PHASE_SKILLS[normalizedName]) {
    return KNOWN_PHASE_SKILLS[normalizedName];
  }

  return null;
}

/** Visual phase/pipeline diagram component */
function PhaseDiagram({ phases }) {
  if (!phases || phases.length === 0) return null;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'flex-start',
      justifyContent: 'center',
      marginBottom: '1.5em',
      padding: '1em 0.5em',
      background: 'var(--surface2)',
      borderRadius: 'var(--radius)',
      border: '1px solid var(--border)',
      overflowX: 'auto',
    }}>
      {phases.map((phase, idx) => (
        <div key={phase} style={{ display: 'flex', alignItems: 'center' }}>
          {/* Phase node */}
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: '60px' }}>
            <div style={{
              width: '24px',
              height: '24px',
              borderRadius: '50%',
              background: 'var(--accent)',
              border: '2px solid var(--accent)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '0.65em',
              fontWeight: 700,
              color: '#000',
            }}>
              {idx + 1}
            </div>
            <span style={{
              marginTop: '0.4em',
              fontSize: '0.72em',
              color: 'var(--muted)',
              fontWeight: 500,
              textAlign: 'center',
              lineHeight: 1.2,
              maxWidth: '80px',
              wordBreak: 'break-word',
            }}>
              {phase}
            </span>
          </div>
          {/* Connector line */}
          {idx < phases.length - 1 && (
            <div style={{
              width: '32px',
              height: '2px',
              background: 'var(--border)',
              marginTop: '12px',
              flexShrink: 0,
            }} />
          )}
        </div>
      ))}
    </div>
  );
}

/** Renders skill content with phase diagram + syntax-highlighted markdown */
function SkillContentView({ content, skillName }) {
  const phases = useMemo(() => extractPhases(content, skillName), [content, skillName]);

  // Strip frontmatter from displayed content for cleaner rendering
  const displayContent = useMemo(() => {
    if (!content) return '';
    const stripped = content.replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, '');
    return stripped.trim();
  }, [content]);

  return (
    <div style={{ minWidth: 0, overflow: 'hidden' }}>
      <PhaseDiagram phases={phases} />
      <div className="markdown-body" style={{ fontSize: '0.92em', lineHeight: 1.6, overflowWrap: 'break-word', wordBreak: 'break-word' }}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
          {displayContent}
        </ReactMarkdown>
      </div>
    </div>
  );
}

export default function SkillsPage() {
  const [skills, setSkills] = useState([]);
  const [selected, setSelected] = useState(null);
  const [content, setContent] = useState('');
  const [etag, setEtag] = useState(null);
  const [editing, setEditing] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newContent, setNewContent] = useState(FRONTMATTER_TEMPLATE);

  const refresh = () => fetch('/api/skills')
    .then(r => r.json())
    .then(json => setSkills(Array.isArray(json) ? json : []))
    .catch(() => setSkills([]));
  useEffect(() => { refresh(); }, []);
  // Live-sync the skill list when skills change on disk, unless mid-edit/create.
  useLiveUpdates(['skill_changed', 'skill_deleted'], () => {
    if (!editing && !creating) refresh();
  });

  const selectSkill = async (name) => {
    const res = await fetch(`/api/skills/${encodeURIComponent(name)}`);
    const json = await res.json();
    setSelected(name);
    setContent(json.content);
    setEtag(json.etag);
    setEditing(false);
    setConflict(false);
    setCreating(false);
  };

  const saveSkill = async () => {
    const res = await fetch(`/api/skills/${encodeURIComponent(selected)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, etag }),
    });
    if (res.status === 409) {
      setConflict(true);
      return;
    }
    const json = await res.json();
    setEtag(json.etag);
    setEditing(false);
    setConflict(false);
    refresh();
  };

  const createSkill = async () => {
    if (!newName.trim()) return;
    const res = await fetch('/api/skills', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName.trim(), content: newContent }),
    });
    if (res.ok) {
      const trimmedName = newName.trim();
      setCreating(false);
      setNewName('');
      setNewContent(FRONTMATTER_TEMPLATE);
      refresh();
      selectSkill(trimmedName);
    }
  };

  const deleteSkill = async (name) => {
    if (!confirm(`Delete skill "${name}"?`)) return;
    await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (selected === name) { setSelected(null); setContent(''); setEtag(null); }
    refresh();
  };

  const getDescription = (skill) => {
    return skill.description || 'No description';
  };

  return (
    <div>
      <div className="page-header">
        <h2>Skills</h2>
        <button className="btn btn-primary" onClick={() => { setCreating(true); setSelected(null); setEditing(false); }}>
          <FiPlus size={14} /> New Skill
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(250px, 300px) 1fr', gap: '1em', minHeight: '500px' }}>
        {/* Left Panel - Skill List */}
        <div className="card" style={{ overflowY: 'auto', maxHeight: '70vh' }}>
          {skills.length === 0 ? (
            <div className="empty-state"><h3>No skills found</h3><p>Create a new skill to get started.</p></div>
          ) : (
            skills.map(skill => (
              <div
                key={skill.name}
                onClick={() => selectSkill(skill.name)}
                style={{
                  padding: '0.6em 0.75em', cursor: 'pointer', borderRadius: 'var(--radius)',
                  background: selected === skill.name ? 'var(--user-bg)' : 'transparent',
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  marginBottom: '4px',
                }}
              >
                <div style={{ overflow: 'hidden', flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                    <span style={{ fontSize: '0.85em', fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {skill.name}
                    </span>
                    <span className={skill.source === 'local' ? 'badge badge-ok' : 'badge'} style={{ flexShrink: 0 }}>
                      {skill.source}
                    </span>
                  </div>
                  <div style={{ fontSize: '0.72em', color: 'var(--muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: '2px' }}>
                    {getDescription(skill)}
                  </div>
                </div>
                {skill.source === 'local' && (
                  <button className="icon-btn" onClick={e => { e.stopPropagation(); deleteSkill(skill.name); }} title="Delete" style={{ flexShrink: 0, marginLeft: '0.5em' }}>
                    <FiTrash2 size={12} />
                  </button>
                )}
              </div>
            ))
          )}
        </div>

        {/* Right Panel - Skill Content */}
        <div className="card" style={{ minWidth: 0, overflow: 'hidden' }}>
          {creating ? (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75em' }}>
                <span style={{ fontWeight: 600 }}>New Skill</span>
                <button className="icon-btn" onClick={() => setCreating(false)} title="Cancel">
                  <FiX size={16} />
                </button>
              </div>
              <div style={{ marginBottom: '0.75em' }}>
                <input
                  className="form-input"
                  type="text"
                  placeholder="Skill name"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  style={{ width: '100%', marginBottom: '0.75em' }}
                />
                <textarea
                  className="form-textarea"
                  value={newContent}
                  onChange={e => setNewContent(e.target.value)}
                  style={{ minHeight: '350px', fontFamily: 'monospace', fontSize: '0.85em', width: '100%' }}
                />
              </div>
              <button className="btn btn-primary" onClick={createSkill}>
                <FiSave size={14} /> Create
              </button>
            </>
          ) : !selected ? (
            <div className="empty-state"><h3>Select a skill</h3><p>Choose a skill from the list to view its content.</p></div>
          ) : (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75em' }}>
                <span style={{ fontWeight: 600 }}>{selected}</span>
                <div style={{ display: 'flex', gap: '0.5em' }}>
                  {editing ? (
                    <>
                      <button className="btn btn-primary" onClick={saveSkill}><FiSave size={14} /> Save</button>
                      <button className="btn" onClick={() => { setEditing(false); setConflict(false); selectSkill(selected); }}><FiEye size={14} /> View</button>
                    </>
                  ) : (
                    <button className="btn" onClick={() => setEditing(true)}><FiEdit3 size={14} /> Edit</button>
                  )}
                </div>
              </div>
              {conflict && (
                <div className="conflict-banner">
                  <span>Conflict: this skill was modified externally. Reload and retry.</span>
                </div>
              )}
              {editing ? (
                <textarea
                  className="form-textarea"
                  value={content}
                  onChange={e => setContent(e.target.value)}
                  style={{ minHeight: '400px', fontFamily: 'monospace', fontSize: '0.85em', width: '100%' }}
                />
              ) : (
                <SkillContentView content={content} skillName={selected} />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
