import { useState, useRef, useCallback } from 'react';
import { streamChat } from '../api/client';

export function useChat() {
  const [messages, setMessages] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const abortRef = useRef(null);

  const send = useCallback(async (prompt, { cwd } = {}) => {
    setMessages(prev => [...prev, { role: 'user', content: prompt }]);
    setStreaming(true);

    let assistantText = '';
    const toolCalls = [];
    let currentToolCall = null;

    try {
      for await (const evt of streamChat(prompt, { cwd, resume: sessionId })) {
        if (evt.type === 'system' && evt.session_id) {
          setSessionId(evt.session_id);
        } else if (evt.type === 'assistant' && evt.message) {
          for (const c of evt.message.content || []) {
            if (c.type === 'text') {
              assistantText += c.text;
              setMessages(prev => {
                const last = prev[prev.length - 1];
                if (last && last.role === 'assistant' && !last.toolCall) {
                  return [...prev.slice(0, -1), { ...last, content: assistantText }];
                }
                return [...prev, { role: 'assistant', content: assistantText }];
              });
            } else if (c.type === 'tool_use') {
              currentToolCall = { id: c.id, name: c.name, input: c.input, result: null };
              toolCalls.push(currentToolCall);
              setMessages(prev => [...prev, { role: 'tool_use', toolCall: currentToolCall }]);
            }
          }
        } else if (evt.type === 'user' && evt.message) {
          for (const c of evt.message.content || []) {
            if (c.type === 'tool_result' && currentToolCall) {
              const resultText = typeof c.content === 'string'
                ? c.content
                : Array.isArray(c.content)
                  ? c.content.map(x => x.text || '').join('')
                  : '';
              currentToolCall.result = resultText;
              setMessages(prev => {
                const idx = prev.findLastIndex(m => m.toolCall?.id === currentToolCall.id);
                if (idx >= 0) {
                  const updated = [...prev];
                  updated[idx] = { ...updated[idx], toolCall: { ...currentToolCall } };
                  return updated;
                }
                return prev;
              });
              currentToolCall = null;
              assistantText = '';
            }
          }
        } else if (evt.type === 'result' && evt.is_error) {
          setMessages(prev => [...prev, { role: 'error', content: evt.result || 'Error' }]);
        }
      }
    } catch (err) {
      setMessages(prev => [...prev, { role: 'error', content: err.message }]);
    } finally {
      setStreaming(false);
    }
  }, [sessionId]);

  const reset = useCallback(() => {
    setMessages([]);
    setSessionId(null);
  }, []);

  const resume = useCallback((id) => {
    setMessages([]);
    setSessionId(id);
  }, []);

  return { messages, streaming, sessionId, send, reset, resume };
}
