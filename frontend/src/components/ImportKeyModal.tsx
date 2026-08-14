import { useEffect, useState } from 'react'
import { KeyRound } from 'lucide-react'
import { apiErrorMessage, apiErrorStatus } from '../api/client'
import { useImportKeyPair } from '../hooks/queries'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Field, Input, Textarea } from '../ui/Field'
import { Alert } from '../ui/Feedback'

/** Import a public key you already have. */
export function ImportKeyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const importMut = useImportKeyPair()

  const [name, setName] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setPublicKey('')
    setFormError(null)
  }, [open])

  const canSubmit = name.trim().length > 0 && publicKey.trim().length > 0 && !importMut.isPending

  // Cheap client-side sanity so the obvious mistake is caught before a round
  // trip. The backend is still the authority — it parses the blob properly.
  const looksPrivate = publicKey.trimStart().startsWith('-----BEGIN')

  const submit = async () => {
    setFormError(null)
    if (!canSubmit || looksPrivate) return
    try {
      await importMut.mutateAsync({ name: name.trim(), public_key: publicKey.trim() })
      onClose()
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
      title="Import key pair"
      icon={KeyRound}
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            intent="primary"
            disabled={!canSubmit || looksPrivate}
            loading={importMut.isPending}
            onClick={submit}
          >
            Import
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        {formError && <Alert tone="danger">{formError}</Alert>}

        <Field label="Name">
          {({ id }) => (
            <Input
              id={id}
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="my-laptop"
            />
          )}
        </Field>

        <Field
          label="Public key"
          error={
            looksPrivate
              ? 'That is a private key. Paste the matching .pub file instead — a private key must never leave your machine.'
              : undefined
          }
          help={
            <>
              The contents of{' '}
              <code className="data rounded-sm bg-surface px-1 py-0.5">
                ~/.ssh/id_ed25519.pub
              </code>{' '}
              — the file ending in <span className="text-text">.pub</span>. Supported:
              ed25519, rsa, ecdsa.
            </>
          }
        >
          {({ id, describedBy }) => (
            <Textarea
              id={id}
              aria-describedby={describedBy}
              data
              rows={4}
              spellCheck={false}
              value={publicKey}
              onChange={(event) => setPublicKey(event.target.value)}
              placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... you@your-machine"
              className="resize-y"
            />
          )}
        </Field>

        <button type="submit" className="hidden" aria-hidden />
      </form>
    </Modal>
  )
}
