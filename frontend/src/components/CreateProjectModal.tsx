import { useEffect, useState } from 'react'
import { FolderOpen } from 'lucide-react'
import { apiErrorMessage } from '../api/client'
import { useCreateProject } from '../hooks/queries'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Field, Input } from '../ui/Field'
import { Alert } from '../ui/Feedback'

/** Create a project. Modal, matching every other creation in the product. */
export function CreateProjectModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const createMut = useCreateProject()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName('')
    setDescription('')
    setError(null)
  }, [open])

  const submit = async () => {
    if (!name.trim()) return
    setError(null)
    try {
      await createMut.mutateAsync({
        name: name.trim(),
        description: description.trim() || null,
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
      title="Create project"
      icon={FolderOpen}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            intent="primary"
            disabled={!name.trim()}
            loading={createMut.isPending}
            onClick={submit}
          >
            Create project
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
              placeholder="client-a"
              autoFocus
            />
          )}
        </Field>

        <Field label="Description" help="What this groups. Optional.">
          {({ id, describedBy }) => (
            <Input
              id={id}
              aria-describedby={describedBy}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Billable work"
            />
          )}
        </Field>

        <button type="submit" className="hidden" aria-hidden />
      </form>
    </Modal>
  )
}
