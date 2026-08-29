import { useCallback, useSyncExternalStore } from 'react'

/**
 * The project the dashboard is currently scoped to.
 *
 * `null` means **all projects**, and it is the default. A scoping control that
 * started narrow would hide instances from someone who never asked to be
 * scoped, and "where did my VMs go" is a worse first experience than a list
 * that is longer than necessary.
 *
 * Held outside React and read through `useSyncExternalStore` so the header
 * selector and every table stay in step without a provider wrapping the app —
 * one value, one subscription, no context plumbing through components that do
 * not care.
 *
 * Persisted by **id**, not name. A renamed project keeps its selection; a
 * project deleted in another tab resolves to nothing and falls back to all
 * projects rather than filtering everything away.
 */
const STORAGE_KEY = 'kurukuru.project'
const LEGACY_STORAGE_KEY = 'iaas.project'

/**
 * Move a value written under the pre-Phase-16 key, once.
 *
 * The key had to change with the product's name, but a key change is a *silent
 * data loss* for anything stored under it: the setting does not fail, it simply
 * reverts to a default and the user re-picks it wondering why. Carrying the
 * value across costs four lines, so the old key is read, rewritten under the
 * new one, and removed — after which this is a no-op forever.
 */
function migrateKey(from: string, to: string): void {
  try {
    const stored = window.localStorage.getItem(from)
    if (stored !== null && window.localStorage.getItem(to) === null) {
      window.localStorage.setItem(to, stored)
    }
    if (stored !== null) window.localStorage.removeItem(from)
  } catch {
    /* private mode, or storage disabled; nothing to carry across */
  }
}

migrateKey(LEGACY_STORAGE_KEY, STORAGE_KEY)

let current: string | null = read()
const listeners = new Set<() => void>()

function read(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || null
  } catch {
    // Private mode, or storage disabled. Scoping is a convenience; losing it
    // must not stop the dashboard loading.
    return null
  }
}

export function getSelectedProject(): string | null {
  return current
}

export function setSelectedProject(id: string | null): void {
  current = id
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* see read() */
  }
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The selected project id, or null for all projects. */
export function useSelectedProject(): [string | null, (id: string | null) => void] {
  const value = useSyncExternalStore(subscribe, getSelectedProject, () => null)
  const set = useCallback((id: string | null) => setSelectedProject(id), [])
  return [value, set]
}
