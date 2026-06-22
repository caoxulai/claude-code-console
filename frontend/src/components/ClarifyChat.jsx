import { useState, useRef, useEffect, useCallback } from 'react';
import { FiSend, FiSquare, FiCheckCircle, FiChevronDown, FiChevronRight, FiSave } from 'react-icons/fi';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { useChat } from '../hooks/useChat';
import { parseTranscriptToMessages } from '../utils/transcript';

// Regex that detects the structured scope summary Claude is instructed to emit.
// It MUST only ever be run against Claude's ASSISTANT text — NEVER the user's
// first prompt (which names these markers verbatim as an instruction) and NEVER
// a partial stream fragment. Scanning the prompt echo or mid-stream would
// "save" the instruction sentence the instant the chat opens. See CLARIFY_PROMPT.
const MARKER_RE = /---CLARIFICATION---\n([\s\S]*?)\n---END---/;

// DISPLAY-ONLY: strip the two verbatim marker fence lines from the LIVE assistant
// turn before rendering it as markdown. The fences are an internal machine
// instruction (so trySaveSummary can extract match[1]) — never content the user
// needs to see. Crucially, under GFM a line of three-or-more dashes renders as a
// thematic break <hr>, and a non-empty line immediately followed by `---` becomes
// a Setext heading, so leaving the fences in would inject stray <hr>/heading
// artifacts. This NEVER touches MARKER_RE or trySaveSummary (the save path keeps
// matching the verbatim markers); it only cleans the text passed to ReactMarkdown.
function stripClarifyMarkers(text) {
  return (text || '')
    .replace(/^---CLARIFICATION---$\n?/m, '')
    .replace(/^---END---$\n?/m, '');
}

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
 * TODO detail panel. On mount it picks EXACTLY ONE branch: if the task already
 * carries a clarification.sessionId it RESUMES that backend session and reloads
 * the transcript from disk (so switching nav tabs mid-conversation no longer
 * restarts a fresh session); otherwise it auto-sends the clarification prompt
 * once. It streams Claude's reply, auto-detects a ---CLARIFICATION---…---END---
 * summary in the completed assistant text (and re-scans once on resume, since a
 * pure resume has no streaming true->false edge), and saves it back to the task
 * exactly once. A collapsed "Save manually" disclosure is the fallback when
 * Claude never emits markers.
 *
 * ISOLATION: useChat() is called INSIDE this component. useChat holds ALL of its
 * state in per-call useState/useRef (messages/streaming/sessionId + abortRef) —
 * there is no module-level store and no React context — so a fresh useChat() per
 * mount is fully isolated from ChatPage and from a sibling task's mini-chat.
 *
 * THREE DISTINCT GUARDS (must never mask one another):
 *   initRef    — gates the ONE mount branch (resume-reload OR auto-send), so
 *                neither double-fires under React.StrictMode.
 *   savedIdRef — gates the early one-time id-only PUT that persists sessionId
 *                the instant the session starts (does NOT lock the input).
 *   savedRef   — gates the summary save exactly once (locks the input).
 *
 * Props:
 *   task   — the task object (must NOT have clarification.summary; the panel
 *            branches to read-only before mounting this).
 *   onSave — TasksPage.saveTaskFields(taskId, fields): the shared PUT + refetch.
 */
