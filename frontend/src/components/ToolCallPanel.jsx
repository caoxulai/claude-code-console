import { useState } from 'react';
import { FiChevronDown, FiChevronRight, FiTerminal, FiFile, FiEdit3 } from 'react-icons/fi';

const TOOL_ICONS = {
  Bash: FiTerminal,
  Read: FiFile,
  Edit: FiEdit3,
  Write: FiEdit3,
};

export function ToolCallPanel({ toolCall }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = TOOL_ICONS[toolCall.name] || FiTerminal;
  const inputStr = toolCall.input
    ? typeof toolCall.input === 'string'
      ? toolCall.input
      : JSON.stringify(toolCall.input, null, 2)
    : '';
  const inputPreview = inputStr.length > 100 ? inputStr.slice(0, 100) + '…' : inputStr;
  const hasResult = toolCall.result != null;

  return (
    <div className="chat-msg chat-msg-assistant tool-call-panel" data-role="tool">
      <div className="chat-msg-inner">
        <div className="chat-msg-avatar" style={{ background: 'var(--surface2)', color: 'var(--accent2)' }}>
          <Icon size={14} />
        </div>
        <div className="chat-msg-content">
          <div
            className="tool-call-header"
            role="button"
            tabIndex={0}
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
            onKeyDown={e => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                setExpanded(!expanded);
              }
            }}
          >
            {expanded ? <FiChevronDown size={13} /> : <FiChevronRight size={13} />}
            <span className="tool-call-name">{toolCall.name}</span>
            {!expanded && <span className="tool-call-preview">{inputPreview}</span>}
            {hasResult && <span className="badge badge-ok" style={{ marginLeft: 'auto' }}>done</span>}
            {!hasResult && <span className="badge badge-warn" style={{ marginLeft: 'auto' }}>running…</span>}
          </div>
          {expanded && (
            <div className="tool-call-body">
              {inputStr && (
                <div className="tool-call-section">
                  <div className="tool-call-section-label">Input</div>
                  <pre style={{ overflowWrap: 'break-word', whiteSpace: 'pre-wrap' }}>{inputStr}</pre>
                </div>
              )}
              {hasResult && (
                <div className="tool-call-section">
                  <div className="tool-call-section-label">Output</div>
                  <pre style={{ overflowWrap: 'break-word', whiteSpace: 'pre-wrap' }}>{toolCall.result.slice(0, 5000)}{toolCall.result.length > 5000 ? '\n…truncated' : ''}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
