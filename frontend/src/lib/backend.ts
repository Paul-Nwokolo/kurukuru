/**
 * Whether the backend is reachable, and — when it is not — why.
 *
 * ## One source of truth
 *
 * There used to be two. The header badge read the `/health` poll; the banner
 * read the same poll's `isError`. That sounds like one source, and it was the
 * problem: `/health` is polled every 15 seconds, so for up to fifteen seconds
 * after the backend goes away the badge still says "Backend online" while a
 * request that just failed is showing a network error two inches below it. A
 * product contradicting itself is worse than a product admitting it does not
 * know.
 *
 * So reachability is derived from *everything the app has actually tried*: the
 * health poll, plus any query or mutation in the cache currently sitting on a
 * network-level error. Any component that wants to show backend state calls
 * `useBackendStatus` and they cannot disagree, because there is nothing to
 * keep in step.
 *
 * ## Telling "nothing listening" from "origin refused"
 *
 * The browser will not tell you. A connection refused and a CORS rejection
 * both arrive as an XHR error with no response, no status and no message worth
 * reading — which is why this is worth the code: the two have completely
 * different fixes, and guessing wrong costs an afternoon.
 *
 * The discriminator is a `mode: 'no-cors'` fetch. It opts out of the CORS
 * check entirely, so the response is opaque and unreadable — but whether the
 * promise *resolves* is the answer:
 *
 *   resolves → a server answered. Something is listening; the original failure
 *              was the origin policy, not the port.
 *   rejects  → nothing answered. Nothing is listening on that port at all.
 *
 * The common cause of the first is mundane and invisible: Vite takes the next
 * free port when 5173 is busy, the page ends up on 5174, and the backend's
 * allowed-origins list does not mention it. Every request then fails with a
 * message that names none of that.
 */
import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { AxiosError } from 'axios'
import { API_URL } from '../api/client'

/** Why the backend cannot be reached. */
export type Unreachable =
  /** Still working it out — the probe is in flight. */
  | 'checking'
  /** Nothing answered on the port at all. */
  | 'no-listener'
  /** Something answered, but this page's origin is not allowed. */
  | 'origin-refused'
  /** Listening, but too slow to answer. */
  | 'not-responding'

export interface BackendStatus {
  online: boolean
  /** Null while online. */
  reason: Unreachable | null
  /** One sentence for the badge's tooltip and the banner's body. */
  summary: string
  /** What to do about it, when there is something specific to say. */
  remedy: string | null
}

/**
 * A failure that never reached the application layer.
 *
 * `response === undefined` is the test: an HTTP 500 is the backend answering,
 * and answering badly is not being unreachable. Only a request that produced
 * no response at all counts.
 */
function isNetworkError(error: unknown): boolean {
  const axiosError = error as AxiosError | null
  return Boolean(axiosError?.isAxiosError) && axiosError?.response === undefined
}

function isTimeout(error: unknown): boolean {
  const code = (error as AxiosError | null)?.code
  return code === 'ECONNABORTED' || code === 'ETIMEDOUT'
}

/**
 * Ask whether *anything* is listening, without asking permission to read it.
 *
 * `no-cors` means the browser sends the request and hands back an opaque
 * response it will not let us look at. We do not need to look at it. That it
 * arrived is the whole answer.
 *
 * `cache: 'no-store'` so a previous success cannot answer for the present.
 */
async function somethingIsListening(signal: AbortSignal): Promise<boolean> {
  try {
    await fetch(`${API_URL}/health`, {
      mode: 'no-cors',
      cache: 'no-store',
      signal,
    })
    return true
  } catch {
    return false
  }
}

export function useBackendStatus(): BackendStatus {
  const client = useQueryClient()
  const [failure, setFailure] = useState<{ timedOut: boolean } | null>(null)
  const [listening, setListening] = useState<boolean | null>(null)

  // Watch every query and mutation, not just the health poll. This is the
  // "one source of truth" half: a network error anywhere is a network error,
  // and the badge has no business staying green through one.
  useEffect(() => {
    const queries = client.getQueryCache()
    const mutations = client.getMutationCache()

    const scan = () => {
      const errors = [
        ...queries.getAll().map((query) => query.state.error),
        ...mutations.getAll().map((mutation) => mutation.state.error),
      ].filter(isNetworkError)
      // A timeout only counts as "the backend is slow" if nothing else failed
      // outright — one refused connection is the more serious diagnosis.
      setFailure(
        errors.length === 0 ? null : { timedOut: errors.every(isTimeout) },
      )
    }

    scan()
    const unsubscribeQueries = queries.subscribe(scan)
    const unsubscribeMutations = mutations.subscribe(scan)
    return () => {
      unsubscribeQueries()
      unsubscribeMutations()
    }
  }, [client])

  // Probe only while something is actually failing, and re-probe each time a
  // new failure starts — a backend that came back up, or an origin that got
  // fixed, must not be reported from a stale answer.
  useEffect(() => {
    if (failure === null) {
      setListening(null)
      return
    }
    const controller = new AbortController()
    setListening(null)
    void somethingIsListening(controller.signal).then((result) => {
      if (!controller.signal.aborted) setListening(result)
    })
    return () => controller.abort()
  }, [failure])

  if (failure === null) {
    return {
      online: true,
      reason: null,
      summary: 'Backend online',
      remedy: null,
    }
  }

  if (listening === null) {
    return {
      online: false,
      reason: 'checking',
      summary: `Cannot reach ${API_URL} — working out why…`,
      remedy: null,
    }
  }

  if (!listening) {
    return {
      online: false,
      reason: 'no-listener',
      summary: `Nothing is listening on ${API_URL}.`,
      remedy: 'Start the backend, or point VITE_API_URL at the one you meant.',
    }
  }

  if (failure.timedOut) {
    return {
      online: false,
      reason: 'not-responding',
      summary: `${API_URL} is listening but did not answer in time.`,
      remedy: 'A long hypervisor operation can block it — check the backend log.',
    }
  }

  return {
    online: false,
    reason: 'origin-refused',
    summary: `${API_URL} is running, but it refused this page's origin (${window.location.origin}).`,
    // The overwhelmingly common cause: Vite moved to the next free port
    // because 5173 was taken, and the backend has never heard of the new one.
    remedy:
      `Add ${window.location.origin} to IAAS_CORS_ORIGINS and restart the ` +
      `backend — or serve this page from an allowed origin.`,
  }
}
