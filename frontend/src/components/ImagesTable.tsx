import { useState } from 'react'
import { HardDrive, Trash2, Upload } from 'lucide-react'
import type { Image } from '../api/client'
import { apiErrorMessage } from '../api/client'
import { useDeleteImage, useImages } from '../hooks/queries'
import { ConfirmDialog } from './ConfirmDialog'
import { ImportImageModal } from './ImportImageModal'
import { formatBytes } from '../lib/format'
import { Button, IconButton } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { StatusBadge } from '../ui/Status'
import { Alert, EmptyState } from '../ui/Feedback'
import { Table, TActions, TBody, TD, TH, THead, TR, TableSkeleton } from '../ui/Table'
import { ViewHeader } from '../ui/ViewHeader'

/** Catalog of disk images instances can be backed by. */
export function ImagesTable() {
  const { data: images, isLoading } = useImages()
  const deleteMut = useDeleteImage()

  const [confirmTarget, setConfirmTarget] = useState<Image | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const confirmDelete = async () => {
    if (!confirmTarget) return
    setActionError(null)
    try {
      await deleteMut.mutateAsync(confirmTarget.id)
    } catch (err) {
      // The in-use guard (409) lands here, and its message names the blocking
      // instances — worth showing verbatim.
      setActionError(apiErrorMessage(err))
    }
    setConfirmTarget(null)
  }

  return (
    <>
      <ViewHeader
        description="Disk images available as a backing store for new instances."
        action={
          /* "Add image", matching the dialog it opens. It said "Import image"
             while the dialog said "Add image" and offered two things, only one
             of which is an import — the other downloads from a URL. A control
             should name its destination, and the umbrella word is the one that
             covers both. */
          <Button intent="primary" icon={Upload} onClick={() => setImportOpen(true)}>
            Add image
          </Button>
        }
      />

      {actionError && (
        <Alert tone="danger" className="mb-3">
          {actionError}
        </Alert>
      )}

      <Table>
        <THead>
          <TH>Name</TH>
          <TH>Format</TH>
          <TH align="right">Virtual size</TH>
          <TH>Source</TH>
          <TH>Status</TH>
          <TH align="right">Actions</TH>
        </THead>

        {isLoading ? (
          <TableSkeleton columns={6} />
        ) : !images || images.length === 0 ? (
          <TBody>
            <tr>
              <TD colSpan={6}>
                <EmptyState
                  icon={HardDrive}
                  title="No images yet"
                  action={
                    <Button intent="primary" icon={Upload} onClick={() => setImportOpen(true)}>
                      Add an image
                    </Button>
                  }
                >
                  Download a cloud image from a URL, or register a qcow2, raw, vmdk or vdi
                  file already on this machine, and launch instances from it.
                </EmptyState>
              </TD>
            </tr>
          </TBody>
        ) : (
          <TBody>
            {images.map((image) => (
              <TR key={image.id}>
                <TD>
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-text">{image.name}</span>
                    {image.has_cloud_init && (
                      <Badge title="cloud-init present: the orchestrator's SSH key is injected on launch">
                        cloud-init
                      </Badge>
                    )}
                  </div>
                  {image.status === 'Error' && image.error_message && (
                    <p className="mt-1 data text-xs text-danger">{image.error_message}</p>
                  )}
                </TD>
                <TD data>{image.format}</TD>
                <TD align="right" data>
                  {image.virtual_size_bytes ? formatBytes(image.virtual_size_bytes) : '—'}
                  {image.actual_size_bytes ? (
                    <span className="ml-1.5 text-text-subtle">
                      ({formatBytes(image.actual_size_bytes)} on disk)
                    </span>
                  ) : null}
                </TD>
                <TD>
                  <Badge tone={image.source === 'builtin' ? 'quiet' : 'default'}>
                    {image.source}
                  </Badge>
                </TD>
                <TD>
                  {/* One status vocabulary across every resource: Importing is
                      transitional, Available is healthy — the same tones an
                      instance uses, so both tables read the same way. */}
                  <StatusBadge status={image.status} />
                </TD>
                <TActions>
                  <IconButton
                    icon={Trash2}
                    intent="danger"
                    title={
                      image.source === 'builtin'
                        ? 'The built-in image cannot be deleted'
                        : 'Delete this image'
                    }
                    disabled={image.source === 'builtin' || deleteMut.isPending}
                    onClick={() => setConfirmTarget(image)}
                  />
                </TActions>
              </TR>
            ))}
          </TBody>
        )}
      </Table>

      <ImportImageModal open={importOpen} onClose={() => setImportOpen(false)} />

      <ConfirmDialog
        open={confirmTarget !== null}
        title="Delete image"
        message={
          <>
            This will permanently delete{' '}
            <span className="font-semibold text-text">{confirmTarget?.name}</span> and its
            file. Instances already running from it keep working, but no new instance can be
            created from it.
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
