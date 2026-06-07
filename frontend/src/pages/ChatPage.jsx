import { useState, useRef, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useChat } from '../hooks/useChat';
import { MessageBubble } from '../components/MessageBubble';
import { ToolCallPanel } from '../components/ToolCallPanel';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { FiSend, FiPlus, FiEdit3, FiCheck } from 'react-icons/fi';

function parseTranscriptToMessages(records) {
  const msgs = [];
  // First pass: collect tool results keyed by tool_use_id
  const toolResults = {};
  for (const rec of records) {
    if (rec.type === 'user' && rec.message?.content) {
      const content = Array.isArray(rec.message.content) ? rec.message.content : [];
      for (const c of content) {
        if (c.type === 'tool_result' && c.tool_use_id) {
          const text = typeof c.content === 'string'
            ? c.content
            : Array.isArray(c.content)
              ? c.content.map(x => x.text || '').join('')
              : '';
          toolResults[c.tool_use_id] = text;
        }
      }
    }
  }

  // Second pass: build message list
  for (const rec of records) {
    if (rec.type === 'user') {
      const m = rec.message;
      if (!m) continue;
      const text = typeof m.content === 'string'
        ? m.content
        : Array.isArray(m.content)
          ? m.content.filter(c => c.type === 'text').map(c => c.text).join('\n')
          : '';
      if (text) msgs.push({ role: 'user', content: text });
    } else if (rec.type === 'assistant') {
      const parts = rec.message?.content || [];
      for (const part of parts) {
        if (part.type === 'text' && part.text) {
          msgs.push({ role: 'assistant', content: part.text });
        } else if (part.type === 'tool_use') {
          const result = toolResults[part.id] || null;
          msgs.push({ role: 'tool_use', toolCall: { id: part.id, name: part.name, input: part.input, result } });
        }
      }
    }
  }
  return msgs;
}

export default function ChatPage() {
  const { messages, streaming, sessionId, send, reset, resume, setMessages } = useChat();
  const [input, setInput] = useState('');
  const [cwd] = useState(localStorage.getItem('claude_web_cwd') || '/home/xulaicao');
  const transcriptRef = useRef(null);
  const [searchParams, setSearchParams] = useSearchParams();
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
        .then(data => setSessionTitle(data.title || ''))
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

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
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
            <h3>Send a prompt to Claude Code</h3>
            <p>All MCPs, skills, and CLAUDE.md are available.</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <ErrorBoundary key={i} label="Failed to render this message.">
            {msg.toolCall
              ? <ToolCallPanel toolCall={msg.toolCall} />
              : <MessageBubble message={msg} />}
          </ErrorBoundary>
        ))}
        {streaming && <div className="thinking-indicator">Claude is thinking…</div>}
      </div>

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: '0.5em', paddingTop: '1em', borderTop: '1px solid var(--border)' }}>
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message Claude — Enter to send, Shift+Enter for newline"
          rows={2}
          disabled={streaming}
          className="form-textarea"
          style={{ minHeight: '42px' }}
        />
        <button type="submit" className="btn btn-primary" disabled={streaming || !input.trim()}>
          <FiSend size={16} />
        </button>
      </form>
    </div>
  );
}
