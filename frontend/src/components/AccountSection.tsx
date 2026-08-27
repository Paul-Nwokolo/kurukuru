import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import {
  apiErrorMessage,
  changePassword,
  cliCommand,
  createApiToken,
  getApiTokens,
  logout,
  revokeApiToken,
  setSignOutNotice,
  whoami,
} from '../api/client'
import { Button, IconButton } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert, CopyButton, TimeAgo } from '../ui/Feedback'

/** Mirrors MIN_PASSWORD_LENGTH in backend/kurukuru/models.py. The backend enforces
 *  it; this only avoids making the user submit to find out. */
const MIN_PASSWORD_LENGTH = 12

/**
 * Account, password and API tokens.
 *
 * Lives in Settings rather than behind an avatar menu because this install has
 * one account and the questions it answers — "which token is the CLI using",
 * "did that revoke take" — are administrative, not personal.
 */
export function AccountSection() {
  const { data: user } = useQuery({ queryKey: ['whoami'], queryFn: whoami })

  return (
    <section className="rounded-lg border border-border">
      {/* No sign-out button here: that lives in the header, where identity
          does, and one exit is easier to find than two. This page is for the
          changes worth reading about before making them. */}
      <header className="flex items-start gap-2 border-b border-border px-4 py-3">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-text-muted" aria-hidden />
        <div>
          <h2 className="text-sm font-semibold text-text">Account</h2>
          <p className="mt-0.5 text-xs text-text-subtle">
            Signed in as{' '}
            <span className="data text-text">{user?.username ?? '…'}</span>
            {user?.is_owner && ' · owner'}. Every account has full control of
            every VM — there are no roles.
          </p>
        </div>
      </header>

      <PasswordForm />
      <TokenList />
    </section>
  )
}

function PasswordForm() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => changePassword(current, next),
    onSuccess: () => {
      setCurrent('')
      setNext('')
      setConfirm('')
      // This session died with every other one, so there is no "changed!"
      // state worth rendering — a background poll would 401 within seconds and
      // replace it with the login screen anyway. Go there deliberately, with
      // the explanation, instead of being sent there by a race.
      const relogin = cliCommand('auth login')
      setSignOutNotice(
        'Password changed. Every session and API token was invalidated, ' +
          'including this one.' +
          (relogin ? ` Run \`${relogin}\` again on any machine using the CLI.` : ''),
      )
      void logout()
    },
    onError: (err) => setError(apiErrorMessage(err)),
  })

  const mismatch = confirm.length > 0 && next !== confirm
  const tooShort = next.length > 0 && next.length < MIN_PASSWORD_LENGTH
  const ready =
    current.length > 0 && next.length >= MIN_PASSWORD_LENGTH && next === confirm

  return (
    <form
      className="space-y-3 border-b border-border px-4 py-4"
      onSubmit={(event) => {
        event.preventDefault()
        setError(null)
        mutation.mutate()
      }}
    >
      <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        Change password
      </h3>

      {/* Said before the change, not after. Someone who has memorised only the
          browser flow will otherwise discover their CLI stopped working an
          hour later, with nothing on screen connecting the two. */}
      <p className="text-xs leading-relaxed text-text-subtle">
        Changing the password signs out every session, revokes every API token
        and kills any open console ticket. That is deliberate: you change a
        password when you think something leaked, and a change that leaves the
        old credentials working has not done anything.
      </p>

      {error && <Alert tone="danger">{error}</Alert>}

      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Current password">
          {({ id }) => (
            <Input
              id={id}
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          )}
        </Field>
        <Field
          label="New password"
          error={tooShort ? `At least ${MIN_PASSWORD_LENGTH} characters` : undefined}
        >
          {({ id, describedBy }) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
            />
          )}
        </Field>
        <Field label="Confirm" error={mismatch ? 'Does not match' : undefined}>
          {({ id, describedBy }) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          )}
        </Field>
      </div>

      <Button
        type="submit"
        intent="danger"
        loading={mutation.isPending}
        disabled={!ready || mutation.isPending}
      >
        Change password
      </Button>
    </form>
  )
}

