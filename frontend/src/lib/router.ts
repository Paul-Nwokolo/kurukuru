/**
 * A small router: one route per tab, plus `/instances/:id`.
 *
 * The brief allowed react-router and asked for minimal. The registry was
 * unreachable when this was written, so the dependency was not worth blocking
 * on for `useLocation` and `<Link>`.
 *
 * It is deliberately shaped like the small part of react-router's API that
 * gets used here (`usePath`, `navigate`, and `<Link>` in components/Link.tsx),
 * so swapping in the real thing later is a change of import rather than a
 * rewrite of every caller.
 *
 * The one non-obvious bit: `navigate` emits an event, because `pushState` does
 * not fire `popstate` and components would otherwise not re-render.
 */
import { useEffect, useState } from 'react'

const NAVIGATION_EVENT = 'app:navigate'

/** Views the sidebar can switch between. */
export type NavKey =
  | 'instances'
  | 'images'
  | 'isos'
  | 'keypairs'
  | 'volumes'
  | 'networks'
  | 'projects'
  | 'activity'
  | 'settings'

/**
 * Every tab, its URL, and the title the header shows.
 *
 * The tabs used to be `useState` in App, which meant the URL never moved: a
 * reload dropped you back on Instances, Back left the app entirely, and there
 * was no way to send someone a link to the Networks page. One table now, so
 * the sidebar, the header title and the address bar cannot disagree — they are
 * all reading the same row.
 *
 * Instances is `/` rather than `/instances` because it is the landing page and
 * a bookmark of the app should be a bookmark of it. `/instances` is accepted
 * as well, since it is what people type.
 */
export const VIEWS: Record<NavKey, { path: string; title: string }> = {
  instances: { path: '/', title: 'Instances' },
  images: { path: '/images', title: 'Images' },
  isos: { path: '/isos', title: 'ISOs' },
  volumes: { path: '/volumes', title: 'Volumes' },
  networks: { path: '/networks', title: 'Networks' },
  keypairs: { path: '/keypairs', title: 'Key pairs' },
  projects: { path: '/projects', title: 'Projects' },
  activity: { path: '/activity', title: 'Activity' },
  settings: { path: '/settings', title: 'Settings' },
}

/** Push a new path and tell the app about it. */
export function navigate(path: string): void {
  if (path === window.location.pathname + window.location.search) return
  window.history.pushState({}, '', path)
  window.dispatchEvent(new Event(NAVIGATION_EVENT))
}

/** The current pathname, re-rendering on back/forward and on `navigate`. */
export function usePath(): string {
  const [path, setPath] = useState(() => window.location.pathname)

  useEffect(() => {
    const sync = () => setPath(window.location.pathname)
    // popstate covers the browser's back/forward; the custom event covers our
    // own pushState, which deliberately does not fire popstate.
    window.addEventListener('popstate', sync)
    window.addEventListener(NAVIGATION_EVENT, sync)
    return () => {
      window.removeEventListener('popstate', sync)
      window.removeEventListener(NAVIGATION_EVENT, sync)
    }
  }, [])

  return path
}

/** Match `/instances/:id`, returning the id or null. */
export function instanceIdFromPath(path: string): string | null {
  const match = /^\/instances\/([^/]+)\/?$/.exec(path)
  return match ? decodeURIComponent(match[1]) : null
}

/**
 * Which tab a path selects.
 *
 * Anything unrecognised lands on Instances, which is the same place a fresh
 * visit lands — a typo in the address bar should not be a dead end in an app
 * this small.
 */
export function viewFromPath(path: string): NavKey {
  const normalised = path.length > 1 ? path.replace(/\/+$/, '') : path
  if (normalised === '/instances') return 'instances'
  for (const [key, view] of Object.entries(VIEWS)) {
    if (view.path === normalised) return key as NavKey
  }
  return 'instances'
}
