import { AlertCircle, CheckCircle2, Cpu, HardDrive, KeyRound, Settings2 } from 'lucide-react'
import { useDiagnostics, useSettings } from '../hooks/queries'
import { AccountSection } from './AccountSection'
import { PathValue } from '../ui/Feedback'
import { formatBytes } from '../lib/format'

/**
 * What this backend is running with, and how to change it.
 *
 * Read-only, and honest about why: settings are read once at startup and
 * cached, and several name directories that VMs already on disk depend on.
 * So every row shows the KURUKURU_ environment variable that controls it — a
 * settings screen that shows a value without saying how to change it has told
 * the user half of what they came for.
 *
 * Live host facts come from /diagnostics (the same endpoint `iaas doctor`
 * reads); the configuration comes from /settings.
 */
export function SettingsView() {
  const { data: groups, isLoading } = useSettings()
  const { data: diag } = useDiagnostics()

  return (
    <div className="max-w-4xl space-y-6">
      {/* Account first: it is the only thing on this page that is a control
          rather than a readout, and it is where someone arrives when they came
          to rotate a token or change a password. */}
      <AccountSection />

      {/* Then live status — it's what changes, and what people check. */}
      <section className="rounded-lg border border-border">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-text">Host</h2>
          <p className="mt-0.5 text-xs text-text-subtle">
            Live state of the machine this backend runs on.
          </p>
        </header>
        <div className="grid grid-cols-1 gap-px bg-surface-overlay sm:grid-cols-2">
          <Fact
            icon={Cpu}
            label="Accelerator"
            value={diag?.engine.accel ?? '—'}
            ok={diag?.engine.accel_available}
            note={
              diag && !diag.engine.accel_available
                ? 'Software emulation — VMs boot roughly 30x slower'
                : undefined
            }
          />
          <Fact
            icon={Settings2}
            label="QEMU"
            value={diag?.engine.version?.replace(/^QEMU emulator version /, '') ?? '—'}
            ok={diag?.engine.available}
          />
          <Fact
            icon={HardDrive}
            label="Instance store"
            value={
              diag
                ? `${formatBytes(diag.instance_store.free_bytes ?? 0)} free`
                : '—'
            }
            ok={diag?.instance_store.writable}
            note={diag?.instance_store.path}
            path
          />
          <Fact
            icon={KeyRound}
            label="Orchestrator key"
            value={diag?.ssh_key.present ? 'present' : 'missing'}
            ok={diag?.ssh_key.present}
            note={diag?.ssh_key.private_key_path ?? diag?.ssh_key.error ?? undefined}
            // An error message in the same slot is prose, not a path; only
            // dress it as one when it actually is one.
            path={Boolean(diag?.ssh_key.private_key_path)}
          />
        </div>
        <div className="border-t border-border px-4 py-2.5 text-xs text-text-subtle">
          API v{diag?.api.version ?? '—'} · Python {diag?.python ?? '—'} ·{' '}
          {/* Says what happens next rather than only what is absent. On a
              fresh install this is the normal state — the image is fetched
              the first time an instance launches — but "not downloaded
              yet" reads as a fault to somebody who has just installed and
              is looking for a reason nothing works. */}
          {diag?.engine.base_image_present
            ? 'base image downloaded'
            : 'base image downloads on first launch'}
        </div>
      </section>

      {/*
        What the installed hypervisor cannot do, and what follows from it.

        Separate from the QEMU version above on purpose. A version number
        cannot express "this build has no TPM, so Windows 11 will not install"
        — that is a property of how it was compiled, not of which release it
        is. Showing only the version, in green, over a machine that cannot do
        what the user is about to try would be the most reassuring possible way
        to be wrong.

        Advisory throughout: nothing here blocks a launch, so it is styled as a
        caveat rather than an error.
      */}
      {!!diag?.engine.support?.warnings?.length && (
        <section className="rounded-lg border border-transitional/30 bg-transitional-quiet">
          <header className="flex items-start gap-2 px-4 py-3">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-transitional" />
            <div>
              <h2 className="text-sm font-semibold text-transitional">
                Hypervisor limitations
              </h2>
              <p className="mt-0.5 text-xs text-transitional/90">
                Things this QEMU build cannot do. Nothing here stops the backend
                working — it is what to check before blaming something else.
              </p>
            </div>
          </header>
          <ul className="space-y-2 border-t border-transitional/20 px-4 py-3">
            {diag.engine.support.warnings.map((warning) => (
              <li key={warning} className="text-xs leading-relaxed text-transitional">
                {warning}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="rounded-lg border border-transitional/30 bg-transitional-quiet px-4 py-3 text-xs text-transitional">
        These values are read once when the backend starts. Set the environment
        variable shown against a setting (or put it in{' '}
        <code className="rounded bg-surface px-1 py-0.5">backend/.env</code>) and
        restart the backend for it to take effect. Paths in particular are load-bearing:
        moving the instance store while VMs exist strands their disks.
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {[0, 1].map((i) => (
            <div key={i} className="h-32 animate-pulse rounded-lg border border-border bg-surface-overlay" />
          ))}
        </div>
      ) : (
        groups?.map((group) => (
          <section key={group.name} className="rounded-lg border border-border">
            <header className="border-b border-border px-4 py-3">
              <h2 className="text-sm font-semibold text-text">{group.name}</h2>
              <p className="mt-0.5 text-xs text-text-subtle">{group.description}</p>
            </header>
            <table className="w-full border-collapse text-sm">
              <tbody>
                {group.settings.map((setting) => (
                  <tr key={setting.key} className="border-b border-border last:border-0">
                    <td className="w-56 px-4 py-2.5 align-top text-text-muted">
                      {setting.label}
                    </td>
                    <td className="px-4 py-2.5 align-top">
                      {setting.kind === 'path' ? (
                        <PathValue value={String(setting.value)} />
                      ) : (
                        <div className="break-all data text-xs text-text">
                          {String(setting.value)}
                        </div>
                      )}
                      <div className="mt-0.5 data text-2xs text-text-subtle">
                        {setting.env}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))
      )}
    </div>
  )
}

function Fact({
  icon: Icon,
  label,
  value,
  ok,
  note,
  path,
  mono = false,
}: {
  icon: typeof Cpu
  label: string
  value: string
  ok?: boolean
  note?: string
  /** Renders `note` as a path — mono, truncated, copyable, like every other. */
  path?: boolean
  mono?: boolean
}) {
  return (
    <div className="bg-surface px-4 py-3">
      <div className="flex items-center gap-2 text-xs text-text-subtle">
        <Icon className="h-3.5 w-3.5" />
        {label}
        {ok === true && <CheckCircle2 className="h-3 w-3 text-healthy" />}
        {ok === false && <AlertCircle className="h-3 w-3 text-transitional" />}
      </div>
      <div className={`mt-1 truncate text-sm text-text ${mono ? 'data text-xs' : ''}`}>
        {value}
      </div>
      {note &&
        (path ? (
          <PathValue dense className="mt-0.5" value={note} />
        ) : (
          <div className="mt-0.5 truncate data text-2xs text-text-subtle" title={note}>
            {note}
          </div>
        ))}
    </div>
  )
}
