import { useState } from 'react'
import { KeyRound, Plus, Sparkles, Trash2 } from 'lucide-react'
import type { KeyPair } from '../api/client'
import { apiErrorMessage } from '../api/client'
import { useDeleteKeyPair, useKeyPairs } from '../hooks/queries'
import { ConfirmDialog } from './ConfirmDialog'
import { ImportKeyModal } from './ImportKeyModal'
import { GenerateKeyModal } from './GenerateKeyModal'
import { Button, IconButton } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { Alert, CopyButton, EmptyState, PathValue, TimeAgo, Truncated } from '../ui/Feedback'
import { Table, TActions, TBody, TD, TH, THead, TR, TableSkeleton } from '../ui/Table'
import { ViewHeader } from '../ui/ViewHeader'

export function KeyPairsTable() {
  const { data: keypairs, isLoading } = useKeyPairs()
  const deleteMut = useDeleteKeyPair()

  const [importOpen, setImportOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [confirmTarget, setConfirmTarget] = useState<KeyPair | null>(null)
  const [error, setError] = useState<string | null>(null)

  const confirmDelete = async () => {
    if (!confirmTarget) return
    setError(null)
    try {
      await deleteMut.mutateAsync(confirmTarget.id)
    } catch (err) {
      setError(apiErrorMessage(err))
    }
    setConfirmTarget(null)
  }

  return (
    <>
      <ViewHeader
        description="Public keys that can be installed on new instances. Choose them in the launch dialog — whoever holds the matching private key can SSH in as the default user."
        action={
          <div className="flex shrink-0 items-center gap-2">
            <Button icon={Sparkles} onClick={() => setGenerateOpen(true)}>
              Generate
            </Button>
            <Button intent="primary" icon={Plus} onClick={() => setImportOpen(true)}>
              Import key
            </Button>
          </div>
        }
      />

      {error && (
        <Alert tone="danger" className="mb-3">
          {error}
        </Alert>
      )}

      <Table>
        <THead>
          <TH>Name</TH>
          <TH>Type</TH>
          <TH>Fingerprint</TH>
          <TH>Source</TH>
          <TH>Created</TH>
          <TH align="right">Actions</TH>
        </THead>

        {isLoading ? (
          <TableSkeleton columns={6} rows={3} />
        ) : !keypairs?.length ? (
          <TBody>
            <tr>
              <TD colSpan={6}>
                <EmptyState
                  icon={KeyRound}
                  title="No key pairs yet"
                  action={
                    <Button intent="primary" icon={Plus} onClick={() => setImportOpen(true)}>
                      Import a key
                    </Button>
                  }
                >
                  Import the public key you already use, or generate a new one here.
                </EmptyState>
              </TD>
            </tr>
          </TBody>
        ) : (
          <TBody>
            {keypairs.map((keypair) => (
              <TR key={keypair.id}>
                <TD>
                  <div className="flex items-center gap-2">
                    <KeyRound className="h-3.5 w-3.5 shrink-0 text-text-subtle" />
                    <span className="font-medium text-text">{keypair.name}</span>
                  </div>
                  {keypair.private_key_path && (
                    <PathValue
                      dense
                      className="mt-0.5 max-w-[34ch]"
                      value={keypair.private_key_path}
                      title={`Private key on the backend: ${keypair.private_key_path}`}
                    />
                  )}
                </TD>
                <TD data>{keypair.key_type || '—'}</TD>
                <TD>
                  <div className="flex items-center gap-1">
                    <span className="data max-w-[22ch] text-xs text-text-muted">
                      <Truncated value={keypair.fingerprint} />
                    </span>
                    <CopyButton value={keypair.fingerprint} title="Copy fingerprint" />
                  </div>
                </TD>
                <TD>
                  {/* Provenance is a fact, not a state — monochrome. The
                      orchestrator key used to be green, which is also what a
                      healthy instance is. */}
                  <Badge tone={keypair.source === 'orchestrator' ? 'default' : 'quiet'}>
                    {keypair.source}
                  </Badge>
                </TD>
                <TD className="text-text-muted">
                  <TimeAgo value={keypair.created_at} />
                </TD>
                <TActions>
                  <IconButton
                    icon={Trash2}
                    intent="danger"
                    disabled={keypair.source === 'orchestrator'}
                    title={
                      keypair.source === 'orchestrator'
                        ? 'The orchestrator key cannot be deleted — every instance trusts it'
                        : 'Delete'
                    }
                    onClick={() => setConfirmTarget(keypair)}
                  />
                </TActions>
              </TR>
            ))}
          </TBody>
        )}
      </Table>

      <ImportKeyModal open={importOpen} onClose={() => setImportOpen(false)} />
      <GenerateKeyModal open={generateOpen} onClose={() => setGenerateOpen(false)} />

      <ConfirmDialog
        open={confirmTarget !== null}
        title="Delete key pair"
        message={
          <>
            This removes{' '}
            <span className="font-semibold text-text">{confirmTarget?.name}</span> from the
            catalog.
            <span className="mt-2 block text-transitional">
              Instances already launched with this key keep it — the key is written into
              their authorized_keys and deleting this record does not reach into a running
              VM. Their detail pages will still list it, marked as deleted.
            </span>
            {confirmTarget?.has_private_key && (
              <span className="mt-2 block text-text-muted">
                The private key file is left on disk; it may still be the only way into
                those instances.
              </span>
            )}
          </>
        }
        confirmLabel="Delete"
        busy={deleteMut.isPending}
        onConfirm={confirmDelete}
        onCancel={() => setConfirmTarget(null)}
      />
    </>
  )
}
