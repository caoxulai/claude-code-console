import { useState, useRef, useEffect } from 'react';
import { useSearchParams, useLocation } from 'react-router-dom';
import { useChat } from '../hooks/useChat';
import { useConfigStore } from '../stores/configStore';
import { MessageBubble } from '../components/MessageBubble';
import { ToolCallPanel } from '../components/ToolCallPanel';
import { ErrorBoundary } from '../components/ErrorBoundary';
import SlashCommandMenu from '../components/SlashCommandMenu';
import { parseTranscriptToMessages } from '../utils/transcript';
import { FiSend, FiPlus, FiEdit3, FiCheck, FiSquare, FiMessageSquare, FiAlertTriangle, FiRefreshCw } from 'react-icons/fi';

export default function ChatPage() {
  const { messages, streaming, sessionId, slashCommands, send, stop, retry, reset, resume, setMessages } = useChat();
  const ensureConfig = useConfigStore(s => s.ensure);
  const [input, setInput] = useState('');
  const [cwd, setCwd] = useState(localStorage.getItem('claude_web_cwd') || '');
  const transcriptRef = useRef(null);

  // Seed the default cwd from the server (portable) when nothing is stored and
  // we're not resuming a session (resume sets its own cwd).
  useEffect(() => {
    if (cwd) return;
    ensureConfig().then(cfg => {
      if (cfg?.defaultCwd) setCwd(prev => prev || cfg.defaultCwd);
    });
  }, [cwd, ensureConfig]);
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const [sessionTitle, setSessionTitle] = useState('');
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');

  // On mount or when ?resume= changes, load that session's history and title
  useEffect(() => {
    const resumeId = searchParams.get('resume');
    if (resumeId && resumeId !== sessionId) {
      resume(resumeId);
      fetch(`/api/sessions/${encodeURIComponent(resumeId)}/title`)
        .then(r => r.json())
        .then(data => {
          setSessionTitle(data.title || '');
          // Resume is cwd-scoped: claude --resume must run in the directory the
          // session was created under, or it reports "No conversation found".
          if (data.cwd) setCwd(data.cwd);
        })
        .catch(() => {});
      fetch(`/api/sessions/${encodeURIComponent(resumeId)}/transcript?limit=500&offset=0&tail=true`)
        .then(r => r.json())
        .then(data => {
          const history = parseTranscriptToMessages(data.messages || []);
          setMessages(history);
        })
        .catch(() => {});
    }
  }, [searchParams, sessionId, resume, setMessages]);

  // Fetch title when a new session is created (after first response)
  useEffect(() => {
    if (sessionId && !searchParams.get('resume')) {
      fetch(`/api/sessions/${encodeURIComponent(sessionId)}/title`)
        .then(r => r.json())
        .then(data => setSessionTitle(data.title || ''))
        .catch(() => {});
    }
  }, [sessionId, searchParams]);

  // Seed the input box from a pre-fill prompt passed via navigation state
  // (e.g. "trigger goal" from the Tasks/Projects pages). We do NOT auto-send —
  // the user reviews and presses send. Clear the history state afterward so a
  // refresh or back-nav doesn't re-seed it.
  useEffect(() => {
    const prefill = location.state?.prefillPrompt;
    if (prefill) {
      setInput(prefill);
      window.history.replaceState({}, document.title);
    }
  }, [location.state]);

  const saveTitle = () => {
    const newTitle = titleDraft.trim();
    if (!newTitle || !sessionId) { setEditingTitle(false); return; }
    fetch(`/api/sessions/${encodeURIComponent(sessionId)}/title`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: newTitle }),
    })
      .then(r => r.json())
      .then(data => { setSessionTitle(data.title); setEditingTitle(false); })
      .catch(() => setEditingTitle(false));
  };

  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSubmit = (e) => {
    e.preventDefault();
    const prompt = input.trim();
    if (!prompt || streaming) return;
    setInput('');
    send(prompt, { cwd });
  };

  const textareaRef = useRef(null);
  const slashMenuRef = useRef(null);
  const [showSlashMenu, setShowSlashMenu] = useState(false);

  const onSlashSelect = (cmd) => {
    if (cmd) {
      setInput(`/${cmd} `);
      textareaRef.current?.focus();
    }
    setShowSlashMenu(false);
  };

  const handleKeyDown = (e) => {
    if (showSlashMenu && slashMenuRef.current?.handleKeyDown(e)) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleInput = (e) => {
    const val = e.target.value;
    setInput(val);
    setShowSlashMenu(val.startsWith('/') && !val.includes(' '));
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 100px)' }}>
      <div className="page-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
          {editingTitle ? (
            <form onSubmit={e => { e.preventDefault(); saveTitle(); }} style={{ display: 'flex', gap: '0.4em', alignItems: 'center' }}>
              <input
                className="form-input"
                style={{ width: '250px', padding: '0.3em 0.6em', fontSize: '0.9em' }}
                value={titleDraft}
                onChange={e => setTitleDraft(e.target.value)}
                placeholder="Session name"
                autoFocus
                onBlur={saveTitle}
              />
              <button type="submit" className="icon-btn" title="Save"><FiCheck size={16} /></button>
            </form>
          ) : (
            <>
              <h2>{sessionTitle || 'Chat'}</h2>
              {sessionId && (
                <button
                  className="icon-btn"
                  onClick={() => { setTitleDraft(sessionTitle); setEditingTitle(true); }}
                  title="Rename session"
                >
                  <FiEdit3 size={14} />
                </button>
              )}
            </>
          )}
        </div>
        <div style={{ display: 'flex', gap: '0.5em', alignItems: 'center' }}>
          <span style={{ fontSize: '0.78em', color: 'var(--muted)' }}>cwd: {cwd}</span>
          {sessionId && <span className="badge">{sessionId.slice(0, 8)}</span>}
          <button className="btn" onClick={() => { reset(); setSearchParams({}); setSessionTitle(''); }} title="New session"><FiPlus size={14} /> New</button>
        </div>
      </div>

      <div ref={transcriptRef} style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '1em' }}>
        {messages.length === 0 && (
          <div className="empty-state">
            <FiMessageSquare size={32} style={{ color: 'var(--accent)', marginBottom: '0.5em', opacity: 0.6 }} />
            <h3>Send a prompt to Claude Code</h3>
            <p>All MCPs, skills, and CLAUDE.md are available.</p>
            <p style={{ fontSize: '0.78em', color: 'var(--muted)', marginTop: '0.5em' }}>
              Type <kbd>/</kbd> for commands · Enter to send · Shift+Enter for newline
            </p>
          </div>
        )}
        {messages.map((msg, i) => (
          <ErrorBoundary key={i} label="Failed to render this message.">
            {msg.role === 'auth-error'
              ? <AuthErrorBanner message={msg} onRetry={retry} disabled={streaming} />
              : msg.toolCall
                ? <ToolCallPanel toolCall={msg.toolCall} />
                : <MessageBubble message={msg} />}
          </ErrorBoundary>
        ))}
        {streaming && (
          <div className="thinking-indicator">
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span style={{ marginLeft: '0.3em' }}>Claude is working</span>
          </div>
        )}
      </div>

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: '0.5em', paddingTop: '1em', borderTop: '1px solid var(--border)', position: 'relative' }}>
        <SlashCommandMenu
          ref={slashMenuRef}
          commands={slashCommands}
          input={input}
          visible={showSlashMenu}
          onSelect={onSlashSelect}
        />
        <textarea
          ref={textareaRef}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder="Message Claude…"
          rows={1}
          disabled={streaming}
          className="form-textarea"
          style={{ minHeight: '42px', maxHeight: '160px', resize: 'none', overflow: 'auto' }}
        />
        {streaming ? (
          <button type="button" className="btn" onClick={stop} title="Stop generating">
            <FiSquare size={16} /> Stop
          </button>
        ) : (
          <button type="submit" className="btn btn-primary" disabled={!input.trim()} title="Send">
            <FiSend size={16} />
          </button>
        )}
      </form>
      {/* The console has no interactive approval channel, so runs execute tool
          calls (including file edits) without per-edit confirmation. */}
      <div
        style={{ fontSize: '0.72em', color: 'var(--muted)', paddingTop: '0.5em', textAlign: 'center' }}
        title="Mode is configurable via ~/.claude-web/config.json 'permissionMode' or CLAUDE_WEB_PERMISSION_MODE (default: bypassPermissions)."
      >
        Console runs execute tool calls, including file edits, without per-edit confirmation.
      </div>
    </div>
  );
}

