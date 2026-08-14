import type { ReactNode } from 'react'

/**
 * A tag. Monochrome by default, and that default is the point.
 *
 * `BUILTIN`, `CLOUD-INIT`, `DEFAULT`, `ORCHESTRATOR`, `derived`, `QEMU` — none
 * of these is a *state*. They are facts about a thing, and before this phase
 * each had picked up its own colour, which meant a row could carry four
 * coloured chips none of which told you whether anything was wrong.
 *
 * `tone="quiet"` exists for the ones that are explicitly about absence of
 * significance — a derived row, a retired engine — and it is grey too, just
 * dimmer.
 */
type BadgeTone = 'default' | 'quiet' | 'accent'

const TONES: Record<BadgeTone, string> = {
  default: 'border-border text-text-muted',
  quiet: 'border-transparent bg-surface-overlay text-text-subtle',
  // The only coloured badge, and only for "this is the one you selected" —
  // a UI state, not a resource state.
  accent: 'border-accent/40 bg-accent-quiet text-accent-text',
}

export function Badge({
  children,
  tone = 'default',
  title,
  className = '',
}: {
  children: ReactNode
  tone?: BadgeTone
  title?: string
  className?: string
}) {
  return (
    <span
      title={title}
      className={[
        'inline-flex items-center rounded-sm border px-1.5 py-px',
        'text-2xs font-medium uppercase tracking-wide whitespace-nowrap',
        TONES[tone],
        className,
      ].join(' ')}
    >
      {children}
    </span>
  )
}
