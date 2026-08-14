import { useEffect, useState } from 'react'
import { Camera } from 'lucide-react'
import { apiErrorMessage, apiErrorStatus } from '../api/client'
import { useCreateSnapshot, useVolumes } from '../hooks/queries'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert } from '../ui/Feedback'

interface CreateSnapshotModalProps {
  instanceId: string
  instanceName: string
  open: boolean
  onClose: () => void
}

export function CreateSnapshotModal({
  instanceId,
  instanceName,
  open,
  onClose,
}: CreateSnapshotModalProps) {
  const createMut = useCreateSnapshot(instanceId)
  const { data: attached } = useVolumes(instanceId)
  const attachedVolumes = attached ?? []

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setDescription('')
    setFormError(null)
  }, [open])

  // Mirrors the server's rule: the name becomes a qcow2 tag passed to qemu-img
  // as a positional argument, so a leading dash would read as a flag.
  const nameInvalid = name.startsWith('-') || /[\r\n\t]/.test(name)
  const canSubmit = name.trim().length > 0 && !nameInvalid && !createMut.isPending

  const submit = async () => {
    setFormError(null)
    if (!canSubmit) return
    try {
      await createMut.mutateAsync({
        name: name.trim(),
        description: description.trim() || null,
      })
      onClose()
    } catch (err) {
      setFormError(
        apiErrorStatus(err) === 409
          ? `${instanceName} already has a snapshot named "${name.trim()}".`
          : apiErrorMessage(err),
      )
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Snapshot ${instanceName}`}
      icon={Camera}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            intent="primary"
            disabled={!canSubmit}
            loading={createMut.isPending}
            onClick={submit}
          >
            Create snapshot
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
        {/* The footgun. A qcow2 internal snapshot lives inside this instance's
            own overlay, so an attached volume — a separate file — is not in it.
            Someone snapshotting "the database VM" would reasonably assume the
            database was captured; it is not, and a restore rolls the OS back
            while the data disk moves on. */}
        {attachedVolumes.length > 0 && (
          <Alert tone="transitional">
            This captures the instance&apos;s own disk only.{' '}
            <span className="font-semibold">
              {attachedVolumes.length === 1
                ? `The attached volume "${attachedVolumes[0].name}" is not included`
                : `The ${attachedVolumes.length} attached volumes are not included`}
            </span>
            , and restoring will not roll them back — the OS returns to this point while
            the data on those disks moves on.
          </Alert>
        )}

        {formError && <Alert tone="danger">{formError}</Alert>}

        <Field
          label="Name"
          error={nameInvalid ? 'Cannot start with "-" or contain line breaks.' : undefined}
        >
          {({ id, describedBy }) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="before-upgrade"
            />
          )}
        </Field>

        <Field label={<>Description <span className="text-text-subtle">(optional)</span></>}>
          {({ id }) => (
            <Input
              id={id}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Clean install, before installing the app"
            />
          )}
        </Field>

        <Alert>
          Captures the disk as it stands now. Memory is not included — this hypervisor
          cannot save a running guest&apos;s state, which is why the instance has to be
          stopped. Restoring returns the disk to exactly this point.
        </Alert>

        <button type="submit" className="hidden" aria-hidden />
      </form>
    </Modal>
  )
}
