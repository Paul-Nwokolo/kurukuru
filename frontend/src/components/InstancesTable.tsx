import { Fragment, useState } from 'react'
import {
  AlertCircle,
  ChevronDown,
  Copy,
  Monitor,
  Play,
  Rocket,
  RotateCw,
  Server,
  Square,
  Trash2,
} from 'lucide-react'
import type { Instance } from '../api/client'
import { apiErrorMessage } from '../api/client'
import {
  useCloneInstance,
  useRestartInstance,
  useSshKey,
  useStartInstance,
  useStopInstance,
  useTerminateInstance,
} from '../hooks/queries'
import { EngineBadge } from './EngineBadge'
import { ConfirmDialog } from './ConfirmDialog'
import { ConsoleModal } from './ConsoleModal'
import { formatMemory } from '../lib/format'
import { displayAddress, sshCommand } from '../lib/ssh'
import { Link } from './Link'
import { Button, IconButton } from '../ui/Button'
import { StatusBadge } from '../ui/Status'
import { Badge } from '../ui/Badge'
import { Alert, CopyButton, EmptyState, TimeAgo } from '../ui/Feedback'
import { Table, TActions, TBody, TD, TH, THead, TR, TableSkeleton } from '../ui/Table'
import { Modal } from '../ui/Modal'
import { Field, Input } from '../ui/Field'

interface InstancesTableProps {
  instances: Instance[]
  isLoading: boolean
  onLaunch: () => void
  /** Lifted so the launch flow can open a console for a just-created instance. */
  consoleTarget: Instance | null
  onConsoleTargetChange: (instance: Instance | null) => void
}

