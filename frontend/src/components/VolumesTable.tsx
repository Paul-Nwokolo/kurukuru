import { useState } from 'react'
import { Database, Link2, Link2Off, Plus, Trash2 } from 'lucide-react'
import type { Volume } from '../api/client'
import { apiErrorMessage } from '../api/client'
import {
  useAttachVolume,
  useDeleteVolume,
  useDetachVolume,
  useInstances,
  useVolumes,
} from '../hooks/queries'
import { deviceHint } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'
import { CreateVolumeModal } from './CreateVolumeModal'
import { Button, IconButton } from '../ui/Button'
import { StatusBadge } from '../ui/Status'
import { Alert, EmptyState, TimeAgo } from '../ui/Feedback'
import { Table, TActions, TBody, TD, TH, THead, TR, TableSkeleton } from '../ui/Table'
import { ViewHeader } from '../ui/ViewHeader'
import { Modal } from '../ui/Modal'
import { Field, Select } from '../ui/Field'

export function VolumesTable() {
  const { data: volumes, isLoading } = useVolumes()
  const { data: instances } = useInstances(false)
  const attachMut = useAttachVolume()
  const detachMut = useDetachVolume()
  const deleteMut = useDeleteVolume()

  const [createOpen, setCreateOpen] = useState(false)
  const [attachTarget, setAttachTarget] = useState<Volume | null>(null)
  const [attachTo, setAttachTo] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Volume | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Only stopped instances can take a volume, so offering a running one would
  // be offering a 409. The empty case gets its own sentence rather than an
  // empty dropdown.
  const attachable = (instances ?? []).filter((i) => i.status === 'Stopped')

  const run = async (fn: () => Promise<unknown>) => {
    setError(null)
    try {
      await fn()
      return true
    } catch (err) {
      setError(apiErrorMessage(err))
      return false
    }
  }

  return (
    <>
      <ViewHeader
        description={
          <>
            Volumes are extra disks that{' '}
            <span className="text-text">outlive the instances they attach to</span> —
            terminating an instance detaches its volumes and leaves them here, with their
            data. A new volume arrives{' '}
            <span className="text-text">raw and uninitialised</span>: the guest sees an
            empty disk and must partition, format and mount it once, whatever OS it runs.
            On Linux that is <code className="data">lsblk</code>,{' '}
            <code className="data">mkfs.ext4 /dev/vdb</code> and{' '}
            <code className="data">mount</code>; on Windows it is Disk Management (or{' '}
            <code className="data">Get-Disk</code> / <code className="data">diskpart</code>)
            to bring the disk online, initialise and format it. Mount by a stable
            identifier rather than by position — UUID in{' '}
            <code className="data">/etc/fstab</code> on Linux, a drive letter or volume
            GUID on Windows — because the path is a position and the identifier is the
            disk. Attaching and detaching require a stopped instance.
          </>
        }
        action={
          <Button intent="primary" icon={Plus} onClick={() => setCreateOpen(true)}>
            Create volume
          </Button>
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
          <TH align="right">Size</TH>
          <TH>Status</TH>
          <TH>Attached to</TH>
          <TH>Device</TH>
          <TH>Created</TH>
          <TH align="right">Actions</TH>
        </THead>

        {isLoading ? (
          <TableSkeleton columns={7} />
        ) : (volumes ?? []).length === 0 ? (
          <TBody>
            <tr>
              <TD colSpan={7}>
                <EmptyState
                  icon={Database}
                  title="No volumes yet"
                  action={
                    <Button intent="primary" icon={Plus} onClick={() => setCreateOpen(true)}>
                      Create a volume
                    </Button>
                  }
                >
                  Create one, then attach it to a stopped instance. It appears in the guest
                  on the next start.
                </EmptyState>
              </TD>
            </tr>
          </TBody>
        ) : (
          <TBody>
            {(volumes ?? []).map((volume) => (
              <TR key={volume.id}>
                <TD>
                  <div className="flex items-center gap-2">
                    <Database className="h-3.5 w-3.5 shrink-0 text-text-subtle" />
                    <span className="font-medium text-text">{volume.name}</span>
                  </div>
                  {volume.error_message && (
                    <span className="mt-0.5 block text-xs text-danger">
                      {volume.error_message}
                    </span>
                  )}
                </TD>
                <TD align="right" data>
                  {volume.size_gb} GB
                </TD>
                <TD>
                  <StatusBadge status={volume.status} />
                </TD>
                <TD className="text-text-muted">
                  {volume.attached_instance_name ?? (
                    <span className="text-text-subtle">—</span>
                  )}
                </TD>
                {/* Named for the guest that will see it — see deviceHint. */}
                <TD data>{deviceHint(volume)}</TD>
                <TD className="text-text-muted">
                  <TimeAgo value={volume.created_at} />
                </TD>
                <TActions>
                  {volume.status === 'Available' && (
                    <IconButton
                      icon={Link2}
                      title={
                        attachable.length
                          ? 'Attach to a stopped instance'
                          : 'No stopped instance to attach to'
                      }
                      disabled={!attachable.length}
                      onClick={() => {
                        setAttachTarget(volume)
                        setAttachTo(attachable[0]?.id ?? '')
                      }}
                    />
                  )}
                  {volume.status === 'Attached' && (
                    <IconButton
                      icon={Link2Off}
                      title="Detach (the instance must be stopped)"
                      onClick={() => run(() => detachMut.mutateAsync(volume.id))}
                    />
                  )}
                  <IconButton
                    icon={Trash2}
                    intent="danger"
                    title={
                      volume.status === 'Attached'
                        ? 'Detach it before deleting'
                        : 'Delete this volume and its data'
                    }
                    disabled={volume.status === 'Attached'}
                    onClick={() => setDeleteTarget(volume)}
                  />
                </TActions>
              </TR>
            ))}
          </TBody>
        )}
      </Table>

      <CreateVolumeModal open={createOpen} onClose={() => setCreateOpen(false)} />

      <Modal
        open={attachTarget !== null}
        onClose={() => setAttachTarget(null)}
        title={`Attach ${attachTarget?.name ?? ''}`}
        icon={Link2}
        size="sm"
        footer={
          <>
            <Button onClick={() => setAttachTarget(null)}>Cancel</Button>
            <Button
              intent="primary"
              loading={attachMut.isPending}
              onClick={async () => {
                if (attachTarget && attachTo) {
                  await run(() =>
                    attachMut.mutateAsync({ id: attachTarget.id, instanceId: attachTo }),
                  )
                }
                setAttachTarget(null)
              }}
            >
              Attach
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <p className="text-sm leading-relaxed text-text-muted">
            Attach to a stopped instance. It appears in the guest on the{' '}
            <span className="text-text">next start</span>, unformatted.
          </p>
          <Field label="Instance">
            {({ id }) => (
              <Select id={id} value={attachTo} onChange={(e) => setAttachTo(e.target.value)}>
                {attachable.map((instance) => (
                  <option key={instance.id} value={instance.id}>
                    {instance.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
      </Modal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete volume"
        message={
          <>
            This permanently deletes{' '}
            <span className="font-semibold text-text">{deleteTarget?.name}</span> (
            {deleteTarget?.size_gb} GB){' '}
            <span className="text-danger">and everything on it</span>. Volumes cannot be
            snapshotted, so there is no undo.
          </>
        }
        confirmLabel="Delete"
        busy={deleteMut.isPending}
        onConfirm={async () => {
          if (deleteTarget) await run(() => deleteMut.mutateAsync(deleteTarget.id))
          setDeleteTarget(null)
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}
