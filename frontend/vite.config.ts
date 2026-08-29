import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Backend the dev server proxies the console WebSocket to. HTTP calls go
// straight to VITE_API_URL (CORS is configured for them); only the WebSocket is
// proxied, so the browser opens it same-origin and no cross-origin upgrade is
// involved in dev.
//
// The default port is 7842, not 8000 — see `port` in backend/kurukuru/config.py
// for why the familiar one was not kept.
const API_TARGET = process.env.VITE_API_URL || 'http://localhost:7842'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Regex-scoped so it catches the console upgrade and nothing else — a
      // bare '/api' key would swallow every REST call too, which in dev must
      // go direct so that CORS is exercised rather than proxied around.
      '^/api/instances/[^/]+/console$': {
        target: API_TARGET,
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
