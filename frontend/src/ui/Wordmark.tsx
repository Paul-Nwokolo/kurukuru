/**
 * The lockup. One component, one place, deliberately generic.
 *
 * **The product name is not decided.** This renders "Local IaaS" over
 * "Orchestrator" in the UI font with a neutral mark, and nothing else in the
 * application draws a logo — so replacing it later is editing this file rather
 * than hunting through a sidebar, a header and a favicon.
 *
 * The mark is monochrome on purpose. It used to be green, which was also the
 * colour of a healthy instance and of the primary button; a brand that shares
 * a colour with a status is a brand that changes meaning depending on where
 * you look.
 */
export function Wordmark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div
        aria-hidden
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border-strong bg-surface-raised"
      >
        {/* A placeholder glyph, not an identity: four squares for a grid of
            machines. Replace the whole component when the name is settled. */}
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5 text-text" fill="currentColor">
          <rect x="1" y="1" width="6" height="6" rx="1" />
          <rect x="9" y="1" width="6" height="6" rx="1" opacity="0.55" />
          <rect x="1" y="9" width="6" height="6" rx="1" opacity="0.55" />
          <rect x="9" y="9" width="6" height="6" rx="1" opacity="0.28" />
        </svg>
      </div>
      {!compact && (
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight text-text">Local IaaS</div>
          <div className="text-2xs uppercase tracking-wide text-text-subtle">
            Orchestrator
          </div>
        </div>
      )}
    </div>
  )
}
