import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

export function MessageBubble({ message }) {
  if (message.role === 'user') {
    return (
      <div className="msg msg-user">
        <div className="msg-label">You</div>
        <div className="msg-body">{message.content}</div>
      </div>
    );
  }

  if (message.role === 'assistant') {
    return (
      <div className="msg msg-assistant">
        <div className="msg-label">Claude</div>
        <div className="msg-body markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {message.content}
          </ReactMarkdown>
        </div>
      </div>
    );
  }

  if (message.role === 'error') {
    return (
      <div className="msg msg-error">
        <div className="msg-label">Error</div>
        <div className="msg-body">{message.content}</div>
      </div>
    );
  }

  return null;
}
