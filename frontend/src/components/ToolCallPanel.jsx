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
  const inputPreview = inputStr.length > 120 ? inputStr.slice(0, 120) + '…' : inputStr;
  const hasResult = toolCall.result != null;

  return (
    <div className="tool-panel">
      <div className="tool-header" onClick={() => setExpanded(!expanded)}>
        {expanded ? <FiChevronDown size={14} /> : <FiChevronRight size={14} />}
        <Icon size={14} />
        <span className="tool-name">{toolCall.name}</span>
        {!expanded && <span className="tool-preview">{inputPreview}</span>}
        {hasResult && <span className="tool-badge">done</span>}
        {!hasResult && <span className="tool-badge tool-badge-pending">running…</span>}
      </div>
      {expanded && (
        <div className="tool-body">
          {inputStr && (
            <div className="tool-section">
              <div className="tool-section-label">Input</div>
              <pre>{inputStr}</pre>
            </div>
          )}
          {hasResult && (
            <div className="tool-section">
              <div className="tool-section-label">Output</div>
              <pre>{toolCall.result.slice(0, 5000)}{toolCall.result.length > 5000 ? '\n…truncated' : ''}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
