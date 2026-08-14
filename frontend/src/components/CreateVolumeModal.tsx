import { useEffect, useState } from 'react'
import { Database } from 'lucide-react'
import { apiErrorMessage } from '../api/client'
import { useCreateVolume } from '../hooks/queries'
import { useSelectedProject } from '../lib/project'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert } from '../ui/Feedback'

/**
 * Create a volume.
 *
 * Was an inline form pinned above the table — three controls of vertical space
 * held permanently for something done occasionally. It is a modal now, like
 * every other creation in the product.
 */
export function CreateVolumeModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const createMut = useCreateVolume()
  const [projectId] = useSelectedProject()
  const [name, setName] = useState('')
  const [sizeGb, setSizeGb] = useState('10')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setSizeGb('10')
    setError(null)
  }, [open])

  const size = Number(sizeGb)
  const valid = name.trim().length > 0 && Number.isFinite(size) && size > 0

  const submit = async () => {
    if (!valid) return
    setError(null)
    try {
      await createMut.mutateAsync({
        name: name.trim(),
        size_gb: size,
        project_id: projectId,
      })
      onClose()
    } catch (err) {
      setError(apiErrorMessage(err))
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Create volume"
      icon={Database}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button intent="primary" disabled={!valid} loading={createMut.isPending} onClick={submit}>
            Create volume
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
        {error && <Alert tone="danger">{error}</Alert>}

        <Field label="Name">
          {({ id }) => (
            <Input
              id={id}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="postgres-data"
              autoFocus
            />
          )}
        </Field>

        <Field
          label="Size (GB)"
          help="Allocated sparsely — a 100 GB volume does not consume 100 GB until the guest writes to it."
        >
          {({ id, describedBy }) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              type="number"
              min={1}
              data
              value={sizeGb}
              onChange={(event) => setSizeGb(event.target.value)}
            />
          )}
        </Field>

        <Alert>
          A new volume is <span className="text-text">unformatted</span>. Attach it to a
          stopped instance, then in the guest run <code className="data">lsblk</code> to find
          it, <code className="data">mkfs.ext4 /dev/vdb</code> and{' '}
          <code className="data">mount</code>. For <code className="data">/etc/fstab</code>,
          mount by UUID (<code className="data">blkid</code>) rather than device path — the
          path is a position, the UUID is the disk.
        </Alert>

        <button type="submit" className="hidden" aria-hidden />
      </form>
    </Modal>
  )
}