// Distinct, actionable banner for credential/auth failures. Explains the fix
// (re-run mwinit) and offers a bounded one-click retry that respawns the
// backend subprocess with fresh credentials and re-sends the last prompt once.
function AuthErrorBanner({ message, onRetry, disabled }) {
  const [details, setDetails] = useState(false);
  return (
    <div
      data-role="auth-error"
      style={{
        border: '1px solid var(--warning, #d9a400)',
        background: 'color-mix(in srgb, var(--warning, #d9a400) 12%, transparent)',
        borderRadius: 'var(--radius)',
        padding: '0.9em 1em',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '0.6em' }}>
        <FiAlertTriangle size={18} style={{ color: 'var(--warning, #d9a400)', flexShrink: 0, marginTop: '0.1em' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, marginBottom: '0.25em' }}>Authentication failed</div>
          <div style={{ fontSize: '0.9em', lineHeight: 1.5 }}>
            {message.content}
          </div>
          <div style={{ display: 'flex', gap: '0.5em', marginTop: '0.7em', alignItems: 'center', flexWrap: 'wrap' }}>
            <button className="btn btn-primary" onClick={onRetry} disabled={disabled} title="Respawn the session with fresh credentials and retry">
              <FiRefreshCw size={14} /> Retry
            </button>
            {message.detail && (
              <button className="btn" onClick={() => setDetails(d => !d)} style={{ fontSize: 'var(--fs-xs)' }}>
                {details ? 'Hide details' : 'Show details'}
              </button>
            )}
          </div>
          {details && message.detail && (
            <pre style={{
              marginTop: '0.6em', fontSize: '0.75em', whiteSpace: 'pre-wrap',
              color: 'var(--muted)', background: 'var(--bg)', padding: '0.6em',
              borderRadius: 'var(--radius)', overflow: 'auto', maxHeight: 160,
            }}>{message.detail}</pre>
          )}
        </div>
      </div>
    </div>
  );
}
