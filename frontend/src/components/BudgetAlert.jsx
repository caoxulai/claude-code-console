import { useState, useEffect } from 'react';
import { FiAlertTriangle, FiSettings, FiX } from 'react-icons/fi';

const STORAGE_KEY = 'claude-web-budget';

function getConfig() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return null;
}

function setConfig(cfg) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
}

export default function BudgetAlert({ dailyCost }) {
  const [config, setLocalConfig] = useState(getConfig);
  const [dismissed, setDismissed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [inputVal, setInputVal] = useState('');

  if (!config && !editing) {
    return null;
  }

  if (dismissed) return null;

  const threshold = config?.dailyLimit;
  const exceeded = threshold && dailyCost >= threshold;
  const nearLimit = threshold && dailyCost >= threshold * 0.8 && !exceeded;

  if (!exceeded && !nearLimit && !editing) return null;

  if (editing) {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '0.5em',
        padding: '0.5em 1em',
        background: 'var(--surface2)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius)',
        marginBottom: '1em',
        fontSize: '0.85em',
      }}>
        <FiSettings size={14} style={{ color: 'var(--accent)' }} />
        <span>Daily budget ($):</span>
        <input
          className="form-input"
          style={{ width: 80, fontSize: '0.9em', padding: '0.2em 0.4em' }}
          value={inputVal}
          onChange={e => setInputVal(e.target.value)}
          placeholder="e.g. 5.00"
          autoFocus
        />
        <button className="btn" style={{ fontSize: '0.8em', padding: '0.2em 0.5em' }} onClick={() => {
          const num = parseFloat(inputVal);
          if (num > 0) {
            const cfg = { dailyLimit: num };
            setConfig(cfg);
            setLocalConfig(cfg);
          }
          setEditing(false);
        }}>Save</button>
        <button className="btn" style={{ fontSize: '0.8em', padding: '0.2em 0.5em' }} onClick={() => {
          localStorage.removeItem(STORAGE_KEY);
          setLocalConfig(null);
          setEditing(false);
        }}>Remove</button>
        <button className="btn" style={{ fontSize: '0.8em', padding: '0.2em 0.5em' }} onClick={() => setEditing(false)}>Cancel</button>
      </div>
    );
  }

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: '0.5em',
      padding: '0.5em 1em',
      background: exceeded ? 'color-mix(in srgb, var(--error) 15%, var(--surface))' : 'color-mix(in srgb, var(--warning) 15%, var(--surface))',
      border: `1px solid ${exceeded ? 'var(--error)' : 'var(--warning)'}`,
      borderRadius: 'var(--radius)',
      marginBottom: '1em',
      fontSize: '0.85em',
    }}>
      <FiAlertTriangle size={14} style={{ color: exceeded ? 'var(--error)' : 'var(--warning)', flexShrink: 0 }} />
      <span style={{ flex: 1 }}>
        {exceeded
          ? `Daily spend ($${dailyCost.toFixed(2)}) exceeds your $${threshold.toFixed(2)} budget`
          : `Daily spend ($${dailyCost.toFixed(2)}) is approaching your $${threshold.toFixed(2)} budget`
        }
      </span>
      <button className="icon-btn" title="Edit budget" onClick={() => { setInputVal(String(threshold)); setEditing(true); }}>
        <FiSettings size={13} />
      </button>
      <button className="icon-btn" title="Dismiss" onClick={() => setDismissed(true)}>
        <FiX size={13} />
      </button>
    </div>
  );
}

export function BudgetSetupButton() {
  const [config, setLocalConfig] = useState(getConfig);
  const [editing, setEditing] = useState(false);
  const [inputVal, setInputVal] = useState('');

  if (config) return null;

  if (editing) {
    return (
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4em' }}>
        <input
          className="form-input"
          style={{ width: 70, fontSize: '0.8em', padding: '0.2em 0.4em' }}
          value={inputVal}
          onChange={e => setInputVal(e.target.value)}
          placeholder="$/day"
          autoFocus
        />
        <button className="btn" style={{ fontSize: '0.75em', padding: '0.2em 0.5em' }} onClick={() => {
          const num = parseFloat(inputVal);
          if (num > 0) {
            const cfg = { dailyLimit: num };
            setConfig(cfg);
            setLocalConfig(cfg);
          }
          setEditing(false);
        }}>Set</button>
        <button className="btn" style={{ fontSize: '0.75em', padding: '0.2em 0.5em' }} onClick={() => setEditing(false)}>×</button>
      </div>
    );
  }

  return (
    <button
      className="btn"
      style={{ fontSize: '0.75em', padding: '0.2em 0.6em' }}
      onClick={() => setEditing(true)}
      title="Set a daily spending alert"
    >
      <FiAlertTriangle size={11} style={{ marginRight: 3 }} />
      Set budget
    </button>
  );
}
