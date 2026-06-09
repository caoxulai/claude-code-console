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
      '/auth': {
        target: 'http://127.0.0.1:7780',
        changeOrigin: true,
      },
      '/healthz': {
        target: 'http://127.0.0.1:7780',
        changeOrigin: true,
      },
    },
  },
})
