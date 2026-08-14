import { useState } from 'react'
import { Check, FolderOpen, Pencil, Plus, Trash2, X } from 'lucide-react'
import type { Project } from '../api/client'
import { apiErrorMessage } from '../api/client'
import { useDeleteProject, useProjects, useRenameProject } from '../hooks/queries'
import { useSelectedProject } from '../lib/project'
import { ConfirmDialog } from './ConfirmDialog'
import { CreateProjectModal } from './CreateProjectModal'
import { Button, IconButton } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { Alert, TimeAgo } from '../ui/Feedback'
import { Input } from '../ui/Field'
import { Table, TActions, TBody, TD, TH, THead, TR, TableSkeleton } from '../ui/Table'
import { ViewHeader } from '../ui/ViewHeader'

export function ProjectsView() {
  const { data: projects, isLoading } = useProjects()
  const renameMut = useRenameProject()
  const deleteMut = useDeleteProject()
  const [selected, setSelected] = useSelectedProject()

  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [draftName, setDraftName] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const [error, setError] = useState<string | null>(null)

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
            Projects group instances, images and key pairs so the dashboard is not one flat
            list. They are <span className="text-text">organisational only</span> — this
            system has no authentication, so a project boundary grants and withholds nothing.
            Anything that can reach the API can see and act on every project.
          </>
        }
        action={
          <Button intent="primary" icon={Plus} onClick={() => setCreateOpen(true)}>
            Create project
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
          <TH align="right">Instances</TH>
          <TH align="right">Images</TH>
          <TH align="right">Keys</TH>
          <TH>Created</TH>
          <TH align="right">Actions</TH>
        </THead>

        {isLoading ? (
          <TableSkeleton columns={6} rows={2} />
        ) : (
          <TBody>
            {(projects ?? []).map((project) => (
              <TR key={project.id}>
                <TD>
                  {editing === project.id ? (
                    <span className="flex items-center gap-2">
                      <Input
                        autoFocus
                        value={draftName}
                        onChange={(event) => setDraftName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Escape') setEditing(null)
                        }}
                        className="w-48"
                      />
                      <IconButton
                        icon={Check}
                        title="Save"
                        onClick={async () => {
                          const ok = await run(() =>
                            renameMut.mutateAsync({
                              id: project.id,
                              name: draftName.trim(),
                            }),
                          )
                          if (ok) setEditing(null)
                        }}
                      />
                      <IconButton icon={X} title="Cancel" onClick={() => setEditing(null)} />
                    </span>
                  ) : (
                    <span className="flex flex-wrap items-center gap-2">
                      <FolderOpen className="h-3.5 w-3.5 shrink-0 text-text-subtle" />
                      <span className="font-medium text-text">{project.name}</span>
                      {project.is_default && <Badge tone="quiet">default</Badge>}
                      {selected === project.id && <Badge tone="accent">viewing</Badge>}
                      {project.description && (
                        <span className="text-xs text-text-subtle">{project.description}</span>
                      )}
                    </span>
                  )}
                </TD>
                <TD align="right" data>
                  {project.instance_count}
                </TD>
                <TD align="right" data>
                  {project.image_count}
                </TD>
                <TD align="right" data>
                  {project.keypair_count}
                </TD>
                <TD className="text-text-muted">
                  <TimeAgo value={project.created_at} />
                </TD>
                <TActions>
                  <IconButton
                    icon={FolderOpen}
                    title={
                      selected === project.id
                        ? 'Stop scoping to this project'
                        : 'View only this project'
                    }
                    onClick={() => setSelected(selected === project.id ? null : project.id)}
                  />
                  <IconButton
                    icon={Pencil}
                    title="Rename"
                    onClick={() => {
                      setEditing(project.id)
                      setDraftName(project.name)
                    }}
                  />
                  <IconButton
                    icon={Trash2}
                    intent="danger"
                    title={
                      project.is_default
                        ? 'The default project cannot be deleted'
                        : 'Delete this project'
                    }
                    disabled={project.is_default}
                    onClick={() => setDeleteTarget(project)}
                  />
                </TActions>
              </TR>
            ))}
          </TBody>
        )}
      </Table>

      <CreateProjectModal open={createOpen} onClose={() => setCreateOpen(false)} />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete project"
        message={
          <>
            This deletes the project{' '}
            <span className="font-semibold text-text">{deleteTarget?.name}</span>.{' '}
            {(deleteTarget?.image_count ?? 0) + (deleteTarget?.keypair_count ?? 0) > 0 ? (
              <>
                Its {deleteTarget?.image_count} image(s) and {deleteTarget?.keypair_count} key
                pair(s) will be{' '}
                <span className="text-text">moved to the default project, not deleted</span>.
              </>
            ) : (
              <>Nothing is filed under it.</>
            )}
          </>
        }
        confirmLabel="Delete"
        busy={deleteMut.isPending}
        onConfirm={async () => {
          if (!deleteTarget) return
          const ok = await run(() => deleteMut.mutateAsync(deleteTarget.id))
          // Scoping to a project that no longer exists would show an empty
          // dashboard with no explanation.
          if (ok && selected === deleteTarget.id) setSelected(null)
          setDeleteTarget(null)
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}
