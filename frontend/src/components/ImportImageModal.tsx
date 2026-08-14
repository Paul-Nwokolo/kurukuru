import { useEffect, useState } from 'react'
import { Upload } from 'lucide-react'
import { apiErrorMessage, apiErrorStatus } from '../api/client'
import { useFetchImage, useImportImage } from '../hooks/queries'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Checkbox, Field, Input, SegmentedControl } from '../ui/Field'
import { Alert } from '../ui/Feedback'

/**
 * Add a disk image to the catalog.
 *
 * Two sources, and the ordering is the point.
 *
 * **From a URL** is the default because it is how cloud images are actually
 * distributed — every distribution publishes a qcow2 at a stable address with
 * a checksum beside it — and because it is the only route that works the same
 * whatever OS the *customer* is on. Nothing about it touches their machine.
 *
 * **From a file on the server** was the only option before, and it was
 * ambiguous in the worst way: it looks like a file picker but it is a path on
 * the machine running the backend. A customer on Windows pasting
 * `C:\\images\\disk.qcow2` gets "no file at ..." unless the backend happens to
 * be their own machine. It is kept, because a file already on the host has no
 * other route in, and it is now labelled for what it is.
 *
 * There is deliberately **no browser upload**: multi-gigabyte images through a
 * form would copy the data twice and time out, and the endpoint does not exist.
 * Better to say so than to offer a control that fails at 4 GB.
 */
type Source = 'url' | 'server'

export function ImportImageModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const fetchMut = useFetchImage()
  const importMut = useImportImage()

  const [source, setSource] = useState<Source>('url')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [sha256, setSha256] = useState('')
  const [path, setPath] = useState('')
  const [hasCloudInit, setHasCloudInit] = useState(true)
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setSource('url')
    setName('')
    setUrl('')
    setSha256('')
    setPath('')
    setHasCloudInit(true)
    setFormError(null)
  }, [open])

  const busy = fetchMut.isPending || importMut.isPending
  const checksumValid = sha256.trim() === '' || /^[0-9a-fA-F]{64}$/.test(sha256.trim())
  const canSubmit =
    name.trim().length > 0 &&
    !busy &&
    checksumValid &&
    (source === 'url' ? /^https?:\/\/\S+$/i.test(url.trim()) : path.trim().length > 0)

  const submit = async () => {
    setFormError(null)
    if (!canSubmit) return
    try {
      if (source === 'url') {
        await fetchMut.mutateAsync({
          name: name.trim(),
          url: url.trim(),
          sha256: sha256.trim() ? sha256.trim().toLowerCase() : null,
          has_cloud_init: hasCloudInit,
        })
      } else {
        await importMut.mutateAsync({
          name: name.trim(),
          path: path.trim(),
          has_cloud_init: hasCloudInit,
        })
      }
      onClose()
    } catch (err) {
      setFormError(
        apiErrorStatus(err) === 409
          ? `An image named "${name.trim()}" already exists.`
          : apiErrorMessage(err),
      )
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add image"
      icon={Upload}
      size="lg"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button intent="primary" disabled={!canSubmit} loading={busy} onClick={submit}>
            {source === 'url' ? 'Download' : 'Import'}
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

        <SegmentedControl
          label="Image source"
          value={source}
          onChange={setSource}
          options={[
            { value: 'url', label: 'From a URL', title: 'The backend downloads it' },
            {
              value: 'server',
              label: 'File on the server',
              title: 'A path on the machine running the backend',
            },
          ]}
        />

        <Field label="Name">
          {({ id }) => (
            <Input
              id={id}
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Debian 12"
            />
          )}
        </Field>

        {source === 'url' ? (
          <>
            <Field
              label="Image URL"
              help={
                <>
                  A direct link to a <code className="data">.qcow2</code>,{' '}
                  <code className="data">.img</code> or <code className="data">.raw</code>{' '}
                  file — the <span className="text-text">backend</span> downloads it, so
                  nothing is uploaded from this computer. Distributions publish these:
                  Ubuntu at <code className="data">cloud-images.ubuntu.com</code>, Debian at{' '}
                  <code className="data">cloud.debian.org/images/cloud</code>, Fedora at{' '}
                  <code className="data">download.fedoraproject.org</code>.
                </>
              }
            >
              {({ id, describedBy }) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  data
                  spellCheck={false}
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  placeholder="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
                />
              )}
            </Field>

            <Field
              label={
                <>
                  SHA256 <span className="text-text-subtle">(optional, recommended)</span>
                </>
              }
              error={checksumValid ? undefined : 'A SHA256 is 64 hexadecimal characters.'}
              help="Published alongside the image, usually in a SHA256SUMS file. Verified as the download streams, so a corrupted or substituted file is rejected rather than launched."
            >
              {({ id, describedBy }) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  data
                  spellCheck={false}
                  value={sha256}
                  onChange={(event) => setSha256(event.target.value)}
                  placeholder="e3b0c44298fc1c149afbf4c8996fb924…"
                />
              )}
            </Field>
          </>
        ) : (
          <Field
            label="Path on the backend host"
            help={
              <>
                This is a path on the{' '}
                <span className="text-text">machine running the backend</span>, readable by
                the backend process — <span className="text-text">not</span> a path on this
                computer, unless they are the same machine. The file is copied into the
                image store, so it can be moved or deleted afterwards.
              </>
            }
          >
            {({ id, describedBy }) => (
              <Input
                id={id}
                aria-describedby={describedBy}
                data
                spellCheck={false}
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder="/var/lib/images/disk.qcow2  ·  C:\images\disk.qcow2"
              />
            )}
          </Field>
        )}

        <div>
          <Checkbox
            checked={hasCloudInit}
            onChange={(event) => setHasCloudInit(event.target.checked)}
            label={<span className="text-text">This image has cloud-init</span>}
          />
          <p className="mt-1.5 pl-6 text-xs leading-relaxed text-text-muted">
            Every distribution&apos;s <em>cloud</em> image does — that is what makes it a
            cloud image. Without it no SSH key can be injected, and instances from this
            image are reachable only through the console.
          </p>
        </div>

        <button type="submit" className="hidden" aria-hidden />
      </form>
    </Modal>
  )
}
