import { useEffect, useRef, useState } from 'react'
import { LogIn } from 'lucide-react'
import {
  apiErrorMessage,
  cliCommand,
  createFirstAccount,
  login,
  takeSignOutNotice,
} from '../api/client'
import type { FirstRunStatus } from '../api/client'
import { Button } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert } from '../ui/Feedback'
import { Wordmark } from '../ui/Wordmark'

/** Mirrors MIN_PASSWORD_LENGTH in backend/kurukuru/models.py, which is what
 *  actually enforces it — this only makes the form say what is missing. */
const MIN_PASSWORD_LENGTH = 12

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
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Read once, on mount, and cleared as it is read: whatever put us here is
  // true now and will not be true of the next visit to this screen.
  const [notice] = useState(takeSignOutNotice)
  const passwordRef = useRef<HTMLInputElement>(null)

  const reachable = status !== null
  const configured = status?.configured ?? null
  // Checked here only to keep the button honest and say what is missing. The
  // backend enforces the same minimum through the same validator, so this can
  // never be the only thing standing between a short password and an account.
  const tooShort = password.length < MIN_PASSWORD_LENGTH
  const mismatched = confirm !== password
  const initCommand = cliCommand('auth init')
  const resetCommand = cliCommand('auth reset-password')

  useEffect(() => {
    // Either screen puts the cursor where the typing starts: the password on
    // a login, and the password on setup too, since the username is prefilled.
    if (configured !== null) passwordRef.current?.focus()
  }, [configured])

  async function createOwner() {
    setBusy(true)
    setError(null)
    try {
      await createFirstAccount(username.trim(), password)
      setPassword('')
      setConfirm('')
      // The backend signs the new owner in, so there is no second step: going
      // straight through is what stops it reading like the account was not
      // created and then asking for the password typed ten seconds ago.
      onSignedIn()
    } catch (e) {
      setError(apiErrorMessage(e))
      setPassword('')
      setConfirm('')
      passwordRef.current?.focus()
    } finally {
      setBusy(false)
    }
  }

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
        <div className="space-y-4 text-center">
          {/* The one place with room for the full lockup at the size the design
              spec's own ratios were drawn for — the descriptor is legible here
              and nowhere else in the app. See Wordmark for the measurements. */}
          <div className="flex justify-center">
            <Wordmark size={72} />
          </div>
          <p className="text-sm text-text-muted">
            {configured === false
              ? 'Create the owner account for this install.'
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

        {/* No account yet: this is the setup form, not a login form. Asking for
            a password here would be asking for a credential that cannot exist. */}
        {reachable && configured === false ? (
          <form
            className="space-y-4 rounded-xl border border-border bg-surface p-5"
            onSubmit={(event) => {
              event.preventDefault()
              void createOwner()
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

            <Field
              label="Password"
              help={`At least ${MIN_PASSWORD_LENGTH} characters. There are no character rules — length is what makes a password hard to guess.`}
            >
              {({ id, describedBy }) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  ref={passwordRef}
                  type="password"
                  value={password}
                  autoComplete="new-password"
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={busy}
                />
              )}
            </Field>

            <Field label="Confirm password">
              {({ id, describedBy }) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  type="password"
                  value={confirm}
                  autoComplete="new-password"
                  onChange={(e) => setConfirm(e.target.value)}
                  disabled={busy}
                />
              )}
            </Field>

            {password.length > 0 && tooShort && (
              <p className="text-xs text-text-muted">
                {MIN_PASSWORD_LENGTH - password.length} more character
                {MIN_PASSWORD_LENGTH - password.length === 1 ? '' : 's'} needed.
              </p>
            )}
            {confirm.length > 0 && mismatched && (
              <p className="text-xs text-danger">The two passwords do not match.</p>
            )}

            <Button
              type="submit"
              intent="primary"
              icon={LogIn}
              className="w-full"
              loading={busy}
              disabled={busy || tooShort || mismatched || !confirm}
            >
              Create account
            </Button>

            <p className="text-xs text-text-subtle">
              This form works only from the machine running the backend, and
              only until an account exists. Reaching it already means access to
              this machine — which is the same thing running{' '}
              {initCommand ? <code className="font-mono">{initCommand}</code> : 'the setup command'}{' '}
              in a terminal would require.
            </p>
          </form>
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
