import { useState, useRef, useEffect, useCallback } from 'react';
import { FiSend, FiSquare, FiCheckCircle, FiChevronDown, FiChevronRight, FiSave } from 'react-icons/fi';
import { useChat } from '../hooks/useChat';

// Regex that detects the structured scope summary Claude is instructed to emit.
// It MUST only ever be run against Claude's ASSISTANT text — NEVER the user's
// first prompt (which names these markers verbatim as an instruction) and NEVER
// a partial stream fragment. Scanning the prompt echo or mid-stream would
// "save" the instruction sentence the instant the chat opens. See CLARIFY_PROMPT.
const MARKER_RE = /---CLARIFICATION---\n([\s\S]*?)\n---END---/;

// Build the auto-send clarification prompt. Every interpolated field is guarded
// so an empty subject/description/project never prints the literal string
// 'null'/'undefined' (prefill-fidelity rule). The appended marker instruction
// names the markers verbatim so Claude wraps its FINAL summary for auto-save.
function buildClarifyPrompt(task) {
  const subject = task.subject || 'Untitled';
  const description = task.description || '(none)';
  const project = task.project || 'global';
  return (
    `I have a TODO item I'd like to clarify before planning:\n\n` +
    `Subject: ${subject}\n` +
    `Notes: ${description}\n` +
    `Project: ${project}\n\n` +
    `Help me refine this into a clear, actionable scope. Ask me questions to ` +
    `understand constraints, acceptance criteria, and edge cases. When we're ` +
    `aligned on the scope, wrap your FINAL scope summary between a line ` +
    `containing exactly ---CLARIFICATION--- and a line containing exactly ` +
    `---END--- so the system can save it automatically.`
  );
}

/**
 * ClarifyChat — a self-contained inline clarification mini-chat embedded in the
 * TODO detail panel. It auto-sends a clarification prompt once, streams Claude's
 * reply, auto-detects a ---CLARIFICATION---…---END--- summary in the completed
 * assistant text, and saves it back to the task exactly once. A collapsed
 * "Save manually" disclosure is the fallback when Claude never emits markers.
 *
 * ISOLATION: useChat() is called INSIDE this component. useChat holds ALL of its
 * state in per-call useState/useRef (messages/streaming/sessionId + abortRef) —
 * there is no module-level store and no React context — so a fresh useChat() per
 * mount is fully isolated from ChatPage and from a sibling task's mini-chat.
 *
 * Props:
 *   task   — the task object (must NOT have clarification.summary; the panel
 *            branches to read-only before mounting this).
 *   onSave — TasksPage.saveTaskFields(taskId, fields): the shared PUT + refetch.
 */
