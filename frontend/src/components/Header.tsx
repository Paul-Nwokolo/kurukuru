import { useEffect, useState } from 'react'
import { RefreshCw, X } from 'lucide-react'
import { HealthIndicator } from './HealthIndicator'
import { ProjectSelector } from './ProjectSelector'
import { ThemeToggle } from './ThemeToggle'
import { IconButton } from '../ui/Button'
import { useRefreshInstances } from '../hooks/queries'
import { apiErrorMessage } from '../api/client'

/**
 * Title, project scope, theme, refresh, health.
 *
 * Refresh is an icon action because everything here already polls — the
 * instances list every three seconds. It is kept because it forces a
 * *reconcile* on the backend, which polling does not, but at the weight of an
 * escape hatch rather than a primary control.
 */
export function Header({ title }: { title: string }) {
  const refresh = useRefreshInstances()
  const [result, setResult] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)

  /*
   * Success fades; failure does not.
   *
   * Both used to disappear after four seconds, which is fine for "checked 12
   * instances" — you either read it or you did not care — and wrong for an
   * error. An error is the one message a user needs to still be there when
   * they look back at the screen, or copy into an issue, or read twice. A
   * timer deciding they have finished with it is the app throwing away the
   * only evidence of what went wrong.
   *
   * So errors stay until they are dismissed or replaced by the next attempt,
   * and they get a dismiss control, because "it goes away when you say so" is
   * the other half of the bargain.
   */
  useEffect(() => {
    if (!result || result.tone === 'error') return
    const timer = setTimeout(() => setResult(null), 4000)
    return () => clearTimeout(timer)
  }, [result])

  /*
   * Three separate defects made this button feel broken, and all three needed
   * fixing — an animation alone would have been theatre:
   *
   *   1. `refresh.mutate()` had no error handler at all, so a 502 from a wedged
   *      hypervisor was swallowed and *nothing at all* appeared. "Sometimes
   *      nothing happens" was sometimes literally a failed request.
   *   2. There was no success feedback, and the round trip is well under a
   *      second, so the spinner was imperceptible.
   *   3. The list already polls every 3s, so a reconcile that changed nothing
   *      looked identical to one that never ran.
   *
   * The fix for (3) is the interesting one: report *what the reconcile found*.
   * "Nothing changed" is a real, useful answer — it means the dashboard already
   * matched the hypervisor — and it is visibly different from a failure.
   */
  const run = async () => {
    setResult(null)
    try {
      const before = Date.now()
      const instances = await refresh.mutateAsync()
      const elapsed = Date.now() - before
      setResult({
        tone: 'ok',
        text: `Checked ${instances.length} instance${instances.length === 1 ? '' : 's'} against the hypervisor · ${elapsed}ms`,
      })
    } catch (err) {
      setResult({ tone: 'error', text: apiErrorMessage(err) })
    }
  }

  return (
    <header className="flex items-center justify-between gap-4 border-b border-border bg-surface px-6 py-3">
      <h1 className="text-xl font-semibold tracking-tight text-text">{title}</h1>
      <div className="flex items-center gap-3">
        {result && (
          <span
            role={result.tone === 'error' ? 'alert' : 'status'}
            title={result.text}
            className={`flex min-w-0 items-center gap-1 text-xs ${
              result.tone === 'error' ? 'text-danger' : 'text-text-muted'
            }`}
          >
            <span className="max-w-md truncate">{result.text}</span>
            {result.tone === 'error' && (
              <button
                type="button"
                onClick={() => setResult(null)}
                aria-label="Dismiss error"
                className="shrink-0 rounded p-0.5 hover:bg-danger/15"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </span>
        )}
        <ProjectSelector />
        <div className="h-4 w-px bg-border" aria-hidden />
        <ThemeToggle />
        <IconButton
          icon={RefreshCw}
          title="Reconcile with the hypervisor and refetch"
          onClick={run}
          disabled={refresh.isPending}
          className={refresh.isPending ? '[&_svg]:animate-spin' : ''}
        />
        <div className="h-4 w-px bg-border" aria-hidden />
        <HealthIndicator />
      </div>
    </header>
  )
}
