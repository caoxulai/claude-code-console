import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 9001,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:9000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://127.0.0.1:9000',
        changeOrigin: true,
        ws: true,
      },
      '/healthz': {
        target: 'http://127.0.0.1:9000',
        changeOrigin: true,
      },
    },
  },
})
