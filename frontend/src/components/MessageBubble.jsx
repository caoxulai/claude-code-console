import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

export function MessageBubble({ message }) {
  if (message.role === 'user') {
    return (
      <div className="chat-msg chat-msg-user" data-role="user">
        <div className="chat-msg-inner">
          <div className="chat-msg-content">{message.content}</div>
        </div>
      </div>
    );
  }

  if (message.role === 'assistant') {
    return (
      <div className="chat-msg chat-msg-assistant" data-role="assistant">
        <div className="chat-msg-inner">
          <div className="chat-msg-avatar">C</div>
          <div className="chat-msg-content markdown-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
              {message.content}
            </ReactMarkdown>
          </div>
        </div>
      </div>
    );
  }

  if (message.role === 'error') {
    return (
      <div className="chat-msg chat-msg-error" data-role="error">
        <div className="chat-msg-inner">
          <div className="chat-msg-content">{message.content}</div>
        </div>
      </div>
    );
  }

  if (message.role === 'system') {
    return (
      <div data-role="system" style={{
        textAlign: 'center',
        color: 'var(--muted)',
        fontSize: '0.8em',
        fontStyle: 'italic',
        padding: '0.3em 0',
      }}>
        {message.content}
      </div>
    );
  }

  return null;
}
