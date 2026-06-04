const BASE = '';

export async function fetchJSON(path, opts = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...opts.headers },
  });
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

export async function* streamChat(prompt, { cwd, resume } = {}) {
  const body = { prompt };
  if (cwd) body.cwd = cwd;
  if (resume) body.resume = resume;

  const resp = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (!frame.startsWith('data: ')) continue;
      const payload = frame.slice(6);
      if (payload === '[DONE]') return;
      try {
        yield JSON.parse(payload);
      } catch {
        yield { type: 'raw', text: payload };
      }
    }
  }
}

export async function listSessions() {
  return fetchJSON('/api/sessions');
}

export async function listCronJobs() {
  return fetchJSON('/api/crons');
}

export async function deleteCronJob(id) {
  return fetchJSON(`/api/crons/${id}`, { method: 'DELETE' });
}
