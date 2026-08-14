import { useEffect, useState } from 'react'
import { Sparkles } from 'lucide-react'
import type { KeyPair } from '../api/client'
import { apiErrorMessage, apiErrorStatus } from '../api/client'
import { useGenerateKeyPair } from '../hooks/queries'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert, CopyButton, PathValue, Truncated } from '../ui/Feedback'

/** Generate an ed25519 pair on the backend. */
export function GenerateKeyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const generateMut = useGenerateKeyPair()

  const [name, setName] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [created, setCreated] = useState<KeyPair | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setFormError(null)
    setCreated(null)
  }, [open])

  const canSubmit = name.trim().length > 0 && !generateMut.isPending

  const submit = async () => {
    setFormError(null)
    if (!canSubmit) return
    try {
      setCreated(await generateMut.mutateAsync(name.trim()))
    } catch (err) {
      setFormError(
        apiErrorStatus(err) === 409
          ? `A key pair named "${name.trim()}" already exists.`
          : apiErrorMessage(err),
      )
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={created ? 'Key pair created' : 'Generate key pair'}
      icon={Sparkles}
      size="lg"
      footer={
        created ? (
          <Button intent="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={onClose}>Cancel</Button>
            <Button
              intent="primary"
              disabled={!canSubmit}
              loading={generateMut.isPending}
              onClick={submit}
            >
              Generate
            </Button>
          </>
        )
      }
    >
      {created ? (
        <div className="space-y-4">
          <Field
            label="Private key"
            help={
              <>
                Written to the backend&apos;s filesystem with owner-only permissions. It is{' '}
                <span className="text-text">not downloadable</span> — no endpoint returns a
                private key, now or later. Use it with{' '}
                <code className="data rounded-sm bg-surface px-1 py-0.5">ssh -i</code> from
                the machine running the backend, or copy the file yourself.
              </>
            }
          >
            {() => <PathValue value={created.private_key_path ?? ''} />}
          </Field>

          <Field label="Fingerprint">
            {() => (
              <div className="flex items-center gap-1 rounded border border-border-strong bg-surface px-2.5 py-1.5">
                <code className="min-w-0 flex-1 text-xs text-text">
                  <Truncated value={created.fingerprint} />
                </code>
                <CopyButton value={created.fingerprint} title="Copy fingerprint" />
              </div>
            )}
          </Field>
        </div>
      ) : (
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            void submit()
          }}
        >
          {formError && <Alert tone="danger">{formError}</Alert>}

          <Field
            label="Name"
            help="Creates an ed25519 pair on the backend. The private half stays there; the public half becomes selectable when launching instances."
          >
            {({ id, describedBy }) => (
              <Input
                id={id}
                aria-describedby={describedBy}
                autoFocus
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="deploy-key"
              />
            )}
          </Field>

          <button type="submit" className="hidden" aria-hidden />
        </form>
      )}
    </Modal>
  )
}