export function InstancesTable({
  instances,
  isLoading,
  onLaunch,
  consoleTarget,
  onConsoleTargetChange,
}: InstancesTableProps) {
  const startMut = useStartInstance()
  const stopMut = useStopInstance()
  const restartMut = useRestartInstance()
  const cloneMut = useCloneInstance()
  const terminateMut = useTerminateInstance()
  // Copy SSH is inert until this resolves — a command without -i would be
  // copied, pasted, and rejected by the guest.
  const { data: sshKey } = useSshKey()
  const keyPath = sshKey?.private_key_path ?? null

  const [confirmTarget, setConfirmTarget] = useState<Instance | null>(null)
  const [cloneTarget, setCloneTarget] = useState<Instance | null>(null)
  const [cloneName, setCloneName] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const isRowBusy = (id: string) =>
    (startMut.isPending && startMut.variables === id) ||
    (stopMut.isPending && stopMut.variables === id) ||
    (terminateMut.isPending && terminateMut.variables === id) ||
    (restartMut.isPending && restartMut.variables === id)

  /** Runs an action, surfacing any failure. Returns whether it succeeded, so a
   *  dialog can stay open on error instead of closing over the message. */
  const runAction = async (fn: () => Promise<unknown>): Promise<boolean> => {
    setActionError(null)
    try {
      await fn()
      return true
    } catch (err) {
      setActionError(apiErrorMessage(err))
      return false
    }
  }

  return (
    <>
      {actionError && (
        <Alert tone="danger" className="mb-3">
          {actionError}
        </Alert>
      )}

      <Table>
        <THead>
          <TH>Name</TH>
          <TH>Status</TH>
          <TH>Address</TH>
          <TH>Size</TH>
          <TH>Created</TH>
          <TH align="right">Actions</TH>
        </THead>

        {isLoading ? (
          <TableSkeleton columns={6} />
        ) : instances.length === 0 ? (
          <TBody>
            <tr>
              <TD colSpan={6}>
                <EmptyState
                  icon={Server}
                  title="No instances yet"
                  action={
                    <Button intent="primary" size="lg" icon={Rocket} onClick={onLaunch}>
                      Launch your first instance
                    </Button>
                  }
                >
                  Spin up a virtual machine to get started. Instances provision in the
                  background and appear here automatically.
                </EmptyState>
              </TD>
            </tr>
          </TBody>
        ) : (
          <TBody>
            {instances.map((instance) => {
              const busy = isRowBusy(instance.id)
              const dimmed = instance.status === 'Terminated'
              const expanded = expandedId === instance.id
              const ssh = sshCommand(instance, keyPath)
              return (
                <Fragment key={instance.id}>
                  <TR className={dimmed ? 'opacity-50' : ''}>
                    {/* Name on its own line, badges on a second one beneath it.
                        Clustered horizontally they competed with the name for
                        the same scan — three of them on a long name pushed the
                        column wide and wrapped raggedly, and the one thing the
                        eye is actually running down the column for is the name.
                        Stacked, the names left-align into a single column and
                        the badges read as annotation on it. */}
                    <TD>
                      {/* A real anchor, so ctrl/middle-click opens a tab. */}
                      <Link
                        to={`/instances/${instance.id}`}
                        className="font-medium text-text hover:text-accent-text hover:underline"
                      >
                        {instance.name}
                      </Link>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <EngineBadge engine={instance.engine} />
                        {/* Guest OS on the row, because almost every piece of
                            advice elsewhere in the UI — how to reach it, how a
                            volume is initialised — differs by it, and a user
                            scanning the list should not have to open a detail
                            page to know which set applies. Linux is the
                            overwhelming default and stays unlabelled; a badge
                            on every row would be noise. */}
                        {instance.guest_os === 'windows' && (
                          <Badge
                            tone="quiet"
                            title="Windows guest — console access only until you enable Remote Desktop inside it. Volumes are initialised in Disk Management, not with mkfs."
                          >
                            Windows
                          </Badge>
                        )}
                        {instance.boot_source === 'iso' && (
                          <Badge
                            tone="quiet"
                            title={`Booted from ${instance.iso ?? 'an ISO'} — no SSH key; use the Console`}
                          >
                            ISO
                          </Badge>
                        )}
                      </div>
                    </TD>

                    <TD>
                      <div className="flex items-center gap-2">
                        <StatusBadge
                          status={instance.degraded ? 'Degraded' : instance.status}
                          label={instance.degraded ? 'Degraded' : undefined}
                        />
                        {instance.degraded && instance.degraded_reason && (
                          <span
                            title={instance.degraded_reason}
                            className="cursor-help text-transitional"
                            aria-label="Why this instance is degraded"
                          >
                            <AlertCircle className="h-3.5 w-3.5" />
                          </span>
                        )}
                        {instance.status === 'Error' && instance.error_message && (
                          <button
                            type="button"
                            onClick={() => setExpandedId(expanded ? null : instance.id)}
                            title={instance.error_message}
                            className="text-text-subtle hover:text-text"
                            // The chevron rotates to say which way this goes;
                            // `aria-expanded` is that same fact for anyone not
                            // reading the rotation, and the label follows it so
                            // the control is never named for the state it is
                            // already in.
                            aria-expanded={expanded}
                            aria-controls={expanded ? `${instance.id}-error` : undefined}
                            aria-label={expanded ? 'Hide error details' : 'Show error details'}
                          >
                            <ChevronDown
                              className={`h-4 w-4 transition-transform ${expanded ? 'rotate-180' : ''}`}
                            />
                          </button>
                        )}
                      </div>
                    </TD>

                    <TD data>
                      {instance.ip_address ? (
                        <div className="flex items-center gap-1">
                          {/* QEMU VMs are reached through a host port forward,
                              so the port is part of the address. */}
                          <span>{displayAddress(instance)}</span>
                          {instance.status === 'Running' && ssh && (
                            <CopyButton value={ssh} title={`Copy: ${ssh}`} />
                          )}
                        </div>
                      ) : (
                        <span className="text-text-subtle">—</span>
                      )}
                    </TD>

                    <TD data title={`Started from: ${instance.flavor}`}>
                      {instance.cpus && instance.memory_mb ? (
                        <span className="whitespace-nowrap">
                          {instance.cpus} vCPU · {formatMemory(instance.memory_mb)}
                          {instance.disk_gb ? ` · ${instance.disk_gb} GB` : ''}
                        </span>
                      ) : (
                        <span className="capitalize">{instance.flavor}</span>
                      )}
                    </TD>

                    <TD className="text-text-muted">
                      <TimeAgo value={instance.created_at} />
                    </TD>

                    <TActions>
                      {/* Shown disabled rather than hidden when unavailable, so
                          the reason is discoverable instead of the action
                          silently vanishing. */}
                      {instance.status === 'Running' && (
                        <IconButton
                          icon={Monitor}
                          title={
                            instance.console_supported
                              ? 'Open console'
                              : 'No console for this instance'
                          }
                          disabled={!instance.console_supported}
                          onClick={() => onConsoleTargetChange(instance)}
                        />
                      )}
                      {instance.status === 'Stopped' && (
                        <IconButton
                          icon={Play}
                          title="Start"
                          disabled={busy}
                          onClick={() => runAction(() => startMut.mutateAsync(instance.id))}
                        />
                      )}
                      {instance.status === 'Running' && (
                        <IconButton
                          icon={RotateCw}
                          title="Restart — graceful shutdown, then boot again"
                          disabled={busy}
                          onClick={() => runAction(() => restartMut.mutateAsync(instance.id))}
                        />
                      )}
                      {instance.status === 'Running' && (
                        <IconButton
                          icon={Square}
                          title="Stop"
                          disabled={busy}
                          onClick={() => runAction(() => stopMut.mutateAsync(instance.id))}
                        />
                      )}
                      {/* Clone needs a stopped source: copying a disk under a
                          running guest captures it mid-write. Shown disabled
                          rather than hidden so the reason is discoverable. */}
                      {instance.status !== 'Terminated' && (
                        <IconButton
                          icon={Copy}
                          title={
                            instance.status === 'Stopped'
                              ? 'Clone this instance'
                              : 'Stop the instance first — a clone copies its disk'
                          }
                          disabled={instance.status !== 'Stopped' || busy}
                          onClick={() => {
                            setCloneTarget(instance)
                            setCloneName(`${instance.name}-copy`.slice(0, 31))
                          }}
                        />
                      )}
                      {instance.status !== 'Terminated' && (
                        <IconButton
                          icon={Trash2}
                          title="Terminate"
                          intent="danger"
                          disabled={busy}
                          onClick={() => setConfirmTarget(instance)}
                        />
                      )}
                    </TActions>
                  </TR>

                  {expanded && instance.error_message && (
                    <tr>
                      <TD colSpan={6} className="bg-surface pt-0">
                        <Alert tone="danger">
                          <pre
                            id={`${instance.id}-error`}
                            className="whitespace-pre-wrap break-words data text-xs"
                          >
                            {instance.error_message}
                          </pre>
                        </Alert>
                      </TD>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </TBody>
        )}
      </Table>

      <ConsoleModal instance={consoleTarget} onClose={() => onConsoleTargetChange(null)} />

      <Modal
        open={cloneTarget !== null}
        onClose={() => setCloneTarget(null)}
        title={`Clone ${cloneTarget?.name ?? ''}`}
        icon={Copy}
        footer={
          <>
            <Button onClick={() => setCloneTarget(null)}>Cancel</Button>
            <Button
              intent="primary"
              disabled={!cloneName.trim()}
              loading={cloneMut.isPending}
              onClick={async () => {
                if (!cloneTarget) return
                const ok = await runAction(() =>
                  cloneMut.mutateAsync({ id: cloneTarget.id, name: cloneName.trim() }),
                )
                if (ok) setCloneTarget(null)
              }}
            >
              Clone
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Field label="Name for the clone">
            {({ id }) => (
              <Input
                id={id}
                autoFocus
                value={cloneName}
                onChange={(event) => setCloneName(event.target.value)}
              />
            )}
          </Field>

          <Alert>
            A <span className="text-text">full copy</span> of the disk, not a
            reference to it — so it costs the whole{' '}
            <span className="data">{cloneTarget?.disk_gb} GB</span> and takes as long as
            that takes. In exchange the clone owns its data: terminating{' '}
            <span className="text-text">{cloneTarget?.name}</span> later cannot damage it.
          </Alert>

          <Alert tone="transitional">
            The clone inherits the guest&apos;s{' '}
            <span className="font-semibold">SSH host keys</span>, so your SSH client will
            see two machines claiming the same identity and warn about it. Snapshots and
            attached volumes are <span className="font-semibold">not</span> copied — they
            stay with the original.
          </Alert>
        </div>
      </Modal>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="Terminate instance"
        message={
          <>
            This permanently deletes and purges{' '}
            <span className="font-semibold text-text">{confirmTarget?.name}</span>, including
            its disk and snapshots. Attached volumes are detached and kept. This cannot be
            undone.
          </>
        }
        confirmLabel="Terminate"
        busy={terminateMut.isPending}
        onConfirm={async () => {
          if (!confirmTarget) return
          await runAction(() => terminateMut.mutateAsync(confirmTarget.id))
          setConfirmTarget(null)
        }}
        onCancel={() => setConfirmTarget(null)}
      />
    </>
  )
}
