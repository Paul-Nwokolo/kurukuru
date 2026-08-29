/**
 * The lockup. One component, one place, deliberately plain.
 *
 * The name and its descriptor come from `src/ui/product.ts`, which is the only
 * file allowed to spell them; nothing else in the application draws a logo, so
 * changing the identity is those two files rather than a hunt through a
 * sidebar, a header and a favicon.
 *
 * **Plain is a requirement here, not a taste.** The descriptor is rendered with
 * the name rather than under an optional prop, and the mark stays abstract —
 * see `product.ts` for the trademark reasoning behind both.
 *
 * The mark is monochrome on purpose. It used to be green, which was also the
 * colour of a healthy instance and of the primary button; a brand that shares
 * a colour with a status is a brand that changes meaning depending on where
 * you look.
 */
import { PRODUCT_NAME, PRODUCT_TAGLINE } from './product'

export function Wordmark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div
        aria-hidden
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border-strong bg-surface-raised"
      >
        {/* Four squares for a grid of machines. Abstract on purpose: nothing
            figurative, nothing that could read as a game character. */}
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5 text-text" fill="currentColor">
          <rect x="1" y="1" width="6" height="6" rx="1" />
          <rect x="9" y="1" width="6" height="6" rx="1" opacity="0.55" />
          <rect x="1" y="9" width="6" height="6" rx="1" opacity="0.55" />
          <rect x="9" y="9" width="6" height="6" rx="1" opacity="0.28" />
        </svg>
      </div>
      {!compact && (
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight text-text">
            {PRODUCT_NAME}
          </div>
          <div className="text-2xs uppercase tracking-wide text-text-subtle">
            {PRODUCT_TAGLINE}
          </div>
        </div>
      )}
    </div>
  )
}
