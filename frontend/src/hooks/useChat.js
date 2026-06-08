import { useState, useCallback, useRef } from 'react';
import { streamChat, stopChat } from '../api/client';

export function useChat() {
  const [messages, setMessages] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [slashCommands, setSlashCommands] = useState([]);
  // Holds the AbortController for the in-flight turn so stop() can cancel it.
  const abortRef = useRef(null);

  const send = useCallback(async (prompt, { cwd } = {}) => {
    setMessages(prev => [...prev, { role: 'user', content: prompt }]);
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    let assistantText = '';
    const toolCalls = [];
    let currentToolCall = null;

    try {
      for await (const evt of streamChat(prompt, { cwd, resume: sessionId, signal: controller.signal })) {
        if (evt.type === 'system' && evt.session_id) {
          setSessionId(evt.session_id);
          if (evt.slash_commands) setSlashCommands(evt.slash_commands);
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
            if (c.type === 'tool_result') {
              const resultText = typeof c.content === 'string'
                ? c.content
                : Array.isArray(c.content)
                  ? c.content.map(x => x.text || '').join('')
                  : '';
              const toolId = c.tool_use_id || currentToolCall?.id;
              if (toolId) {
                setMessages(prev => {
                  const idx = prev.findLastIndex(m => m.toolCall && m.toolCall.id === toolId);
                  if (idx >= 0) {
                    const updated = [...prev];
                    updated[idx] = { ...updated[idx], toolCall: { ...updated[idx].toolCall, result: resultText } };
                    return updated;
                  }
                  return prev;
                });
              }
              currentToolCall = null;
              assistantText = '';
            }
          }
        } else if (evt.type === 'result' && evt.is_error) {
          setMessages(prev => [...prev, { role: 'error', content: evt.result || 'Error' }]);
        }
      }
    } catch (err) {
      // An intentional stop (AbortController) isn't an error — show a quiet
      // interruption notice instead of a red error bubble.
      if (err.name === 'AbortError') {
        setMessages(prev => [...prev, { role: 'system', content: 'Response stopped.' }]);
      } else {
        setMessages(prev => [...prev, { role: 'error', content: err.message }]);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [sessionId]);

  // Interrupt the in-flight turn: abort the client stream AND tell the backend
  // to terminate the session process (so it doesn't keep running detached).
  const stop = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
    stopChat(sessionId);
  }, [sessionId]);

  const reset = useCallback(() => {
    setMessages([]);
    setSessionId(null);
  }, []);

  const resume = useCallback((id) => {
    setMessages([]);
    setSessionId(id);
  }, []);

  return { messages, streaming, sessionId, slashCommands, send, stop, reset, resume, setMessages };
}
