import { useEffect, useState } from 'react'
import { Menu, RefreshCw, X } from 'lucide-react'
import { AccountMenu } from './AccountMenu'
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
 *
 * While it runs, the instances list stops polling (see useInstances). A table
 * that updated mid-reconcile for an unrelated reason would be attributed to the
 * button, which is exactly the wrong lesson to teach about the one control
 * people press when something already looks wrong.
 */
export function Header({
  title,
  /** Opens the navigation drawer. Undefined at `lg` and above, where the
   *  sidebar is a permanent column and a menu button would be a lie. */
  onOpenNav,
}: {
  title: string
  onOpenNav?: () => void
}) {
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
    // `min-w-0` on the title so it truncates, `shrink-0` on the controls so it
    // is the title that gives way. Without the pair, the reconcile result — the
    // one item here with unbounded text — pushed the account menu off the right
    // edge rather than either side yielding.
    <header className="flex items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3 lg:px-6">
      <div className="flex min-w-0 items-center gap-2">
        {onOpenNav && (
          <IconButton icon={Menu} title="Open navigation" onClick={onOpenNav} size="sm" />
        )}
        <h1 className="truncate text-lg font-semibold tracking-tight text-text lg:text-xl">
          {title}
        </h1>
      </div>
      <div className="flex shrink-0 items-center gap-2 lg:gap-3">
        {/* In progress, said in words.
          *
          * The spinning icon alone was the whole affordance, and at 16px next
          * to four other controls it reads as decoration — during a wait of
          * several seconds the honest question "did that do anything?" had no
          * answer on screen. This is deliberately a plain label rather than a
          * progress bar or a percentage: the backend reports nothing about how
          * far along a reconcile is, and inventing a bar that fills at a made
          * up rate would claim knowledge this UI does not have.
          *
          * It occupies the same slot as the result, so the sequence reads as
          * one message changing rather than two things appearing. */}
        {/* The in-progress label is the first thing to go when width runs out:
            it is the one item here with no control attached, and the refresh
            button spins beside it saying the same thing. The *result* stays at
            every width — an error is the reason someone is looking. */}
        {refresh.isPending && (
          <span
            role="status"
            className="hidden items-center gap-1.5 text-xs text-text-muted lg:flex"
          >
            <RefreshCw className="h-3 w-3 animate-spin" aria-hidden />
            Reconciling with the hypervisor…
          </span>
        )}
        {!refresh.isPending && result && (
          <span
            role={result.tone === 'error' ? 'alert' : 'status'}
            title={result.text}
            className={`flex min-w-0 items-center gap-1 text-xs ${
              result.tone === 'error' ? 'text-danger' : 'text-text-muted'
            }`}
          >
            <span className="max-w-[9rem] truncate lg:max-w-md">{result.text}</span>
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
        <div className="hidden h-4 w-px bg-border lg:block" aria-hidden />
        <ThemeToggle />
        <IconButton
          icon={RefreshCw}
          title={
            refresh.isPending
              ? 'Reconciling…'
              : 'Reconcile with the hypervisor and refetch'
          }
          onClick={run}
          disabled={refresh.isPending}
          className={refresh.isPending ? '[&_svg]:animate-spin' : ''}
        />
        <div className="hidden h-4 w-px bg-border lg:block" aria-hidden />
        <HealthIndicator />
        <div className="hidden h-4 w-px bg-border lg:block" aria-hidden />
        <AccountMenu />
      </div>
    </header>
  )
}