export default function ClarifyChat({ task, onSave }) {
  const { messages, streaming, sessionId, send, stop } = useChat();

  const [input, setInput] = useState('');
  const [saved, setSaved] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [manualText, setManualText] = useState('');
  const [manualSaving, setManualSaving] = useState(false);

  const scrollRef = useRef(null);
  // Guards the one-shot auto-send. A REF (not state) so it survives the board's
  // constant re-renders (task_changed refetch / 60s poll / focus refetch) AND
  // React StrictMode's double-invoke of effects in dev.
  const sentRef = useRef(false);
  // Guards the one-shot auto-save so a second fenced block (or a re-scan as text
  // accumulates) can never fire a second PUT that overwrites the first summary.
  const savedRef = useRef(false);
  // Tracks the previous streaming value so we can detect the true->false edge
  // (a turn just completed) and scan ONLY then.
  const prevStreamingRef = useRef(false);

  // --- Auto-send the first prompt exactly once ---
  useEffect(() => {
    if (sentRef.current) return;
    sentRef.current = true;
    const prompt = buildClarifyPrompt(task);
    // Omit cwd entirely when the task has no project so the backend uses its
    // default — never pass a literal undefined string (streamChat drops a
    // falsy cwd, so `|| undefined` is correct).
    send(prompt, { cwd: task.projectPath || undefined });
    // Intentionally NOT keyed on `task`/`send` — the ref guard is the source of
    // truth; re-arming on those would risk a duplicate session per re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Auto-detect the structured summary on a completed assistant turn ---
  useEffect(() => {
    const justCompleted = prevStreamingRef.current && !streaming;
    prevStreamingRef.current = streaming;
    if (!justCompleted || savedRef.current) return;

    // Scan ONLY the latest assistant message's text. useChat stores the full
    // cumulative turn text in that message's content, so the completed turn is
    // available here. NEVER scan user messages (the prompt echo names the
    // markers) and NEVER scan mid-stream (this only runs on the completed edge).
    let assistantText = '';
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') {
        assistantText = messages[i].content || '';
        break;
      }
    }
    const match = MARKER_RE.exec(assistantText);
    if (!match) return;

    const summary = match[1].trim();
    if (!summary) return;

    // Gate BEFORE the async save so a re-render can't slip a second save in.
    savedRef.current = true;
    setSaved(true);
    // Spread the task's existing clarification: the backend REPLACES
    // clarification wholesale (tasks.py: t['clarification'] = clarification — no
    // merge), so a bare object would drop sibling keys (e.g. acceptanceCriteria
    // that planPrompt reads). sessionId is THIS mini-chat's session id.
    onSave(task.id, {
      clarification: {
        ...task.clarification,
        summary,
        sessionId,
        resolvedAt: Date.now(),
      },
    });
  }, [streaming, messages, sessionId, task.id, task.clarification, onSave]);

  // --- Auto-scroll the message area to the newest content ---
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  const disabled = streaming || saved;

  const handleSend = useCallback(() => {
    const prompt = input.trim();
    if (!prompt || disabled) return;
    setInput('');
    send(prompt, { cwd: task.projectPath || undefined });
  }, [input, disabled, send, task.projectPath]);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleManualSave = useCallback(async () => {
    const text = manualText.trim();
    if (!text) return;
    setManualSaving(true);
    const ok = await onSave(task.id, {
      clarification: {
        ...task.clarification,
        summary: text,
        sessionId: sessionId || task.clarification?.sessionId,
        resolvedAt: Date.now(),
      },
    });
    setManualSaving(false);
    if (ok) {
      savedRef.current = true;
      setSaved(true);
    }
  }, [manualText, onSave, task.id, task.clarification, sessionId]);

  return (
    <div style={{ marginBottom: 'var(--space-md)' }}>
      <label style={{ display: 'block', fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 'var(--space-xs)', fontWeight: 600 }}>
        Clarification Chat
      </label>

      {/* Success indicator */}
      {saved && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 'var(--space-xs)',
          fontSize: 'var(--fs-sm)', color: 'var(--accent)',
          marginBottom: 'var(--space-sm)', fontWeight: 600,
        }}>
          <FiCheckCircle size={14} /> Clarification saved to TODO
        </div>
      )}

      {/* Scrollable message area (plain text, no markdown) */}
      <div
        ref={scrollRef}
        style={{
          maxHeight: '300px', overflowY: 'auto',
          border: '1px solid var(--border)', borderRadius: 'var(--radius)',
          background: 'var(--card-bg)', padding: 'var(--space-sm)',
          display: 'flex', flexDirection: 'column', gap: 'var(--space-sm)',
        }}
      >
        {messages.length === 0 && !streaming && (
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
            Starting the clarification conversation…
          </div>
        )}
        {messages.map((msg, i) => (
          <ClarifyMessage key={i} message={msg} />
        ))}
        {streaming && (
          <div className="thinking-indicator" style={{ fontSize: 'var(--fs-xs)' }}>
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span style={{ marginLeft: '0.3em' }}>Claude is working</span>
          </div>
        )}
      </div>

      {/* Input row */}
      <div style={{ display: 'flex', gap: 'var(--space-xs)', marginTop: 'var(--space-xs)' }}>
        <textarea
          className="form-textarea"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={saved ? 'Clarification saved — chat is read-only.' : 'Reply to Claude…'}
          rows={1}
          disabled={disabled}
          style={{ minHeight: '38px', maxHeight: '120px', resize: 'none', fontSize: 'var(--fs-sm)', flex: 1 }}
        />
        {streaming ? (
          <button
            type="button"
            className="btn"
            onClick={stop}
            title="Stop generating"
            style={{ fontSize: 'var(--fs-xs)', alignSelf: 'stretch' }}
          >
            <FiSquare size={14} /> Stop
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleSend}
            disabled={!input.trim() || disabled}
            title="Send"
            style={{ fontSize: 'var(--fs-xs)', alignSelf: 'stretch' }}
          >
            <FiSend size={14} />
          </button>
        )}
      </div>

      {/* Manual-save fallback disclosure */}
      <div style={{ marginTop: 'var(--space-sm)' }}>
        <button
          onClick={() => setManualOpen(o => !o)}
          style={{ background: 'none', border: 'none', padding: 0, color: 'var(--accent)', fontSize: '0.8em', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
        >
          {manualOpen ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
          Save manually
        </button>
        {manualOpen && (
          <div style={{ marginTop: 'var(--space-xs)' }}>
            <textarea
              className="form-textarea"
              value={manualText}
              onChange={e => setManualText(e.target.value)}
              placeholder="Paste or type the refined scope yourself, then save…"
              style={{ minHeight: '80px', fontSize: 'var(--fs-sm)' }}
            />
            <button
              className="btn"
              disabled={manualSaving || !manualText.trim()}
              onClick={handleManualSave}
              style={{ marginTop: 'var(--space-xs)', fontSize: 'var(--fs-xs)' }}
            >
              <FiSave size={12} /> Save Clarification
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// A single message rendered as PLAIN TEXT (no markdown) — a labeled user bubble
// vs. assistant text. Tool-call / system / error messages from useChat render as
// a muted note so the mini-chat stays light.
function ClarifyMessage({ message }) {
  const { role, content } = message;

  if (role === 'user') {
    return (
      <div style={{ alignSelf: 'flex-end', maxWidth: '85%' }}>
        <div style={{
          background: 'var(--accent)', color: '#fff',
          borderRadius: 'var(--radius)', padding: 'var(--space-xs) var(--space-sm)',
          fontSize: 'var(--fs-sm)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}>
          {content}
        </div>
      </div>
    );
  }

  if (role === 'assistant') {
    return (
      <div style={{
        fontSize: 'var(--fs-sm)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        color: 'var(--text)', lineHeight: 1.5,
      }}>
        {content}
      </div>
    );
  }

  // tool_use, system, error, auth-error — keep it light: a muted one-liner.
  const note = role === 'tool_use'
    ? `[tool: ${message.toolCall?.name || 'call'}]`
    : (content || '');
  if (!note) return null;
  return (
    <div style={{ fontSize: 'var(--fs-xs)', color: role === 'error' || role === 'auth-error' ? 'var(--danger, #d9534f)' : 'var(--muted)', fontStyle: 'italic' }}>
      {note}
    </div>
  );
}
