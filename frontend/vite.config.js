import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 9000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7780',
        changeOrigin: true,
      },
      // The live-update WebSocket (settings_changed / hooks_changed broadcasts).
      // Without this, file-change events don't reach the UI under `npm run dev`.
      '/ws': {
        target: 'http://127.0.0.1:7780',
        changeOrigin: true,
        ws: true,
      },
      '/healthz': {
        target: 'http://127.0.0.1:7780',
        changeOrigin: true,
      },
    },
  },
})
