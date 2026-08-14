import { Disc } from 'lucide-react'
import { useIsos, useSettings } from '../hooks/queries'
import { formatBytes } from '../lib/format'
import { EmptyState, PathValue, TimeAgo } from '../ui/Feedback'
import { Table, TBody, TD, TH, THead, TR, TableSkeleton } from '../ui/Table'

/**
 * Boot media the backend can see.
 *
 * Read-only by design — there is no upload endpoint, because installer images
 * run to gigabytes and the backend usually shares a machine with the user.
 * That makes the directory path the single most important thing on this
 * screen: it is the only way to add anything, so it is shown literally and is
 * copyable.
 */
export function IsosTable() {
  const { data: isos, isLoading } = useIsos()
  const { data: settings } = useSettings()

  const isoDir = settings
    ?.find((g) => g.name === 'Paths')
    ?.settings.find((s) => s.key === 'iso_dir')?.value

  return (
    <>
      <div className="mb-4 rounded-lg border border-border bg-surface-raised px-4 py-3">
        <p className="text-sm leading-relaxed text-text-muted">
          Installer images you can boot a new instance from. There is no upload — put{' '}
          <code className="data rounded-sm bg-surface px-1 py-0.5 text-xs text-text">
            .iso
          </code>{' '}
          files in this directory on the machine running the backend and they appear here.
        </p>
        {isoDir && <PathValue value={String(isoDir)} className="mt-2" />}
      </div>

      <Table>
        <THead>
          <TH>Name</TH>
          <TH align="right">Size</TH>
          <TH align="right">Modified</TH>
        </THead>

        {isLoading ? (
          <TableSkeleton columns={3} rows={3} />
        ) : !isos?.length ? (
          <TBody>
            <tr>
              <TD colSpan={3}>
                <EmptyState icon={Disc} title="No boot media">
                  Copy an .iso into the directory above and it will show up here — no restart
                  needed.
                </EmptyState>
              </TD>
            </tr>
          </TBody>
        ) : (
          <TBody>
            {isos.map((iso) => (
              <TR key={iso.name}>
                <TD>
                  <div className="flex items-center gap-2">
                    <Disc className="h-3.5 w-3.5 shrink-0 text-text-subtle" />
                    <span className="font-medium text-text">{iso.name}</span>
                  </div>
                </TD>
                <TD align="right" data>
                  {formatBytes(iso.size_bytes)}
                </TD>
                <TD align="right" className="text-text-muted">
                  <TimeAgo value={iso.modified_at} />
                </TD>
              </TR>
            ))}
          </TBody>
        )}
      </Table>
    </>
  )
}
