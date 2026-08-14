import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Backend the dev server proxies the console WebSocket to. HTTP calls go
// straight to VITE_API_URL (CORS is configured for them); only the WebSocket is
// proxied, so the browser opens it same-origin and no cross-origin upgrade is
// involved in dev.
const API_TARGET = process.env.VITE_API_URL || 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Regex-scoped so it catches the console upgrade and nothing else — a
      // bare '/instances' key would swallow every REST call too.
      '^/instances/[^/]+/console$': {
        target: API_TARGET,
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
