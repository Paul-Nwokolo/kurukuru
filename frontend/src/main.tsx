import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App.tsx'
import { SessionGate } from './components/SessionGate'
import './index.css'

// One shared client. Data-fetching cadence (3s instance polling, 15s health)
// is configured per-hook in the components that own each query.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* Inside the provider, because losing a session has to clear the cache
          as well as the screen. The gate swaps what is rendered and never
          navigates, so the path survives an expiry: sign in again and the same
          instance detail page comes back, rather than the dashboard root. */}
      <SessionGate>
        <App />
      </SessionGate>
    </QueryClientProvider>
  </StrictMode>,
)
