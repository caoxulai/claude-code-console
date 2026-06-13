import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

// Build the slug Claude Code uses for a project's session directory:
// "/local/home/u/workspace/projects/foo" -> "-local-home-u-workspace-projects-foo".
function projectPathToSlug(path) {
  return '-' + path.replace(/^\//, '').replace(/\//g, '-');
}

// Compose the /goal prompt text from a task.
export function goalPromptForTask(task) {
  const subject = task.subject || `Task ${task.id}`;
  const desc = task.description ? `\n\n${task.description}` : '';
  return `/goal ${subject}${desc}`;
}

/**
 * Returns a `triggerGoal(task)` function that opens the Chat page pre-filled
 * with a /goal prompt for the task, in the task's project.
 *
 * Session selection:
 *   - project has exactly one session  → resume it (Chat runs in its cwd)
 *   - project has more than one session → caller's onPickSession is invoked
 *     with the list so the UI can prompt; the chosen id is then resumed.
 *   - project has no sessions / no path → open a fresh Chat with the prompt
 *     pre-filled (cwd defaults to the server default).
 *
 * The prompt is passed via router state (location.state.prefillPrompt); ChatPage
 * seeds its input box from it but does NOT auto-send — the user reviews + sends.
 */
export function useTriggerGoal({ onPickSession } = {}) {
  const navigate = useNavigate();

  const openChat = useCallback((prompt, { resumeId } = {}) => {
    const search = resumeId ? `?resume=${encodeURIComponent(resumeId)}` : '';
    navigate(`/chat${search}`, { state: { prefillPrompt: prompt } });
  }, [navigate]);

  const triggerGoal = useCallback(async (task) => {
    const prompt = goalPromptForTask(task);

    // No known project path → can't scope to a session; open a fresh chat.
    if (!task.projectPath) {
      openChat(prompt);
      return;
    }

    const slug = projectPathToSlug(task.projectPath);
    let sessions = [];
    try {
      const res = await fetch(`/api/sessions?project=${encodeURIComponent(slug)}&limit=50`);
      const json = await res.json();
      sessions = Array.isArray(json.sessions) ? json.sessions : [];
    } catch {
      // Network/listing failure — fall back to a fresh chat with the prompt.
      openChat(prompt);
      return;
    }

    if (sessions.length === 0) {
      openChat(prompt);
    } else if (sessions.length === 1) {
      openChat(prompt, { resumeId: sessions[0].id });
    } else if (onPickSession) {
      // Defer to the caller's UI; it calls back with the chosen session id.
      onPickSession({ task, prompt, sessions, resume: (id) => openChat(prompt, { resumeId: id }) });
    } else {
      // No picker provided — default to the most recent session.
      openChat(prompt, { resumeId: sessions[0].id });
    }
  }, [openChat, onPickSession]);

  return triggerGoal;
}