export default function ClarifyChat({ task, onSave }) {
  const { messages, streaming, sessionId, send, stop, resume, setMessages } = useChat();

  const [input, setInput] = useState('');
  const [saved, setSaved] = useState(false);
  // True once the mount effect chose the RESUME branch — used so a still-partial
  // reloaded history is shown as-is instead of the "Starting…" empty-state.
  const [resumed, setResumed] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [manualText, setManualText] = useState('');
  const [manualSaving, setManualSaving] = useState(false);

  const scrollRef = useRef(null);
  // Guards the ONE mount branch (resume-reload OR auto-send) exactly once. A REF
  // (not state) so it survives the board's constant re-renders (task_changed
  // refetch / 60s poll / focus refetch) AND React StrictMode's double-invoke of
  // effects in dev. Replaces the old sentRef — neither branch may double-fire.
  const initRef = useRef(false);
  // Guards the EARLY one-time id-only PUT that persists clarification.sessionId
  // the instant the session starts. DISTINCT from savedRef: it must NOT lock the
  // input or it would pre-empt the later summary save.
  const savedIdRef = useRef(false);
  // Guards the one-shot summary auto-save so a second fenced block (or a re-scan
  // as text accumulates) can never fire a second PUT that overwrites the first
  // summary.
  const savedRef = useRef(false);
  // Tracks the previous streaming value so we can detect the true->false edge
  // (a turn just completed) and scan ONLY then.
  const prevStreamingRef = useRef(false);

  // Scan the latest assistant text for a complete ---CLARIFICATION---…---END---
  // block and, if found and not already saved, fire the one-shot summary save.
  // Shared by the streaming true->false edge AND the post-resume re-scan so the
  // two paths can never drift. `msgs` is passed explicitly because the resume
  // path scans the freshly-reloaded history (not the closure's stale messages).
  // `id` is the session id to persist (the live sessionId or the resumed id).
  const trySaveSummary = useCallback((msgs, id) => {
    if (savedRef.current) return;
    // Scan ONLY the latest assistant message's text. NEVER scan user messages
    // (the prompt echo names the markers verbatim).
    let assistantText = '';
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant') {
        assistantText = msgs[i].content || '';
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
    // that planPrompt reads).
    onSave(task.id, {
      clarification: {
        ...task.clarification,
        summary,
        sessionId: id || sessionId || task.clarification?.sessionId,
        resolvedAt: Date.now(),
      },
    });
  }, [onSave, task.id, task.clarification, sessionId]);

  // --- Mount branch: resume + reload OR auto-send, exactly once ---
  // Picks EXACTLY ONE branch from the task snapshot captured at mount, guarded
  // by initRef so neither double-fires under React.StrictMode.
  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;

    const resumeId = task.clarification?.sessionId;
    if (resumeId) {
      // RESUME branch: re-attach useChat to the existing backend session and
      // reload its transcript from disk. Do NOT auto-send. Mirrors ChatPage.
      resume(resumeId);
      setResumed(true);
      // RESUME-WARM (B3): a session reaped after the 30-min TTL means the first
      // real send pays a full `claude --resume` cold boot synchronously in front
      // of the user. Fire a best-effort pre-warm NOW so that boot overlaps with
      // the user reading the reloaded transcript; by the time they reply the
      // backend has a warm process to claim. Fire-and-forget: a failed/absent
      // endpoint is silent (.catch), it never blocks the UI, and it sets NONE of
      // the save guards. Guarded once by the same initRef-gated mount effect.
      fetch('/api/chat/prewarm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resume: resumeId, cwd: task.projectPath || undefined }),
      }).catch(() => {});
      fetch(`/api/sessions/${encodeURIComponent(resumeId)}/transcript?limit=500&offset=0&tail=true`)
        .then(r => r.json())
        .then(data => {
          const history = parseTranscriptToMessages(data.messages || []);
          // Tolerate an empty/short transcript gracefully (flush lag): never
          // blank an existing view — only set when we actually have content.
          if (history.length === 0) return;
          setMessages(history);
          // POST-RESUME SUMMARY SCAN: a pure resume starts streaming=false with
          // no true->false edge, so the streaming-edge effect never fires. If
          // Claude emitted a complete block WHILE THE USER WAS AWAY it would be
          // reloaded but never saved, stranding the task in 'clarifying' — so
          // re-scan the reloaded history here.
          trySaveSummary(history, resumeId);
        })
        .catch(() => {
          // Transcript fetch failed: the branch was already chosen (resume), so
          // do NOT fall through to auto-send and do NOT blank the view. The next
          // send (re-attaching via resume:sessionId) or a session refetch fills
          // it in.
        });
    } else {
      // AUTO-SEND branch: no session yet — start one. Omit cwd entirely when the
      // task has no project so the backend uses its default (streamChat drops a
      // falsy cwd, so `|| undefined` is correct).
      const prompt = buildClarifyPrompt(task);
      send(prompt, { cwd: task.projectPath || undefined });
    }
    // Ref-guarded one-shot: intentionally deps [] so a re-render can't re-arm it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Persist clarification.sessionId early, exactly once ---
  // Fires the moment useChat surfaces a non-null sessionId on the AUTO-SEND
  // branch — so a nav-tab switch mid-stream leaves a recoverable id on disk.
  // Skips when the id already equals the persisted one (the RESUME branch, where
  // it would be a redundant write). Guarded by savedIdRef (DISTINCT from
  // savedRef) so it does NOT lock the input or pre-empt the later summary save.
  // Reads the LATEST task.clarification (a current prop, in the deps) and spreads
  // it — the backend REPLACES clarification wholesale, so a bare {sessionId}
  // would clobber sibling keys (acceptanceCriteria/summary). No-blind-overwrite.
  useEffect(() => {
    if (!sessionId || savedIdRef.current) return;
    if (sessionId === task.clarification?.sessionId) {
      // Already persisted (resumed session) — mark done, skip the redundant PUT.
      savedIdRef.current = true;
      return;
    }
    savedIdRef.current = true;
    onSave(task.id, {
      clarification: { ...task.clarification, sessionId },
    });
  }, [sessionId, onSave, task.id, task.clarification]);

  // --- Auto-detect the structured summary on a completed assistant turn ---
  // Fires on the live streaming true->false edge (a turn just finished) for both
  // the initial turn and the next send after a resume. The post-resume re-scan
  // (above) covers the no-edge case.
  useEffect(() => {
    const justCompleted = prevStreamingRef.current && !streaming;
    prevStreamingRef.current = streaming;
    if (!justCompleted) return;
    trySaveSummary(messages, sessionId);
  }, [streaming, messages, sessionId, trySaveSummary]);

  // --- Auto-scroll the message area to the newest content ---
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  const disabled = streaming || saved;

  // HONEST COLD-START COPY (B4/AC-8): the ~2-9s spawn/boot window before the
  // first token must read as progress, not a frozen panel, and must NEVER claim
  // completion before real text arrives. While streaming with no assistant text
  // yet (the subprocess is still spawning / Claude hasn't emitted a token) show
  // "Starting Claude…"; the indicator flips to "Claude is working" only once
  // real assistant content has actually arrived. Derive (not state) from the
  // current messages so it can never assert "done" ahead of the stream.
  const hasAssistantText = messages.some(m => m.role === 'assistant' && (m.content || '').trim());
  const workingLabel = (streaming && !hasAssistantText) ? 'Starting Claude…' : 'Claude is working';

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

      {/* Scrollable message area (assistant turns render markdown; user/notes stay plain) */}
      <div
        ref={scrollRef}
        style={{
          maxHeight: '300px', overflowY: 'auto',
          border: '1px solid var(--border)', borderRadius: 'var(--radius)',
          background: 'var(--card-bg)', padding: 'var(--space-sm)',
          display: 'flex', flexDirection: 'column', gap: 'var(--space-sm)',
        }}
      >
        {messages.length === 0 && !streaming && !resumed && (
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
            Starting the clarification conversation…
          </div>
        )}
        {messages.length === 0 && !streaming && resumed && (
          // RESUME window: transcript fetch + first reply. A STATIC line reads as
          // hung, so reuse the streaming thinking-indicator spinner — it's honest
          // (work is genuinely in flight: reloading the conversation) and never
          // claims completion.
          <div className="thinking-indicator" style={{ fontSize: 'var(--fs-xs)' }}>
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span style={{ marginLeft: '0.3em' }}>Resuming the clarification conversation…</span>
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
            <span style={{ marginLeft: '0.3em' }}>{workingLabel}</span>
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

// A single message. The ASSISTANT turn renders as markdown via the canonical
// chat stack (chat-msg-content markdown-body + ReactMarkdown/remarkGfm/
// rehypeHighlight, mirroring MessageBubble) so it matches the main ChatPage; the
// user bubble and the tool-call / system / error notes stay PLAIN (user-typed
// text must render literally, and the auto-sent first prompt names the marker
// fences verbatim — rendering it as markdown would inject <hr>/heading artifacts).
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
    // Strip the verbatim marker fences for DISPLAY ONLY (the save path still
    // matches them via MARKER_RE) so the dashes never render as <hr>/headings.
    const displayContent = stripClarifyMarkers(content);
    return (
      <div className="chat-msg-content markdown-body" style={{ fontSize: 'var(--fs-sm)', wordBreak: 'break-word' }}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
          {displayContent}
        </ReactMarkdown>
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
