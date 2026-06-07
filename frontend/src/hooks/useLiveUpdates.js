import { useEffect, useRef } from 'react';

// Single shared WebSocket to /ws for the whole app. The backend broadcasts
// file-change events (memory_changed, mcp_changed, settings_changed, cron_*,
// skill_*, hooks_changed, …) whenever a write happens — from this GUI, another
// browser tab, or the `claude` CLI editing the same files. Pages subscribe to
// the event types they care about and refetch, so the console stays in sync
// with the CLI without per-page polling.

const listeners = new Set(); // Set<(msg) => void>
let socket = null;
let reconnectTimer = null;
let reconnectDelay = 1000; // backs off to 30s

function ensureSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${window.location.host}/ws`;
  try {
    socket = new WebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    reconnectDelay = 1000; // reset backoff on a healthy connection
  };

  socket.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    for (const fn of listeners) {
      try { fn(msg); } catch { /* a listener throwing must not kill the rest */ }
    }
  };

  socket.onclose = () => {
    socket = null;
    if (listeners.size > 0) scheduleReconnect();
  };

  socket.onerror = () => {
    // onclose fires after onerror; reconnect is handled there.
    if (socket) socket.close();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (listeners.size > 0) ensureSocket();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 30000);
}

/**
 * Subscribe to live change events.
 *
 * @param {string[]} eventTypes - event `type` values to react to (e.g.
 *   ['memory_changed', 'memory_deleted']). Pass an empty array to receive all.
 * @param {() => void} onChange - called when a matching event arrives.
 */
export function useLiveUpdates(eventTypes, onChange) {
  // Keep the latest callback without re-subscribing on every render. Assigned
  // in an effect (not during render) so the ref write stays a side effect.
  const cbRef = useRef(onChange);
  useEffect(() => { cbRef.current = onChange; }, [onChange]);

  // Stabilize the type list so the effect doesn't re-run on array identity.
  const typesKey = (eventTypes || []).join(',');

  useEffect(() => {
    const types = typesKey ? typesKey.split(',') : [];
    const listener = (msg) => {
      if (types.length === 0 || types.includes(msg.type)) {
        cbRef.current();
      }
    };
    listeners.add(listener);
    ensureSocket();
    return () => {
      listeners.delete(listener);
      // When the last subscriber unmounts, close the socket so we don't hold
      // an idle connection (and so reconnect logic goes quiet).
      if (listeners.size === 0 && socket) {
        socket.close();
        socket = null;
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      }
    };
  }, [typesKey]);
}
