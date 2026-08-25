import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { onUnauthenticated, refreshCsrfToken, whoami } from '../api/client'
import type { User } from '../api/client'
import { LoginScreen } from './LoginScreen'

/**
 * Renders the app only when there is a session, and the login screen otherwise.
 *
 * Three things it has to get right, all of which are about *not* showing the
 * wrong thing:
 *
 * 1. **On load, ask.** The session cookie is httpOnly, so the page cannot look
 *    at it — the only way to know whether we are signed in is to call the API.
 *    Until that answers, neither the app nor the login form is correct, so
 *    neither is rendered.
 * 2. **Recover the CSRF token.** A reload keeps the cookie and loses the
 *    in-memory token, which would leave the dashboard able to read and unable
 *    to do anything — the most confusing possible state. It is re-fetched.
 * 3. **Expiry can arrive from anywhere.** A background poll is as likely as a
 *    click to be the request that discovers the session is gone. The client
 *    publishes 401s, this subscribes once, and the cached data is dropped so
 *    the next session cannot see the previous one's rows.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<User | null>(null)
  const [checked, setChecked] = useState(false)

  const establish = useCallback(async () => {
    const me = await whoami()
    // The cookie survives a reload; the CSRF token does not. Without this the
    // dashboard would load, list everything, and fail every button.
    await refreshCsrfToken().catch(() => undefined)
    setUser(me)
  }, [])

  useEffect(() => {
    let cancelled = false
    establish()
      .catch(() => undefined) // 401 here simply means "show the login screen"
      .finally(() => {
        if (!cancelled) setChecked(true)
      })
    return () => {
      cancelled = true
    }
  }, [establish])

  useEffect(
    () =>
      onUnauthenticated(() => {
        setUser(null)
        // Anything cached belonged to the session that just ended.
        queryClient.clear()
      }),
    [queryClient],
  )

  // Deliberately blank rather than a spinner: the check is one local request,
  // and a flash of "loading" followed by a login form reads worse than a beat
  // of nothing.
  if (!checked) return <div className="min-h-screen bg-bg" />

  if (!user) {
    return (
      <LoginScreen
        onSignedIn={() => {
          void establish().then(() => queryClient.invalidateQueries())
        }}
      />
    )
  }

  return <>{children}</>
}
