import { useState } from 'react'
import { History, RotateCcw, Trash2 } from 'lucide-react'
import type { Volume } from '../api/client'
import { apiErrorMessage } from '../api/client'
import {
  useCreateVolumeSnapshot,
  useDeleteVolumeSnapshot,
  useRestoreVolumeSnapshot,
  useVolumeSnapshots,
} from '../hooks/queries'
import { formatBytes } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'
import { Button, IconButton } from '../ui/Button'
import { StatusBadge } from '../ui/Status'
import { Alert, EmptyState, TimeAgo } from '../ui/Feedback'
import { Field, Input } from '../ui/Field'
import { Modal } from '../ui/Modal'
import { Table, TActions, TBody, TD, TH, THead, TR } from '../ui/Table'

/** Rejected by the API as a qcow2 tag; refused here so the round trip is not
 *  needed to learn it. Kept in step with validate_snapshot_tag on the server. */
function nameIsInvalid(name: string): boolean {
  return name.startsWith('-') || /[\r\n\t]/.test(name)
}

export function VolumeSnapshotsModal({
  volume,
  onClose,
}: {
  volume: Volume
  onClose: () => void
}) {
  const { data: snapshots, isLoading } = useVolumeSnapshots(volume.id)
  const createMut = useCreateVolumeSnapshot(volume.id)
  const restoreMut = useRestoreVolumeSnapshot(volume.id)
  const deleteMut = useDeleteVolumeSnapshot(volume.id)

  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [restoreTarget, setRestoreTarget] = useState<{ id: string; name: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null)

  const invalid = name.trim().length > 0 && nameIsInvalid(name.trim())
  const busy = createMut.isPending || restoreMut.isPending || deleteMut.isPending

  async function run(action: () => Promise<unknown>) {
    setError(null)
    try {
      await action()
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }

  return (
    <>
      <Modal
        open
        onClose={onClose}
        title={`Snapshots of ${volume.name}`}
        icon={History}
        footer={<Button onClick={onClose}>Close</Button>}
      >
        <div className="space-y-4">
          {/* The counterpart of the warning on the instance snapshot dialog,
              and it has to be said here too. Someone snapshotting the data
              disk of a database VM would reasonably assume the VM came with
              it. It does not: these are two files and two operations, and
              restoring this one leaves the instance exactly where it was. */}
          <Alert tone="transitional">
            This captures the volume&apos;s disk only.{' '}
            <span className="font-semibold">
              {volume.attached_instance_name
                ? `The instance "${volume.attached_instance_name}" is not included`
                : 'No instance is included'}
            </span>
            , and restoring will not roll one back — the data on this disk returns to the
            snapshot while everything else moves on.
          </Alert>

          {error && <Alert tone="danger">{error}</Alert>}

          <form
            className="flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              const trimmed = name.trim()
              if (!trimmed || invalid) return
              void run(async () => {
                await createMut.mutateAsync({ name: trimmed })
                setName('')
              })
            }}
          >
            <div className="flex-1">
              <Field
                label="New snapshot"
                error={invalid ? 'Cannot start with "-" or contain line breaks.' : undefined}
              >
                {({ id, describedBy }) => (
                  <Input
                    id={id}
                    aria-describedby={describedBy}
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="before-upgrade"
                    disabled={busy}
                  />
                )}
              </Field>
            </div>
            <Button
              type="submit"
              intent="primary"
              disabled={!name.trim() || invalid || busy}
              loading={createMut.isPending}
            >
              Create
            </Button>
          </form>

          {isLoading ? (
            <p className="text-sm text-text-muted">Loading…</p>
          ) : !snapshots?.length ? (
            <EmptyState icon={History} title="No snapshots">
              A snapshot records this volume&apos;s contents so you can return to them
              later.
            </EmptyState>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>Name</TH>
                  <TH>Status</TH>
                  <TH>Size</TH>
                  <TH>Created</TH>
                  <TH />
                </TR>
              </THead>
              <TBody>
                {snapshots.map((snapshot) => (
                  <TR key={snapshot.id}>
                    <TD className="font-medium">{snapshot.name}</TD>
                    <TD>
                      <StatusBadge status={snapshot.status} />
                      {snapshot.error_message && (
                        <span className="ml-2 text-xs text-text-muted">
                          {snapshot.error_message}
                        </span>
                      )}
                    </TD>
                    <TD>{formatBytes(snapshot.size_bytes ?? 0)}</TD>
                    <TD>
                      <TimeAgo value={snapshot.created_at} />
                    </TD>
                    <TActions>
                      <IconButton
                        icon={RotateCcw}
                        title={
                          snapshot.status === 'Available'
                            ? 'Restore the volume to this snapshot'
                            : 'Only an Available snapshot can be restored'
                        }
                        disabled={snapshot.status !== 'Available' || busy}
                        onClick={() =>
                          setRestoreTarget({ id: snapshot.id, name: snapshot.name })
                        }
                      />
                      <IconButton
                        icon={Trash2}
                        intent="danger"
                        title="Delete this snapshot"
                        disabled={busy}
                        onClick={() =>
                          setDeleteTarget({ id: snapshot.id, name: snapshot.name })
                        }
                      />
                    </TActions>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </div>
      </Modal>

      {restoreTarget && (
        <ConfirmDialog
          open
          title={`Restore ${volume.name}?`}
          confirmLabel="Restore"
          busy={restoreMut.isPending}
          onCancel={() => setRestoreTarget(null)}
          onConfirm={() => {
            const target = restoreTarget
            setRestoreTarget(null)
            void run(() => restoreMut.mutateAsync(target.id))
          }}
          message={
            <>
              This returns the volume to{' '}
              <span className="font-semibold text-text">{restoreTarget.name}</span> and
              discards everything written to it since.
              {volume.attached_instance_name && (
                <>
                  {' '}
                  <span className="font-semibold text-text">
                    {volume.attached_instance_name}
                  </span>{' '}
                  is not restored — if it holds state that expects the newer contents of
                  this disk, the two will disagree.
                </>
              )}
            </>
          }
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          open
          title="Delete snapshot?"
          confirmLabel="Delete"
          busy={deleteMut.isPending}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const target = deleteTarget
            setDeleteTarget(null)
            void run(() => deleteMut.mutateAsync(target.id))
          }}
          message={
            <>
              This permanently deletes{' '}
              <span className="font-semibold text-text">{deleteTarget.name}</span>. The
              volume&apos;s current contents are untouched.
            </>
          }
        />
      )}
    </>
  )
}
