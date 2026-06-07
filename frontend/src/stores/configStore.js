import { create } from 'zustand';

// Server-provided environment config (home dir, default cwd, workspace
// projects). Fetched once from /api/config and cached so pages don't hardcode
// personal paths like '/home/xulaicao' or '-local-home-xulaicao'.
export const useConfigStore = create((set, get) => ({
  config: null,
  loading: false,
  loaded: false,

  // Fetch once. Safe to call from multiple components on mount — concurrent
  // calls are coalesced via the `loading` guard, and a completed fetch is not
  // repeated.
  ensure: async () => {
    const { loaded, loading } = get();
    if (loaded || loading) return get().config;
    set({ loading: true });
    try {
      const res = await fetch('/api/config');
      const config = await res.json();
      set({ config, loading: false, loaded: true });
      return config;
    } catch {
      // Leave loaded=false so a later call can retry; callers fall back to
      // sensible defaults when config is null.
      set({ loading: false });
      return null;
    }
  },
}));
