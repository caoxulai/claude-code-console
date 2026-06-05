import { create } from 'zustand';

export const useSettingsStore = create((set, get) => ({
  data: null,
  etag: null,
  loading: false,
  error: null,

  fetch: async () => {
    set({ loading: true, error: null });
    try {
      const res = await fetch('/api/settings');
      const json = await res.json();
      set({ data: json.data, etag: json.etag, loading: false });
    } catch (e) {
      set({ loading: false, error: e.message });
    }
  },

  save: async (newData) => {
    const { etag } = get();
    set({ loading: true, error: null });
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: newData, etag }),
      });
      const json = await res.json();
      if (res.status === 409) {
        set({ data: json.current, etag: json.etag, loading: false, error: 'Settings changed externally. Your changes were not saved. Review and try again.' });
        return false;
      }
      set({ data: newData, etag: json.etag, loading: false });
      return true;
    } catch (e) {
      set({ loading: false, error: e.message });
      return false;
    }
  },

  invalidate: () => {
    get().fetch();
  },
}));
