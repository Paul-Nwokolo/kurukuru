/**
 * One breakpoint, defined once.
 *
 * The app is a desktop console and stays one — the target is a smaller laptop
 * or an unmaximised window, not a phone. Below this width the sidebar stops
 * being a column and becomes a drawer; above it, nothing changes at all.
 *
 * 1024px is Tailwind's `lg`, and it is picked because it is where the fixed
 * 14rem sidebar plus a six-column instances table stops fitting rather than
 * because it is a common device width.
 *
 * The value is duplicated as a media query string and as a Tailwind prefix at
 * the call sites, which is the one thing worth watching here: if this number
 * changes, the `lg:` prefixes in App/Sidebar/Header have to change with it.
 * Tailwind cannot read a TS constant, and a hand-rolled arbitrary variant
 * (`[@media(min-width:1024px)]:`) at every call site was worse to read than the
 * comment saying to keep them in step.
 */
import { useEffect, useState } from 'react'

export const DESKTOP_QUERY = '(min-width: 1024px)'

/**
 * Whether the viewport is wide enough for the sidebar to be a column.
 *
 * Needed in JS as well as in CSS because two things cannot be expressed with a
 * media query alone: whether the off-canvas drawer should be `inert` (an
 * off-screen sidebar that is still tabbable sends focus somewhere invisible),
 * and whether the backdrop should exist at all.
 */
export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window === 'undefined' || window.matchMedia(DESKTOP_QUERY).matches,
  )

  useEffect(() => {
    const query = window.matchMedia(DESKTOP_QUERY)
    const sync = () => setIsDesktop(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  return isDesktop
}
