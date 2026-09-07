/**
 * The lockup. One component, one place.
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
 * you look. It draws in `currentColor` and inherits `text-text`, so there is
 * one mark rather than a light one and a dark one.
 */
import { PRODUCT_NAME, PRODUCT_TAGLINE } from './product'

/*
 * Lockup geometry, from the design spec.
 *
 * Every number below is a ratio, and every ratio is expressed against the
 * *mark's interior box* — the 76×76 the artwork actually occupies inside its
 * 100×100 viewBox, the remaining 12% a side being the safe zone. So `size` is
 * the rendered viewBox, and MARK (0.76 of it) is what the spec calls the mark
 * height. Doing the arithmetic here rather than in a stack of Tailwind classes
 * is what lets one `size` prop scale the whole lockup and keep it in
 * proportion, which is the entire point of a spec written in ratios.
 */

/** Fraction of the viewBox the artwork occupies. 100 − (2 × 12% safe zone). */
const INTERIOR = 0.76

/** Wordmark size, as a fraction of mark height. */
const WORDMARK = 0.79

/** Mark-to-wordmark gap, as a fraction of mark width — measured from the
 *  artwork's edge, not the SVG box, so the safe zone comes back off it. */
const GAP = 0.26

/** Descriptor size, as a fraction of the wordmark size. */
const DESCRIPTOR = 0.23

/** Descriptor baseline below the wordmark baseline, in interior units. */
const DESCRIPTOR_DROP = 26 / 76

/**
 * Where the wordmark baseline sits, in interior units from the artwork's top.
 *
 * The spec says "the upper edge of the mark's lowest blocks". The two shapes
 * that reach the bottom start at different heights — the left column's third
 * block at y=46, the wedge at y=42 — so there is no single edge to read it
 * off. This takes 46: the left column is three flat-topped rectangles whose
 * edges are unambiguous, while the wedge's "upper edge" is a 20-wide stub on a
 * diagonal. The difference is 4 of 76 units, about 1px at sidebar size.
 */
const BASELINE = 46 / 76

/**
 * Distance from the top of a `line-height: 1` line box to the alphabetic
 * baseline, as a fraction of font size — for Space Grotesk specifically.
 *
 * Needed because the descriptor's offset is specified baseline-to-baseline and
 * CSS stacks boxes, not baselines. Measured in the browser rather than read off
 * the font's tables, because what matters is where the browser actually puts
 * the baseline: at 1000px, Space Grotesk's sits 846px down a `line-height: 1`
 * box, and the ratio holds for the descriptor's weight too.
 *
 * Measured *large* on purpose. Browsers snap the baseline to a whole pixel, so
 * at UI sizes the realised ratio drifts — 20.000px on a 24.016px wordmark is
 * 0.833, not 0.846 — and reading it off a small sample would bake one size's
 * rounding into every size. The cost of the rounding is that the descriptor's
 * drop lands within about half a pixel of the spec rather than exactly on it,
 * which is not worth a fudge factor that would misdescribe the geometry.
 */
const BASELINE_IN_LINE_BOX = 0.846

/** The artwork. Ratios above assume this exact geometry. */
function Mark({ size }: { size: number }) {
  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      fill="currentColor"
      aria-hidden
      focusable="false"
      style={{ position: 'absolute', top: 0, left: 0 }}
    >
      <g transform="translate(12, 12)">
        <rect x="0" y="0" width="30" height="24" />
        <rect x="0" y="30" width="30" height="10" />
        <rect x="0" y="46" width="30" height="30" />
        <rect x="36" y="0" width="40" height="16" />
        <rect x="36" y="22" width="20" height="14" />
        <path d="M36 42 h20 l20 34 h-30 Z" />
      </g>
    </svg>
  )
}

export function Wordmark({
  /** Rendered size of the mark's viewBox, in px. Everything else follows. */
  size = 40,
  /** Mark only. The name is not shown, so the descriptor is not owed one. */
  compact = false,
}: {
  size?: number
  compact?: boolean
}) {
  const mark = size * INTERIOR
  const wordmark = mark * WORDMARK
  const descriptor = wordmark * DESCRIPTOR

  // The spec's gap is from the artwork to the wordmark. The SVG already
  // carries 12% of empty safe zone on that side, so the CSS gap is what is
  // left after it — otherwise the lockup reads as too loose by exactly the
  // padding built into the file.
  const gap = mark * GAP - size * 0.12

  // Baseline-to-baseline, converted to the margin CSS actually needs: the
  // wordmark's box extends (1 - k) of its size below its own baseline, and the
  // descriptor's baseline sits k of its size below the top of its box.
  const drop = mark * DESCRIPTOR_DROP
  const descriptorMargin =
    drop - wordmark * (1 - BASELINE_IN_LINE_BOX) - descriptor * BASELINE_IN_LINE_BOX

  return (
    <div className="flex items-baseline" style={{ gap: `${gap}px` }}>
      {/*
        The wrapper is deliberately shorter than the mark it contains, and the
        mark is positioned out of flow so it overflows.

        That is what puts the wordmark on the right baseline without knowing a
        single font metric: a box with no in-flow line of its own takes its
        baseline from its bottom edge, so cutting the wrapper off at the
        spec's baseline height and letting `align-items: baseline` do the rest
        is exact by construction, at any size, in any font.
      */}
      <div
        className="relative shrink-0 text-text"
        style={{ width: `${size}px`, height: `${size * INTERIOR * BASELINE + size * 0.12}px` }}
      >
        <Mark size={size} />
      </div>

      {!compact && (
        <div className="min-w-0">
          <div
            className="text-text"
            style={{
              fontSize: `${wordmark}px`,
              lineHeight: 1,
              fontWeight: 700,
              letterSpacing: '-0.025em',
            }}
          >
            {PRODUCT_NAME}
          </div>
          <div
            className="uppercase text-text-subtle"
            style={{
              fontSize: `${descriptor}px`,
              lineHeight: 1,
              fontWeight: 600,
              letterSpacing: '0.25em',
              marginTop: `${descriptorMargin}px`,
            }}
          >
            {PRODUCT_TAGLINE}
          </div>
        </div>
      )}
    </div>
  )
}
