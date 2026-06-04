import { useState, useRef, useEffect } from 'react';
import { useChat } from './hooks/useChat';
import { MessageBubble } from './components/MessageBubble';
import { ToolCallPanel } from './components/ToolCallPanel';
import { SessionList } from './components/SessionList';
import { CronPanel } from './components/CronPanel';
import { CwdSelector } from './components/CwdSelector';
import { FiSend, FiPlus, FiSidebar } from 'react-icons/fi';
import 'highlight.js/styles/github-dark.css';
import './App.css';

function App() {
  const { messages, streaming, sessionId, send, reset, resume } = useChat();
  const [input, setInput] = useState('');
  const [cwd, setCwd] = useState(localStorage.getItem('claude_web_cwd') || '/home/xulaicao');
  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth > 768);
  const transcriptRef = useRef(null);

  useEffect(() => {
    localStorage.setItem('claude_web_cwd', cwd);
  }, [cwd]);

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
    <div className="app">
      {sidebarOpen && (
        <aside className="sidebar">
          <SessionList onResume={resume} currentSessionId={sessionId} />
          <CronPanel />
        </aside>
      )}
      <main className="main">
        <header className="topbar">
          <button className="icon-btn" onClick={() => setSidebarOpen(!sidebarOpen)} title="Toggle sidebar">
            <FiSidebar size={18} />
          </button>
          <h1>Claude Code Console</h1>
          <CwdSelector cwd={cwd} onChange={setCwd} />
          <button className="icon-btn" onClick={reset} title="New session">
            <FiPlus size={18} />
          </button>
          {sessionId && <span className="session-badge">{sessionId.slice(0, 8)}</span>}
        </header>

        <div className="transcript" ref={transcriptRef}>
          {messages.length === 0 && (
            <div className="empty-state">
              <h2>Claude Code Console</h2>
              <p>Send a prompt to interact with Claude Code on your Cloud Desktop.</p>
              <p className="hint">All MCPs (Slack, Builder, Outlook) and skills are available.</p>
            </div>
          )}
          {messages.map((msg, i) => (
            msg.toolCall
              ? <ToolCallPanel key={i} toolCall={msg.toolCall} />
              : <MessageBubble key={i} message={msg} />
          ))}
          {streaming && <div className="streaming-indicator">Claude is thinking…</div>}
        </div>

        <form className="composer" onSubmit={handleSubmit}>
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Message Claude — Enter to send, Shift+Enter for newline"
            rows={2}
            disabled={streaming}
          />
          <button type="submit" disabled={streaming || !input.trim()} title="Send">
            <FiSend size={18} />
          </button>
        </form>
      </main>
    </div>
  );
}

export default App;
