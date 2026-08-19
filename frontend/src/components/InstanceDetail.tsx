import { useState } from 'react'
import {
  AlertCircle,
  ArrowLeft,
  Camera,
  Check,
  Database,
  Copy,
  KeyRound,
  Monitor,
  Network as NetworkIcon,
  Play,
  RotateCcw,
  Server,
  Square,
  Trash2,
} from 'lucide-react'
import type { GuestOS, Snapshot } from '../api/client'
import { apiErrorMessage } from '../api/client'
import {
  useAddForward,
  useDeleteSnapshot,
  useDetachVolume,
  useForwardPresets,
  useForwards,
  useRemoveForward,
  useInstance,
  useInstanceEvents,
  useInstanceKeyPairs,
  useKeyPairs,
  useRestoreSnapshot,
  useSnapshots,
  useStartInstance,
  useStopInstance,
  useTerminateInstance,
  useVolumes,
} from '../hooks/queries'
import { navigate } from '../lib/router'
import { deviceHint, formatMemory, relativeTime } from '../lib/format'
import { displayAddress, sshCommand } from '../lib/ssh'
import { eventStyle } from '../lib/events'
import { TimeAgo } from '../ui/Feedback'
import { StatusBadge } from '../ui/Status'
import { ConfirmDialog } from './ConfirmDialog'
import { ConsoleModal } from './ConsoleModal'
import { CreateSnapshotModal } from './CreateSnapshotModal'

interface InstanceDetailProps {
  instanceId: string
}

