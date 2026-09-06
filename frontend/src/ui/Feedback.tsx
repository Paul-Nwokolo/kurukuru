import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertCircle, Check, Copy, Info } from 'lucide-react'
import { absoluteTime, relativeTime } from '../lib/format'

/**
 * Inline messages, empty states, skeletons, copy buttons, truncation, time.
 *
 * Small things, but each was implemented three or four times with slightly
 * different wording and colour before this phase — which is how a UI ends up
 * feeling assembled rather than designed.
 */

/* ------------------------------------------------------------------ */
/* Alerts                                                              */
/* ------------------------------------------------------------------ */
type AlertTone = 'danger' | 'transitional' | 'info'

const ALERT_TONES: Record<AlertTone, { box: string; icon: typeof Info }> = {
  danger: { box: 'border-danger/40 bg-danger-quiet text-danger', icon: AlertCircle },
  transitional: {
    box: 'border-transitional/40 bg-transitional-quiet text-transitional',
    icon: AlertCircle,
  },
  // Not a status: an explanation. Grey, so it cannot be mistaken for one.
  info: { box: 'border-border bg-surface-raised text-text-muted', icon: Info },
}

export function Alert({
  tone = 'info',
  children,
  className = '',
}: {
  tone?: AlertTone
  children: ReactNode
  className?: string
}) {
  const config = ALERT_TONES[tone]
  const Icon = config.icon
  return (
    <div
      role={tone === 'danger' ? 'alert' : undefined}
      className={`flex items-start gap-2 rounded-md border px-3 py-2.5 text-sm ${config.box} ${className}`}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1 leading-relaxed">{children}</div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* Empty states                                                        */
/* ------------------------------------------------------------------ */
/**
 * The Instances empty state was the good one — an icon, a sentence that says
 * what the thing is for, and the action. Every other table got a shrug. This
 * is that pattern, extracted.
 */
export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon: typeof Info
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center px-6 py-14 text-center">
      <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-surface">
        <Icon className="h-5 w-5 text-text-subtle" />
      </div>
      <h3 className="text-base font-semibold text-text">{title}</h3>
      {children && (
        <p className="mt-1.5 max-w-md text-sm leading-relaxed text-text-muted">{children}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* Skeletons                                                           */
/* ------------------------------------------------------------------ */
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse rounded-sm bg-surface-overlay ${className}`} />
}

/* ------------------------------------------------------------------ */
/* Copy to clipboard                                                   */
/* ------------------------------------------------------------------ */
/**
 * One copy control, one confirmation.
 *
 * The check mark replaces the icon for two seconds — no toast, because the
 * confirmation belongs where the action was, not in the corner of the screen.
 *
 * That confirmation is visual, so it is also *said*. The swapped `aria-label`
 * alone was not enough: renaming the control a user is already focused on is
 * announced by some screen readers and silently ignored by others, which makes
 * "did that copy?" unanswerable for exactly the people who cannot see the tick.
 * The live region below is the part that is guaranteed to speak, and it is
 * `polite` so it waits its turn rather than interrupting.
 */
export function CopyButton({
  value,
  title = 'Copy',
  className = '',
}: {
  value: string
  title?: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
    } catch {
      // Older browsers, and any context where the clipboard API is blocked.
      const scratch = document.createElement('textarea')
      scratch.value = value
      document.body.appendChild(scratch)
      scratch.select()
      document.execCommand('copy')
      document.body.removeChild(scratch)
    }
    setCopied(true)
  }

  return (
    <>
      <button
        type="button"
        onClick={copy}
        title={copied ? 'Copied' : title}
        aria-label={copied ? 'Copied' : title}
        className={[
          'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded transition-colors',
          copied ? 'text-healthy' : 'text-text-subtle hover:bg-surface-overlay hover:text-text',
          className,
        ].join(' ')}
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
      {/* Empty until it has something to say: a live region that already holds
          text when it is inserted announces nothing, so the message has to
          *arrive* in a region that was there and empty beforehand. */}
      <span role="status" aria-live="polite" className="sr-only">
        {copied ? 'Copied to clipboard' : ''}
      </span>
    </>
  )
}

/* ------------------------------------------------------------------ */
/* Truncation                                                          */
/* ------------------------------------------------------------------ */
/**
 * A long path or fingerprint, cut with the full value on hover.
 *
 * `title` rather than a custom tooltip on purpose: it is selectable, it works
 * before JavaScript settles, and it is what a screen reader already announces.
 */
export function Truncated({
  value,
  className = '',
  data = true,
}: {
  value: string
  className?: string
  data?: boolean
}) {
  return (
    <span
      title={value}
      className={`block truncate ${data ? 'data' : ''} ${className}`}
    >
      {value}
    </span>
  )
}

/* ------------------------------------------------------------------ */
/* Paths                                                               */
/* ------------------------------------------------------------------ */
/**
 * A filesystem path on the backend's machine. One treatment, everywhere.
 *
 * There were three. A path could be a bordered box with a copy button
 * (ISOs, the generated-key modal), a truncated grey subtitle with only a
 * tooltip (key pairs), or a caption under a Fact with neither (Settings) —
 * and the two boxed ones did not even agree on their border token. Nothing
 * distinguished the cases except which screen was written first.
 *
 * What every one of them is, is the same thing: a long string the user cannot
 * retype, needs to see in full, and is going to paste into a terminal. So:
 *
 *   - **mono**, because it is data people compare character by character;
 *   - **truncated with the full value on hover**, because these are long and
 *     the middle of a path is rarely the interesting part;
 *   - **copyable**, always. That was the real inconsistency — two of the three
 *     places showed you a path you then had to select by hand.
 *
 * `dense` drops the box for use as a caption under something else. It changes
 * the chrome and nothing else: same font, same truncation, same tooltip, same
 * copy button, so it stays one style rather than becoming a second one.
 */
export function PathValue({
  value,
  dense = false,
  className = '',
  title,
}: {
  value: string
  dense?: boolean
  className?: string
  /** Overrides the hover text, for a path that needs naming as well as showing. */
  title?: string
}) {
  if (!value) return <span className="text-text-subtle">—</span>
  return (
    <div
      className={[
        'flex min-w-0 items-center gap-1',
        dense ? '' : 'rounded border border-border bg-surface px-2.5 py-1.5',
        className,
      ].join(' ')}
    >
      <span
        title={title ?? value}
        className={`block min-w-0 flex-1 truncate data ${
          dense ? 'text-2xs text-text-subtle' : 'text-xs text-text'
        }`}
      >
        {value}
      </span>
      <CopyButton
        value={value}
        title="Copy path"
        className={dense ? 'h-5 w-5' : ''}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* Time                                                                */
/* ------------------------------------------------------------------ */
/**
 * Relative time, absolute on hover. Everywhere, without exception.
 *
 * Some views showed "3m ago", others a formatted date, and the detail view did
 * both in adjacent rows. Relative is the right default — "how long has this
 * been up" is the question — and the exact timestamp is one hover away.
 */
export function TimeAgo({
  value,
  className = '',
}: {
  value: string | null | undefined
  className?: string
}) {
  if (!value) return <span className={className}>—</span>
  return (
    <time dateTime={value} title={absoluteTime(value)} className={className}>
      {relativeTime(value)}
    </time>
  )
}
