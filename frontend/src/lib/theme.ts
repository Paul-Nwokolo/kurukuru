import { useCallback, useSyncExternalStore } from 'react'

/**
 * Theme selection: dark (default), light, or follow the system.
 *
 * The value is applied by stamping `data-theme` on `<html>`, which is what
 * src/styles/tokens.css keys its semantic layer off. Nothing else in the app
 * knows which theme is active — components reference semantic tokens and the
 * assignment changes underneath them.
 *
 * "System" is a real third choice rather than just the initial default: a
 * machine that switches at sunset should take the dashboard with it, and that
 * only works if we keep listening.
 *
 * The *first paint* is handled in index.html by an inline script, not here.
 * React mounting is far too late — the page would paint dark, then correct
 * itself, which is the flash this design is required not to have.
 */
export type ThemeChoice = 'dark' | 'light' | 'system'

const STORAGE_KEY = 'iaas.theme'

const query = () =>
  typeof window !== 'undefined' && window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: light)')
    : null

function read(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark' || stored === 'light' || stored === 'system') return stored
  } catch {
    /* private mode; fall through to the default */
  }
  return 'system'
}

let choice: ThemeChoice = read()
const listeners = new Set<() => void>()

/** The theme actually rendered, once "system" is resolved. */
export function resolveTheme(value: ThemeChoice): 'dark' | 'light' {
  if (value !== 'system') return value
  return query()?.matches ? 'light' : 'dark'
}

function apply(): void {
  const root = document.documentElement
  // Colour transitions are suppressed across the swap — see the
  // .theme-switching rule in index.css for why this is a correctness fix and
  // not a polish one.
  root.classList.add('theme-switching')
  root.setAttribute('data-theme', resolveTheme(choice))

  // Cleared two ways, and the second is not redundant.
  //
  // requestAnimationFrame is the right trigger when the page is visible: it
  // removes the class the moment the new values have painted, so a hover
  // transition a fraction of a second later still animates.
  //
  // But rAF does not run in a background tab. A theme change while hidden —
  // the OS flipping to dark at sunset with the dashboard on another tab, which
  // is exactly what the "system" option is for — would leave the class on
  // forever and silently kill every transition in the app. The timeout is the
  // guarantee; the rAF is the optimisation. Both are idempotent.
  const clear = () => root.classList.remove('theme-switching')
  requestAnimationFrame(() => requestAnimationFrame(clear))
  window.setTimeout(clear, 120)
}

export function getThemeChoice(): ThemeChoice {
  return choice
}

export function setThemeChoice(next: ThemeChoice): void {
  choice = next
  try {
    window.localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* see read() */
  }
  apply()
  listeners.forEach((listener) => listener())
}

// Follow the OS while the choice is "system". Registered once, at module load.
query()?.addEventListener('change', () => {
  if (choice === 'system') {
    apply()
    listeners.forEach((listener) => listener())
  }
})

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** `[choice, resolved, setChoice]` — the choice, what it resolves to now. */
export function useTheme(): [ThemeChoice, 'dark' | 'light', (next: ThemeChoice) => void] {
  const value = useSyncExternalStore(subscribe, getThemeChoice, () => 'dark' as ThemeChoice)
  const set = useCallback((next: ThemeChoice) => setThemeChoice(next), [])
  return [value, resolveTheme(value), set]
}
