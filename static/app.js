const transcriptEl = document.getElementById("transcript");
const cwdLabel = document.getElementById("cwd-label");
const composer = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const resetBtn = document.getElementById("reset-btn");

const STORAGE_KEY_SESSION = "claude_web_session_id";
const STORAGE_KEY_CWD = "claude_web_cwd";

let currentSessionId = sessionStorage.getItem(STORAGE_KEY_SESSION) || null;
let cwd = localStorage.getItem(STORAGE_KEY_CWD) || "";
cwdLabel.textContent = cwd ? `cwd: ${cwd}` : "cwd: (default)";

function bubble(kind, text) {
  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  div.textContent = text;
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  return div;
}

function appendToBubble(div, text) {
  div.textContent += text;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function summarizeToolUse(content) {
  const name = content.name || "tool";
  const input = content.input ? JSON.stringify(content.input).slice(0, 200) : "";
  return `→ ${name}${input ? "(" + input + ")" : ""}`;
}

function summarizeToolResult(content) {
  let text = "";
  if (typeof content.content === "string") text = content.content;
  else if (Array.isArray(content.content)) text = content.content.map(c => c.text || "").join("");
  return `← ${text.slice(0, 300)}${text.length > 300 ? "…" : ""}`;
}

function handleStreamEvent(evt, assistantBubble) {
  // stream-json event types: system, assistant, user, result.
  if (evt.type === "system" && evt.session_id) {
    currentSessionId = evt.session_id;
    sessionStorage.setItem(STORAGE_KEY_SESSION, currentSessionId);
    return assistantBubble;
  }
  if (evt.type === "assistant" && evt.message) {
    for (const c of evt.message.content || []) {
      if (c.type === "text") {
        if (!assistantBubble) assistantBubble = bubble("assistant", "");
        appendToBubble(assistantBubble, c.text);
      } else if (c.type === "tool_use") {
        bubble("tool", summarizeToolUse(c));
        assistantBubble = null;
      }
    }
    return assistantBubble;
  }
  if (evt.type === "user" && evt.message) {
    // tool_result echoes — only show when content is non-trivial.
    for (const c of evt.message.content || []) {
      if (c.type === "tool_result") bubble("tool", summarizeToolResult(c));
    }
    return null;
  }
  if (evt.type === "result") {
    if (evt.is_error) bubble("error", evt.result || "claude returned an error");
    return null;
  }
  return assistantBubble;
}

async function send(prompt) {
  bubble("user", prompt);
  let assistantBubble = null;

  const body = { prompt };
  if (currentSessionId) body.resume = currentSessionId;
  if (cwd) body.cwd = cwd;

  let resp;
  try {
    resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    bubble("error", `network error: ${e.message}`);
    return;
  }
  if (!resp.ok) {
    bubble("error", `${resp.status} ${resp.statusText}`);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (!frame.startsWith("data: ")) continue;
      const payload = frame.slice(6);
      if (payload === "[DONE]") return;
      try {
        const evt = JSON.parse(payload);
        assistantBubble = handleStreamEvent(evt, assistantBubble);
      } catch (e) {
        // Non-JSON line — show raw.
        bubble("tool", payload);
      }
    }
  }
}

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const prompt = input.value.trim();
  if (!prompt) return;
  input.value = "";
  sendBtn.disabled = true;
  send(prompt).finally(() => { sendBtn.disabled = false; input.focus(); });
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

resetBtn.addEventListener("click", () => {
  currentSessionId = null;
  sessionStorage.removeItem(STORAGE_KEY_SESSION);
  bubble("tool", "— new session —");
});

// Long-press header cwd label to set a project cwd (small power-user affordance).
cwdLabel.addEventListener("click", () => {
  const next = prompt("set cwd (must resolve under $HOME or $HOME/workspace; blank = default):", cwd);
  if (next === null) return;
  cwd = next.trim();
  if (cwd) localStorage.setItem(STORAGE_KEY_CWD, cwd);
  else localStorage.removeItem(STORAGE_KEY_CWD);
  cwdLabel.textContent = cwd ? `cwd: ${cwd}` : "cwd: (default)";
});
