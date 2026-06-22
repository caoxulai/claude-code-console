export function parseTranscriptToMessages(records) {
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
