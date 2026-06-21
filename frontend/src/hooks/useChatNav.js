import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

// Build the slug Claude Code uses for a project's session directory:
// "/local/home/u/workspace/projects/foo" -> "-local-home-u-workspace-projects-foo".
// Same logic as useTriggerGoal's projectPathToSlug (not exported there).
function projectPathToSlug(path) {
  return '-' + path.replace(/^\//, '').replace(/\//g, '-');
}

/**
 * Returns an async `openChat(prompt, { projectPath })` function that navigates
 * to the Chat page with the given prompt prefilled (NOT auto-sent).
 *
 * Session resolution (mirrors useTriggerGoal):
 *   - no projectPath         → open a fresh Chat with the prompt prefilled
 *   - project has 0 sessions → same as above
 *   - project has 1 session  → resume it
 *   - project has N sessions → invoke caller's onPickSession callback
 *     with { prompt, sessions, resume: (id) => ... }
 *
 * Options:
 *   onPickSession({ prompt, sessions, resume }) — called when multiple sessions
 *   exist and the caller needs to let the user choose.
 */
export function useChatNav({ onPickSession } = {}) {
  const navigate = useNavigate();

  const navigateToChat = useCallback((prompt, { resumeId } = {}) => {
    const search = resumeId ? `?resume=${encodeURIComponent(resumeId)}` : '';
    navigate(`/chat${search}`, { state: { prefillPrompt: prompt } });
  }, [navigate]);

  const openChat = useCallback(async (prompt, { projectPath } = {}) => {
    // No project path → open a fresh chat with the prompt prefilled.
    if (!projectPath) {
      navigateToChat(prompt);
      return;
    }

    const slug = projectPathToSlug(projectPath);
    let sessions = [];
    try {
      const res = await fetch(`/api/sessions?project=${encodeURIComponent(slug)}&limit=50`);
      const json = await res.json();
      sessions = Array.isArray(json.sessions) ? json.sessions : [];
    } catch {
      // Network/listing failure — fall back to a fresh chat with the prompt.
      navigateToChat(prompt);
      return;
    }

    if (sessions.length === 0) {
      navigateToChat(prompt);
    } else if (sessions.length === 1) {
      navigateToChat(prompt, { resumeId: sessions[0].id });
    } else if (onPickSession) {
      // Defer to the caller's UI; it calls back with the chosen session id.
      onPickSession({ prompt, sessions, resume: (id) => navigateToChat(prompt, { resumeId: id }) });
    } else {
      // No picker provided — default to the most recent session.
      navigateToChat(prompt, { resumeId: sessions[0].id });
    }
  }, [navigateToChat, onPickSession]);

  return openChat;
}
