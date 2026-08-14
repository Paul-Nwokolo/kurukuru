import { useHealth } from '../hooks/queries'
import { useBackendStatus } from '../lib/backend'
import { StatusBadge } from '../ui/Status'

/**
 * Backend liveness. An indicator, not a control.
 *
 * It used to be a bordered pill with a background — the same shape as the
 * Refresh button beside it — so it read as something you could press, and
 * pressing it did nothing. The border and fill are gone: it is now a dot and a
 * word, which is what it always was.
 *
 * It reads `useBackendStatus`, the *same* thing the banner reads, and that is
 * the point. Reading `/health` directly meant it could sit on a green
 * "Backend online" for up to fifteen seconds — one poll interval — while a
 * request that had just failed showed a network error immediately below it.
 * Two indicators of one fact will eventually disagree, and the one that is
 * wrong is always the reassuring one.
 *
 * "Degraded" is a separate axis and stays here: the backend answered, so it is
 * online; what it said was that its hypervisor is not usable.
 */
export function HealthIndicator() {
  const status = useBackendStatus()
  const { data, isLoading } = useHealth()

  if (isLoading && status.online) {
    return (
      <div className="inline-flex items-center gap-2 px-1" title="Checking…">
        <StatusBadge status="Pending" label="Checking…" />
      </div>
    )
  }

  const degraded = status.online && data?.status !== 'ok'
  const badge = !status.online
    ? { status: 'Error', label: 'Backend unreachable' }
    : degraded
      ? { status: 'Error', label: 'Engine unavailable' }
      : { status: 'Running', label: 'Backend online' }

  // The tooltip carries the diagnosis; the banner carries the remedy. Both
  // come from the one status object, so they cannot drift apart.
  const title = status.online
    ? degraded
      ? 'The backend is up, but its hypervisor is not usable — see Settings.'
      : 'Backend online'
    : [status.summary, status.remedy].filter(Boolean).join(' ')

  return (
    <div className="inline-flex items-center gap-2 px-1" title={title}>
      <StatusBadge status={badge.status} label={badge.label} />
    </div>
  )
}
