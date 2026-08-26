import { useEffect, useRef, useState } from 'react'
import { LogIn, ShieldCheck, Terminal } from 'lucide-react'
import { apiErrorMessage, cliCommand, login, takeSignOutNotice } from '../api/client'
import type { FirstRunStatus } from '../api/client'
import { Button } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert } from '../ui/Feedback'
import { PRODUCT_NAME, PRODUCT_TAGLINE } from '../ui/product'

/**
 * The sign-in screen, and the only thing rendered when there is no session.
 *
 * It handles two states that look identical to a naive client and mean very
 * different things:
 *
 *   - **no account exists yet** — the install has never been set up, and the
 *     answer is a command on the host, not a password. Showing a login form
 *     here would ask for a credential that cannot exist.
 *   - **not signed in** — an ordinary login.
 *
 * A third, "backend down", is distinguished too: an unreachable API cannot
 * report either of the above, and telling someone their password was wrong when
 * the server is off is the worst of the three messages.
 */
export function LoginScreen({
  /** Null when `/auth/first-run` did not answer — that is the "backend
   *  unreachable" case, and it is reported as itself. */
  status,
  onSignedIn,
}: {
  status: FirstRunStatus | null
  onSignedIn: () => void
}) {
  const [username, setUsername] = useState('owner')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Read once, on mount, and cleared as it is read: whatever put us here is
  // true now and will not be true of the next visit to this screen.
  const [notice] = useState(takeSignOutNotice)
  const passwordRef = useRef<HTMLInputElement>(null)

  const reachable = status !== null
  const configured = status?.configured ?? null
  const initCommand = cliCommand('auth init')
  const resetCommand = cliCommand('auth reset-password')

  useEffect(() => {
    if (configured) passwordRef.current?.focus()
  }, [configured])

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await login(username.trim(), password)
      setPassword('')
      onSignedIn()
    } catch (e) {
      setError(apiErrorMessage(e))
      setPassword('')
      passwordRef.current?.focus()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-2 text-center">
          <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-surface ring-1 ring-border">
            <ShieldCheck className="h-5 w-5 text-text-muted" aria-hidden />
          </div>
          <h1 className="text-lg font-semibold tracking-tight text-text">
            {PRODUCT_NAME}
          </h1>
          <p className="text-2xs uppercase tracking-wide text-text-subtle">
            {PRODUCT_TAGLINE}
          </p>
          <p className="text-sm text-text-muted">
            {configured === false
              ? 'This install has no account yet.'
              : 'Sign in to manage your virtual machines.'}
          </p>
        </div>

        {notice && <Alert tone="transitional">{notice}</Alert>}

        {!reachable && (
          <Alert tone="danger">
            Cannot reach the backend. Start it, then reload — this is not a
            password problem.
          </Alert>
        )}

        {/* No account: a password would be the wrong question entirely. */}
        {reachable && configured === false ? (
          <div className="space-y-4 rounded-xl border border-border bg-surface p-5">
            <div className="flex items-start gap-3">
              <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-text-muted" aria-hidden />
              <div className="space-y-2 text-sm text-text-muted">
                <p>
                  Create the owner account on the machine running the backend:
                </p>
                <pre className="overflow-x-auto rounded-lg bg-bg px-3 py-2 font-mono text-xs text-text">
                  {initCommand}
                </pre>
                <p>
                  It is done on the host rather than here on purpose: a setup
                  page reachable over the network would hand ownership of every
                  VM to whoever opened it first.
                </p>
              </div>
            </div>
            <Button className="w-full" onClick={() => window.location.reload()}>
              I have run it — reload
            </Button>
          </div>
        ) : (
          reachable && (
            <form
              className="space-y-4 rounded-xl border border-border bg-surface p-5"
              onSubmit={(event) => {
                event.preventDefault()
                void submit()
              }}
            >
              {error && <Alert tone="danger">{error}</Alert>}

              <Field label="Username">
                {({ id, describedBy }) => (
                  <Input
                    id={id}
                    aria-describedby={describedBy}
                    value={username}
                    autoComplete="username"
                    onChange={(e) => setUsername(e.target.value)}
                    disabled={busy}
                  />
                )}
              </Field>

              <Field label="Password">
                {({ id, describedBy }) => (
                  <Input
                    id={id}
                    aria-describedby={describedBy}
                    ref={passwordRef}
                    type="password"
                    value={password}
                    autoComplete="current-password"
                    onChange={(e) => setPassword(e.target.value)}
                    disabled={busy}
                  />
                )}
              </Field>

              <Button
                type="submit"
                intent="primary"
                icon={LogIn}
                className="w-full"
                loading={busy}
                disabled={busy || !password}
              >
                Sign in
              </Button>
            </form>
          )
        )}

        {/* Omitted rather than guessed when the backend has not told us what
            the command is called — instructions naming a command that does not
            exist are worse than no instructions. */}
        {resetCommand && (
          <p className="text-center text-xs text-text-muted">
            Forgotten the password? Run{' '}
            <code className="font-mono">{resetCommand}</code> on the host.
          </p>
        )}
      </div>
    </div>
  )
}