function TokenList() {
  const queryClient = useQueryClient()
  const { data: tokens, isLoading } = useQuery({
    queryKey: ['api-tokens'],
    queryFn: getApiTokens,
  })
  const [name, setName] = useState('')
  const [secret, setSecret] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: () => createApiToken(name.trim()),
    onSuccess: (created) => {
      setSecret(created.token)
      setName('')
      void queryClient.invalidateQueries({ queryKey: ['api-tokens'] })
    },
    onError: (err) => setError(apiErrorMessage(err)),
  })

  const revoke = useMutation({
    mutationFn: revokeApiToken,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['api-tokens'] }),
    onError: (err) => setError(apiErrorMessage(err)),
  })

  return (
    <div className="px-4 py-4">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
        API tokens
      </h3>
      <p className="mt-1 text-xs leading-relaxed text-text-subtle">
        Used by the CLI.{' '}
        {cliCommand('auth login') && (
          <>
            <code className="data">{cliCommand('auth login')}</code> creates one
            and stores it in a file locked to your OS user;{' '}
          </>
        )}
        create one here if you want a second machine or a script to have its own,
        revocable credential.
      </p>

      {error && (
        <Alert tone="danger" className="mt-3">
          {error}
        </Alert>
      )}

      {/* Shown until dismissed, never on a timer. It cannot be retrieved
          again, so hiding it after a few seconds would be the app destroying
          the user's only copy on their behalf. */}
      {secret && (
        <div className="mt-3 space-y-2 rounded-md border border-transitional/30 bg-transitional-quiet p-3">
          <p className="text-xs font-medium text-transitional">
            Copy this now — it is not stored anywhere and cannot be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-surface px-2 py-1.5 data text-xs text-text">
              {secret}
            </code>
            <CopyButton value={secret} title="Copy token" />
          </div>
          <Button size="sm" onClick={() => setSecret(null)}>
            I have copied it
          </Button>
        </div>
      )}

      <form
        className="mt-3 flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          setError(null)
          create.mutate()
        }}
      >
        <Field label="Name" className="flex-1">
          {({ id }) => (
            <Input
              id={id}
              value={name}
              placeholder="laptop-cli"
              onChange={(e) => setName(e.target.value)}
            />
          )}
        </Field>
        <Button
          type="submit"
          icon={Plus}
          loading={create.isPending}
          disabled={!name.trim() || create.isPending}
        >
          Create
        </Button>
      </form>

      <div className="mt-4 overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-subtle">
              <th className="py-2 pr-3 font-medium">Name</th>
              <th className="py-2 pr-3 font-medium">Prefix</th>
              <th className="py-2 pr-3 font-medium">Created</th>
              <th className="py-2 pr-3 font-medium">Last used</th>
              <th className="py-2 pr-3 font-medium">Status</th>
              <th className="w-10 py-2" />
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr>
                <td colSpan={6} className="py-3 text-xs text-text-subtle">
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && (tokens?.length ?? 0) === 0 && (
              <tr>
                <td colSpan={6} className="py-3 text-xs text-text-subtle">
                  No tokens yet.
                </td>
              </tr>
            )}
            {tokens?.map((token) => (
              <tr key={token.id} className="border-b border-border last:border-0">
                <td className="py-2 pr-3 text-text">{token.name}</td>
                <td className="py-2 pr-3 data text-xs text-text-muted">
                  {token.prefix}…
                </td>
                <td className="py-2 pr-3 text-xs text-text-muted">
                  <TimeAgo value={token.created_at} />
                </td>
                {/* "Never" is the interesting value here: a token created and
                    never used is usually one that was pasted somewhere wrong. */}
                <td className="py-2 pr-3 text-xs text-text-muted">
                  {token.last_used_at ? <TimeAgo value={token.last_used_at} /> : 'Never'}
                </td>
                <td className="py-2 pr-3 text-xs">
                  {token.revoked_at ? (
                    <span className="text-text-subtle">
                      Revoked <TimeAgo value={token.revoked_at} />
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-healthy">
                      <KeyRound className="h-3 w-3" aria-hidden />
                      Active
                    </span>
                  )}
                </td>
                <td className="py-2">
                  {!token.revoked_at && (
                    <IconButton
                      icon={Trash2}
                      intent="danger"
                      title={`Revoke ${token.name}`}
                      disabled={revoke.isPending}
                      onClick={() => {
                        setError(null)
                        revoke.mutate(token.id)
                      }}
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
