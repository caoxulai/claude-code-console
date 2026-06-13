const BASE = '';

export async function* streamChat(prompt, { cwd, resume, signal } = {}) {
  const body = { prompt };
  if (cwd) body.cwd = cwd;
  if (resume) body.resume = resume;

  const resp = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
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

// Ask the backend to terminate a session's Claude process. Used by the Chat
// Stop button so interrupting a turn also tears down the underlying process
// (aborting the fetch alone stops streaming to the client but leaves the
// process running server-side).
export async function stopChat(sessionId) {
  if (!sessionId) return;
  try {
    await fetch(`${BASE}/api/chat/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });
  } catch {
    /* best-effort; the abort already stopped the client-side stream */
  }
}

// Respawn a session's Claude process so it picks up freshly-authenticated
// credentials (the long-lived process caches the creds it started with). Used
// by auth-error recovery before re-sending the prompt. Throws on failure so the
// caller can surface it rather than silently re-sending against a stale process.
export async function restartChat(sessionId, cwd) {
  if (!sessionId) return;
  const body = { session_id: sessionId };
  if (cwd) body.cwd = cwd;
  const resp = await fetch(`${BASE}/api/chat/restart`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`restart failed: ${resp.status}`);
}