export function InstanceDetail({ instanceId }: InstanceDetailProps) {
  const { data: instance, isLoading, isError } = useInstance(instanceId)
  const { data: installedKeys } = useInstanceKeyPairs(instanceId)
  const { data: snapshots } = useSnapshots(instanceId)
  const { data: catalog } = useKeyPairs()

  const startMut = useStartInstance()
  const stopMut = useStopInstance()
  const terminateMut = useTerminateInstance()
  const restoreMut = useRestoreSnapshot(instanceId)
  const deleteSnapMut = useDeleteSnapshot(instanceId)

  const [actionError, setActionError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [consoleOpen, setConsoleOpen] = useState(false)
  const [snapshotOpen, setSnapshotOpen] = useState(false)
  const [confirmTerminate, setConfirmTerminate] = useState(false)
  const [restoreTarget, setRestoreTarget] = useState<Snapshot | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Snapshot | null>(null)

  if (isLoading) return <DetailSkeleton />
  if (isError || !instance) return <NotFound />

  const busy = startMut.isPending || stopMut.isPending || terminateMut.isPending

  // Which private key opens *this* instance.
  //
  // Until key pairs existed every guest trusted the orchestrator's key, so the
  // command could hardcode it. Now an instance may have been launched with
  // someone else's key, and offering `-i <orchestrator key>` for it produces a
  // command that is confidently wrong — it fails with "Permission denied
  // (publickey)", which reads like a broken instance rather than a wrong flag.
  //
  // So: prefer a key this instance actually has *and* whose private half we
  // hold. If none qualifies — every installed key was imported, and its
  // private half is on the user's machine — say so rather than guess.
  const installedWithPrivateKey = (installedKeys ?? [])
    .map((installed) => catalog?.find((k) => k.id === installed.keypair_id))
    .find((k) => k?.has_private_key && k.private_key_path)

  const keyPath = installedWithPrivateKey?.private_key_path ?? null
  const ssh = sshCommand(instance, keyPath ?? '<path to your private key>')
  const keyIsOurs = Boolean(keyPath)

  const run = async (fn: () => Promise<unknown>) => {
    setActionError(null)
    try {
      await fn()
    } catch (err) {
      setActionError(apiErrorMessage(err))
    }
  }

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="space-y-5">
      <button
        type="button"
        onClick={() => navigate('/')}
        className="inline-flex items-center gap-1.5 text-sm text-text-subtle hover:text-text-muted"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Instances
      </button>

      {/* Header: identity and the actions that change it. */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-text">{instance.name}</h1>
          <StatusBadge
            status={instance.degraded ? 'Degraded' : instance.status}
            label={instance.degraded ? 'Degraded' : undefined}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {instance.status === 'Stopped' && (
            <Action
              icon={Play}
              label="Start"
              busy={busy}
              onClick={() => run(() => startMut.mutateAsync(instance.id))}
              className="text-healthy hover:bg-healthy-quiet"
            />
          )}
          {instance.status === 'Running' && (
            <>
              <Action
                icon={Monitor}
                label="Console"
                busy={false}
                disabled={!instance.console_supported}
                onClick={() => setConsoleOpen(true)}
                className="text-accent-text hover:bg-accent-quiet"
              />
              <Action
                icon={Square}
                label="Stop"
                busy={busy}
                onClick={() => run(() => stopMut.mutateAsync(instance.id))}
                className="text-transitional hover:bg-transitional-quiet"
              />
            </>
          )}
          <Action
            icon={Camera}
            label="Snapshot"
            busy={false}
            disabled={instance.status !== 'Stopped'}
            title={
              instance.status === 'Stopped'
                ? 'Capture this disk'
                : 'Stop the instance first — snapshots capture the disk at rest'
            }
            onClick={() => setSnapshotOpen(true)}
            className="text-accent-text hover:bg-accent-quiet"
          />
          {instance.status !== 'Terminated' && (
            <Action
              icon={Trash2}
              label="Terminate"
              busy={busy}
              onClick={() => setConfirmTerminate(true)}
              className="text-danger hover:bg-danger-quiet"
            />
          )}
        </div>
      </div>

      {actionError && (
        <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger-quiet px-3 py-2 text-sm text-danger">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          {actionError}
        </div>
      )}
      {instance.error_message && (
        <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger-quiet px-3 py-2 text-sm text-danger">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <pre className="whitespace-pre-wrap break-words data text-xs">
            {instance.error_message}
          </pre>
        </div>
      )}
      {instance.degraded && instance.degraded_reason && (
        <div className="rounded-md border border-transitional/40 bg-transitional-quiet px-3 py-2 text-sm text-transitional">
          {instance.degraded_reason}
        </div>
      )}

      {/*
        Ordered by what people come to this page for, not by the order the
        features were built.

        Access first: the reason you open an instance is almost always to get
        into it. Then Network, because a forward is how you reach anything
        other than SSH. Then Storage — volumes and snapshots, which are both
        "what disks does this have". Configuration and the user-data it was
        built from are reference material, so they sit below the fold. Activity
        last, full width, because it is the longest thing here.

        Two columns at wide widths: the page was a single left-aligned column
        of six stacked cards, which on a 1440px screen meant a great deal of
        scrolling past empty space on the right.
      */}
        <Section title="Access">
          {ssh ? (
            <div className="px-4 py-3">
              <div className="mb-1.5 text-xs text-text-subtle">SSH</div>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded border border-border bg-surface px-2.5 py-1.5 data text-xs text-text-muted">
                  {ssh}
                </code>
                <button
                  type="button"
                  onClick={() => copy(ssh)}
                  title="Copy"
                  className="shrink-0 rounded p-1.5 text-text-subtle hover:bg-surface-overlay hover:text-text"
                >
                  {copied ? (
                    <Check className="h-3.5 w-3.5 text-healthy" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </button>
              </div>
              {!keyIsOurs && (
                <p className="mt-1.5 text-xs text-transitional/90">
                  This instance's keys were imported, so their private halves are on
                  your machine — substitute the path to the one you imported.
                </p>
              )}
            </div>
          ) : (
            <div className="px-4 py-3 text-sm text-text-subtle">
              {instance.guest_os === 'windows' ? (
                /* Named specifically rather than folded into the generic
                   no-key case. "No key was injected" is true of a Windows
                   guest and sounds like something that went wrong; the real
                   reason is that Windows has neither cloud-init to inject with
                   nor an SSH server to inject for. */
                <>
                  No SSH — Windows has no cloud-init to receive a key and no SSH server
                  by default. Use the console. Once the OS is up you can enable Remote
                  Desktop inside it and add a forward for port 3389 below.
                </>
              ) : instance.ssh_enabled === false ? (
                'No SSH — no key was injected into this guest. The console is the way in.'
              ) : (
                'No address yet.'
              )}
            </div>
          )}

          <div className="border-t border-border px-4 py-3">
            <div className="mb-2 text-xs text-text-subtle">Key pairs installed at launch</div>
            {!installedKeys?.length ? (
              <p className="text-sm text-text-subtle">None — this instance is console-only.</p>
            ) : (
              <ul className="space-y-1.5">
                {installedKeys.map((key) => (
                  <li key={key.keypair_id} className="flex items-center gap-2 text-sm">
                    <KeyRound className="h-3.5 w-3.5 shrink-0 text-text-subtle" />
                    <span className="text-text">{key.name}</span>
                    {key.deleted && (
                      <span
                        title="This key pair has been deleted from the catalog. The key is still in the guest's authorized_keys — deleting the record did not reach into the VM."
                        className="rounded border border-transitional/30 bg-transitional-quiet px-1.5 py-0.5 text-2xs uppercase tracking-wide text-transitional"
                      >
                        deleted
                      </span>
                    )}
                    <code className="ml-auto truncate data text-2xs text-text-subtle">
                      {key.fingerprint}
                    </code>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {instance.console_caveat && (
            <div className="border-t border-border px-4 py-3 text-xs text-transitional/80">
              {instance.console_caveat}
            </div>
          )}
        </Section>

      {/* What reaches this guest. SSH included, marked derived. */}
      <Section title="Network">
        <InstanceForwards instanceId={instanceId} guestOs={instance.guest_os} />
      </Section>

      {/* Storage: both answers to "what disks does this have?" */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
      {/* Volumes attached to this instance, in the order the guest sees them. */}
      <Section title="Volumes">
        <InstanceVolumes instanceId={instanceId} stopped={instance.status === 'Stopped'} />
      </Section>

      {/* Snapshots */}
      <Section
        title="Snapshots"
        action={
          <button
            type="button"
            disabled={instance.status !== 'Stopped'}
            title={
              instance.status === 'Stopped'
                ? undefined
                : 'Stop the instance first — snapshots capture the disk at rest'
            }
            onClick={() => setSnapshotOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border-strong px-2.5 py-1 text-xs font-medium text-text-muted hover:bg-surface disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Camera className="h-3.5 w-3.5" />
            New snapshot
          </button>
        }
      >
        {!snapshots?.length ? (
          <p className="px-4 py-4 text-sm text-text-subtle">
            No snapshots. Stop the instance to capture its disk — restoring returns it to
            exactly that state.
          </p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <tbody>
              {snapshots.map((snap) => (
                <tr key={snap.id} className="border-b border-border last:border-0">
                  <td className="px-4 py-2.5">
                    <div className="font-medium text-text">{snap.name}</div>
                    {snap.description && (
                      <div className="text-xs text-text-subtle">{snap.description}</div>
                    )}
                    {snap.error_message && (
                      <div className="text-xs text-danger">{snap.error_message}</div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-text-muted">{snap.status}</td>
                  <td className="px-4 py-2.5 text-right text-xs text-text-subtle">
                    <TimeAgo value={snap.created_at} />
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center justify-end gap-1">
                      <button
                        type="button"
                        disabled={instance.status !== 'Stopped' || snap.status !== 'Available'}
                        title={
                          instance.status !== 'Stopped'
                            ? 'Stop the instance to restore'
                            : 'Restore — discards changes made since'
                        }
                        onClick={() => setRestoreTarget(snap)}
                        className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-transitional hover:bg-transitional-quiet disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <RotateCcw className="h-3 w-3" />
                        Restore
                      </button>
                      <button
                        type="button"
                        disabled={instance.status !== 'Stopped'}
                        onClick={() => setDeleteTarget(snap)}
                        className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-danger hover:bg-danger-quiet disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <Trash2 className="h-3 w-3" />
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Section title="Configuration">
          <Field label="Engine" value={instance.engine} />
          <Field label="Boot source" value={instance.iso ?? instance.boot_source} />
          <Field
            label="Size"
            value={
              instance.cpus
                ? `${instance.cpus} vCPU · ${formatMemory(instance.memory_mb ?? 0)} · ${instance.disk_gb} GB`
                : instance.flavor
            }
          />
          <Field label="Preset" value={instance.flavor} />
          <Field label="Accelerator" value={instance.accel ?? '—'} />
          <Field label="Display" value={instance.display ?? '—'} />
          <Field label="Address" value={displayAddress(instance) ?? '—'} mono />
          <Field
            label="Ports"
            value={
              instance.vnc_port
                ? `vnc ${instance.vnc_port} · qmp ${instance.qmp_port}`
                : '—'
            }
            mono
          />
          <Field label="PID" value={instance.pid ? String(instance.pid) : '—'} mono />
          <FieldNode label="Created">
            <TimeAgo value={instance.created_at} />
          </FieldNode>
          <FieldNode label="Updated">
            <TimeAgo value={instance.updated_at} />
          </FieldNode>
        </Section>

        <Section title="User-data">
          {instance.user_data ? (
            <pre className="max-h-72 overflow-auto px-4 py-3 data text-xs leading-relaxed text-text-muted">
              {instance.user_data}
            </pre>
          ) : (
            <p className="px-4 py-3 text-sm text-text-subtle">
              None supplied. This instance was built from the generated cloud-config
              alone — default user, selected keys, package refresh.
            </p>
          )}
        </Section>
      </div>

        <Section title="Activity">
          <Activity instanceId={instanceId} />
        </Section>

      <ConsoleModal instance={consoleOpen ? instance : null} onClose={() => setConsoleOpen(false)} />
      <CreateSnapshotModal
        instanceId={instanceId}
        instanceName={instance.name}
        open={snapshotOpen}
        onClose={() => setSnapshotOpen(false)}
      />

      <ConfirmDialog
        open={confirmTerminate}
        title="Terminate instance"
        message={
          <>
            This permanently deletes and purges{' '}
            <span className="font-semibold text-text">{instance.name}</span>, including
            its {snapshots?.length ?? 0} snapshot(s). This cannot be undone.
          </>
        }
        confirmLabel="Terminate"
        busy={terminateMut.isPending}
        onConfirm={async () => {
          await run(() => terminateMut.mutateAsync(instance.id))
          setConfirmTerminate(false)
          navigate('/')
        }}
        onCancel={() => setConfirmTerminate(false)}
      />

      <ConfirmDialog
        open={restoreTarget !== null}
        title="Restore snapshot"
        message={
          <>
            This returns <span className="font-semibold text-text">{instance.name}</span>{' '}
            to <span className="font-semibold text-text">{restoreTarget?.name}</span> and{' '}
            <span className="text-transitional">discards every change made since</span> (
            {restoreTarget && relativeTime(restoreTarget.created_at)}).
          </>
        }
        confirmLabel="Restore"
        busy={restoreMut.isPending}
        onConfirm={async () => {
          if (restoreTarget) await run(() => restoreMut.mutateAsync(restoreTarget.id))
          setRestoreTarget(null)
        }}
        onCancel={() => setRestoreTarget(null)}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete snapshot"
        message={
          <>
            This deletes the snapshot{' '}
            <span className="font-semibold text-text">{deleteTarget?.name}</span>. The
            instance itself is untouched.
          </>
        }
        confirmLabel="Delete"
        busy={deleteSnapMut.isPending}
        onConfirm={async () => {
          if (deleteTarget) await run(() => deleteSnapMut.mutateAsync(deleteTarget.id))
          setDeleteTarget(null)
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  )
}

/**
 * What happened to this instance, read from the event log.
 *
 * This used to be derived from timestamps, and carried a caveat saying so:
 * `updated_at` is a single field, so a stop/start collapsed into one entry and
 * an earlier error was overwritten by a later one. The rows now come from an
 * append-only table, so the caveat is gone and the list is the history rather
 * than a reconstruction of it.
 */
function Activity({ instanceId }: { instanceId: string }) {
  const { data: events, isLoading, isError } = useInstanceEvents(instanceId)
  const [expanded, setExpanded] = useState<string | null>(null)

  if (isLoading) {
    return <p className="px-4 py-3 text-sm text-text-subtle">Loading…</p>
  }
  if (isError) {
    return <p className="px-4 py-3 text-sm text-text-subtle">The event log could not be read.</p>
  }
  if (!events || events.length === 0) {
    return (
      <p className="px-4 py-3 text-sm text-text-subtle">
        Nothing recorded yet. Instances created before this version have no history —
        events are recorded from the moment they happen and nothing is back-dated.
      </p>
    )
  }

  return (
    <div className="px-4 py-3">
      <ol className="space-y-1">
        {events.map((event) => {
          const { Icon, tone } = eventStyle(event.kind)
          const open = expanded === event.id
          const hasDetail = Boolean(event.detail)
          return (
            <li key={event.id}>
              <div
                className={`flex items-start gap-3 rounded-md py-1.5 text-sm ${
                  hasDetail ? 'cursor-pointer hover:bg-surface-overlay' : ''
                }`}
                onClick={hasDetail ? () => setExpanded(open ? null : event.id) : undefined}
              >
                <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${tone}`} />
                <span className="min-w-0 flex-1">
                  <span className="text-text">{event.summary}</span>
                  {event.actor === 'reconciler' && (
                    <span
                      className="ml-2 rounded bg-surface-overlay px-1.5 py-0.5 text-2xs uppercase tracking-wide text-text-muted"
                      title="Nobody asked for this — the background reconciler found the hypervisor disagreeing with the record and corrected it."
                    >
                      auto
                    </span>
                  )}
                  {hasDetail && (
                    <span className="ml-2 text-xs text-text-subtle">{open ? '−' : '+'}</span>
                  )}
                </span>
                <span
                  className="shrink-0 text-xs text-text-subtle"
                >
                  <TimeAgo value={event.occurred_at} />
                </span>
              </div>
              {open && event.detail && (
                <pre className="mb-1 ml-7 overflow-x-auto whitespace-pre-wrap rounded bg-surface px-3 py-2 data text-xs leading-relaxed text-text-muted">
                  {event.detail}
                </pre>
              )}
            </li>
          )
        })}
      </ol>
      {events.length >= 50 && (
        <p className="mt-3 border-t border-border pt-2 text-xs text-text-subtle">
          Showing the 50 most recent.
        </p>
      )}
    </div>
  )
}

/**
 * Port forwards reaching this instance.
 *
 * The SSH forward is listed first and rendered as derived: dimmed, badged, and
 * with no delete control at all. It is not a row in the forwards table — it
 * comes from the instance's pinned port — and offering a button that fails, or
 * worse appears to work and then shows the row again on the next poll, is worse
 * than offering nothing.
 */
function InstanceForwards({
  instanceId,
  guestOs,
}: {
  instanceId: string
  guestOs?: GuestOS
}) {
  const { data: forwards, isLoading } = useForwards(instanceId)
  const { data: presets } = useForwardPresets(guestOs)
  const addMut = useAddForward(instanceId)
  const removeMut = useRemoveForward(instanceId)
  const [hostPort, setHostPort] = useState('')
  const [guestPort, setGuestPort] = useState('')
  const [error, setError] = useState<string | null>(null)

  if (isLoading) return <p className="px-4 py-3 text-sm text-text-subtle">Loading…</p>

  return (
    <div className="px-4 py-3">
      <ul className="space-y-1.5">
        {(forwards ?? []).map((forward) => (
          <li key={forward.id} className="flex items-center gap-3 text-sm">
            <NetworkIcon
              className={`h-3.5 w-3.5 shrink-0 ${
                forward.derived ? 'text-text-subtle' : 'text-text-subtle'
              }`}
            />
            <span className={forward.derived ? 'text-text-subtle' : 'text-text'}>
              127.0.0.1:{forward.host_port}
            </span>
            <span className="text-text-subtle">→</span>
            <span className={forward.derived ? 'text-text-subtle' : 'text-text-muted'}>
              guest:{forward.guest_port}
            </span>
            <span className="text-xs text-text-subtle">{forward.protocol}</span>
            {forward.derived ? (
              <span
                className="rounded bg-surface-overlay px-1.5 py-0.5 text-2xs uppercase tracking-wide text-text-subtle"
                title="Created with the instance and pinned for its lifetime. It cannot be removed — terminate the instance to release the port."
              >
                derived
              </span>
            ) : (
              <button
                type="button"
                disabled={removeMut.isPending}
                onClick={async () => {
                  setError(null)
                  try {
                    await removeMut.mutateAsync(forward.id)
                  } catch (err) {
                    setError(apiErrorMessage(err))
                  }
                }}
                className="ml-auto rounded px-2 py-0.5 text-xs text-transitional hover:bg-transitional-quiet"
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>

      {/* Presets. One click fills the form in; it does not create the forward,
          so the host port stays visible and editable and the collision check
          still runs on submit. RDP is the reason this exists — it is the access
          path for a Windows guest once the console has got the OS installed. */}
      {!!presets?.length && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <span className="text-xs text-text-subtle">Common:</span>
          {presets.map((preset) => (
            <button
              key={preset.key}
              type="button"
              title={preset.description}
              onClick={() => {
                setGuestPort(String(preset.guest_port))
                // Same number on the host unless it is taken — the backend
                // refuses a collision and names what holds it, which is a
                // better answer than picking a surprising port silently.
                setHostPort(String(preset.guest_port))
              }}
              className="rounded-md border border-border-strong bg-surface px-2 py-1 text-xs text-text hover:bg-surface-overlay"
            >
              {preset.label}
            </button>
          ))}
        </div>
      )}

      <form
        className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3"
        onSubmit={async (e) => {
          e.preventDefault()
          setError(null)
          try {
            await addMut.mutateAsync({
              host_port: Number(hostPort),
              guest_port: Number(guestPort),
            })
            setHostPort('')
            setGuestPort('')
          } catch (err) {
            setError(apiErrorMessage(err))
          }
        }}
      >
        <input
          value={hostPort}
          onChange={(e) => setHostPort(e.target.value)}
          placeholder="host port"
          inputMode="numeric"
          className="w-28 rounded border border-border-strong bg-surface px-2 py-1 text-sm text-text focus:border-border-strong focus:outline-none"
        />
        <span className="text-text-subtle">→</span>
        <input
          value={guestPort}
          onChange={(e) => setGuestPort(e.target.value)}
          placeholder="guest port"
          inputMode="numeric"
          className="w-28 rounded border border-border-strong bg-surface px-2 py-1 text-sm text-text focus:border-border-strong focus:outline-none"
        />
        <button
          type="submit"
          disabled={!hostPort || !guestPort || addMut.isPending}
          className="rounded-md bg-surface-overlay px-3 py-1 text-sm font-medium text-text hover:bg-border-strong disabled:opacity-50"
        >
          Forward
        </button>
        <span className="text-xs text-text-subtle">Applies immediately — no restart.</span>
      </form>

      {/* Said plainly because the failure it prevents looks like a broken
          forward: expose a web server, try it from your phone, conclude the
          feature does not work. */}
      <p className="mt-2 text-xs text-text-subtle">
        Forwards bind to <code className="text-text-subtle">127.0.0.1</code> only — reachable
        from this machine, not from other devices on your network.
      </p>

      {error && <p className="mt-2 text-sm text-danger">{error}</p>}
    </div>
  )
}

/**
 * The disks attached to this instance, in attach order.
 *
 * Order is shown because it is what decides the guest's device names: the
 * engine emits `-drive` arguments in this sequence and QEMU enumerates
 * virtio-blk in argument order, so the first row is `/dev/vdb` on a Linux
 * guest — and `Disk 1` on a Windows one, which is why the name comes from
 * `deviceHint` rather than being spelled out here.
 */
function InstanceVolumes({ instanceId, stopped }: { instanceId: string; stopped: boolean }) {
  const { data: volumes, isLoading } = useVolumes(instanceId)
  const detachMut = useDetachVolume()
  const [error, setError] = useState<string | null>(null)

  if (isLoading) return <p className="px-4 py-3 text-sm text-text-subtle">Loading…</p>
  if (!volumes || volumes.length === 0) {
    return (
      <p className="px-4 py-3 text-sm text-text-subtle">
        No volumes attached. Attach one from the Volumes tab while this instance is
        stopped — it appears in the guest on the next start.
      </p>
    )
  }

  return (
    <div className="px-4 py-3">
      {error && <p className="mb-2 text-sm text-danger">{error}</p>}
      <ul className="space-y-1.5">
        {volumes.map((volume) => (
          <li key={volume.id} className="flex items-center gap-3 text-sm">
            <Database className="h-3.5 w-3.5 shrink-0 text-text-subtle" />
            <span className="text-text">{volume.name}</span>
            <span className="text-text-subtle">{volume.size_gb} GB</span>
            <span className="data text-xs text-text-subtle">{deviceHint(volume)}</span>
            <button
              type="button"
              disabled={!stopped || detachMut.isPending}
              title={stopped ? 'Detach this volume' : 'Stop the instance to detach'}
              onClick={async () => {
                setError(null)
                try {
                  await detachMut.mutateAsync(volume.id)
                } catch (err) {
                  setError(apiErrorMessage(err))
                }
              }}
              className="ml-auto rounded px-2 py-0.5 text-xs text-transitional hover:bg-transitional-quiet disabled:cursor-not-allowed disabled:text-text-subtle disabled:hover:bg-transparent"
            >
              Detach
            </button>
          </li>
        ))}
      </ul>
      <p className="mt-3 border-t border-border pt-2 text-xs text-text-subtle">
        Device names follow this order. Snapshots do not include volumes — they capture
        this instance's own disk only.
      </p>
    </div>
  )
}

function Section({
  title,
  action,
  children,
}: {
  title: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-border">
      <header className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-semibold text-text">{title}</h2>
        {action}
      </header>
      {children}
    </section>
  )
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <FieldNode label={label} mono={mono}>
      {value}
    </FieldNode>
  )
}

/** The same row, for a value that is a node rather than a string. */
function FieldNode({
  label,
  mono = false,
  children,
}: {
  label: string
  mono?: boolean
  children: React.ReactNode
}) {
  return (
    <div className="flex items-baseline gap-4 border-b border-border/50 px-4 py-2 last:border-0">
      <span className="w-28 shrink-0 text-xs text-text-subtle">{label}</span>
      <span className={`min-w-0 flex-1 break-words text-sm text-text ${mono ? 'data text-xs' : ''}`}>
        {children}
      </span>
    </div>
  )
}

function Action({
  icon: Icon,
  label,
  onClick,
  busy,
  disabled = false,
  title,
  className = '',
}: {
  icon: typeof Play
  label: string
  onClick: () => void
  busy: boolean
  disabled?: boolean
  title?: string
  className?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || disabled}
      title={title ?? label}
      className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${className}`}
    >
      {busy ? (
        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
      ) : (
        <Icon className="h-3.5 w-3.5" />
      )}
      {label}
    </button>
  )
}

function DetailSkeleton() {
  return (
    <div className="space-y-5">
      <div className="h-5 w-24 animate-pulse rounded bg-surface-overlay" />
      <div className="h-8 w-56 animate-pulse rounded bg-surface-overlay" />
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {[0, 1].map((i) => (
          <div key={i} className="h-64 animate-pulse rounded-lg border border-border bg-surface" />
        ))}
      </div>
    </div>
  )
}

function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-surface/30 px-6 py-16 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface-overlay">
        <Server className="h-7 w-7 text-text-subtle" />
      </div>
      <h3 className="mt-4 text-base font-semibold text-text">Instance not found</h3>
      <p className="mt-1 max-w-sm text-sm text-text-subtle">
        It may have been terminated, or the link may be from another backend.
      </p>
      <button
        type="button"
        onClick={() => navigate('/')}
        className="mt-5 inline-flex items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-fg hover:bg-accent-hover"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to instances
      </button>
    </div>
  )
}
