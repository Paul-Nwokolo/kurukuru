import { Loader2, WifiOff } from 'lucide-react'
import { useBackendStatus } from '../lib/backend'

/**
 * The unreachable-backend banner.
 *
 * It used to say "Backend unreachable — showing last known data. Retrying
 * automatically…" and stop there, which is true of every possible cause and
 * therefore no help with any of them. It now names which one it is: nothing
 * listening on the port, or a backend that is running and refusing this
 * page's origin. Those have nothing in common except the symptom, and the
 * browser reports them identically — see lib/backend.ts.
 *
 * Non-blocking on purpose. The last known data is still the most useful thing
 * on the screen, so the banner explains and the app keeps working.
 */
export function BackendBanner() {
  const status = useBackendStatus()
  if (status.online) return null

  const Icon = status.reason === 'checking' ? Loader2 : WifiOff

  return (
    <div
      role="alert"
      className="flex items-start gap-2 border-b border-danger/40 bg-danger-quiet px-6 py-2 text-sm text-danger"
    >
      <Icon
        className={`mt-0.5 h-4 w-4 shrink-0 ${
          status.reason === 'checking' ? 'animate-spin' : ''
        }`}
      />
      <div className="min-w-0">
        <span>{status.summary}</span>{' '}
        {status.remedy && <span className="opacity-80">{status.remedy}</span>}{' '}
        <span className="opacity-60">
          Showing last known data; still retrying.
        </span>
      </div>
    </div>
  )
}
