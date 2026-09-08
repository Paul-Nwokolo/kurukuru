import { useId, useState } from 'react'
import { Check, ChevronDown, Network as NetworkIcon } from 'lucide-react'
import { useNetworkModes, useNetworks } from '../hooks/queries'
import type { NetworkModeInfo } from '../api/client'

/**
 * Networks, and an honest account of what this build cannot do.
 *
 * The deferred modes are presented as information about *this machine* — the
 * driver and the privilege each would need — rather than as greyed-out buttons.
 * A disabled control tells you that you cannot do something; this tells you
 * why, and what would change it. The text comes from the API so it cannot
 * drift from what the backend actually measured.
 *
 * **They are collapsed, though, and that is a hierarchy fix rather than a
 * hedge.** Rendered open, the two unavailable modes occupied more of the page
 * than the one that works: their explanations are the longest text here — the
 * bridged mode's requirements run to a paragraph per platform — so the page
 * read as a list of failures with the working configuration lost among them.
 * Each row still states what the mode *is* and that it is unavailable without
 * being opened, so the user wondering whether they can bridge to the LAN gets
 * that answer at a glance; the measurement behind it is one click away instead
 * of in front of the thing they are actually using.
 */
export function NetworksView() {
  const { data: networks } = useNetworks()
  const { data: modes } = useNetworkModes()

  return (
    <div className="space-y-6">
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-surface text-left text-xs uppercase tracking-wide text-text-subtle">
            <tr>
              <th className="px-4 py-2.5 font-medium">Name</th>
              <th className="px-4 py-2.5 font-medium">Mode</th>
              <th className="px-4 py-2.5 font-medium">Subnet</th>
              <th className="px-4 py-2.5 text-right font-medium">Instances</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {(networks ?? []).map((network) => (
              <tr key={network.id}>
                <td className="px-4 py-2.5">
                  <span className="flex items-center gap-2">
                    <NetworkIcon className="h-3.5 w-3.5 text-text-subtle" />
                    <span className="font-medium text-text">{network.name}</span>
                    {network.is_default && (
                      <span className="rounded bg-surface-overlay px-1.5 py-0.5 text-2xs uppercase tracking-wide text-text-muted">
                        default
                      </span>
                    )}
                  </span>
                </td>
                <td className="px-4 py-2.5 text-text-muted">{network.mode}</td>
                <td className="px-4 py-2.5 font-mono text-xs text-text-subtle">
                  {network.cidr ?? '—'}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-text-muted">
                  {network.instance_count}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* The working mode, and how to use it, together. The instruction about
          forwards belongs here rather than in a footnote at the bottom of the
          page: it is what you do with this mode, not a remark about the page. */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-text">Mode in use</h2>
        {(modes?.available ?? []).map((mode) => (
          <div
            key={mode.mode}
            className="rounded-lg border border-healthy/30 bg-healthy-quiet px-4 py-3"
          >
            <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-healthy">
              <Check className="h-4 w-4 shrink-0" />
              {mode.label}
              <span className="data text-xs text-healthy/70">{mode.mode}</span>
            </div>
            <p className="mt-1 text-sm text-text-muted">{mode.summary}</p>
            <p className="mt-2 text-sm text-text-muted">
              Every instance is on it and reached through forwarded host ports. Add a
              forward from an instance's detail page — it takes effect immediately, with
              no restart.
            </p>
          </div>
        ))}
      </section>

      {(modes?.deferred ?? []).length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-text-muted">
            Not available on this machine
          </h2>
          <p className="text-xs text-text-subtle">
            Listed with what each would need, because "why can't I bridge to my LAN?"
            deserves an answer rather than a search. Measured on this host.
          </p>
          <div className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {(modes?.deferred ?? []).map((mode) => (
              <DeferredMode key={mode.mode} mode={mode} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

/** One unavailable mode: what it is, always; why not, on request. */
function DeferredMode({ mode }: { mode: NetworkModeInfo }) {
  const [open, setOpen] = useState(false)
  const detailId = useId()
  const hasDetail = Boolean(mode.blocker || mode.requires)

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        // Only while the panel exists — a dangling reference otherwise.
        aria-controls={open ? detailId : undefined}
        disabled={!hasDetail}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-surface-overlay disabled:cursor-default disabled:hover:bg-transparent"
      >
        <span className="text-sm font-medium text-text-muted">{mode.label}</span>
        <span className="data text-xs text-text-subtle">{mode.mode}</span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          <span className="rounded bg-surface px-1.5 py-0.5 text-2xs uppercase tracking-wide text-text-subtle">
            not available
          </span>
          {hasDetail && (
            <ChevronDown
              aria-hidden
              className={`h-4 w-4 text-text-subtle transition-transform ${open ? 'rotate-180' : ''}`}
            />
          )}
        </span>
      </button>

      {/* The one-line description stays visible. Knowing *what* bridged mode is
          costs nothing to show and is most of what a passing reader wants. */}
      <p className="px-4 pb-2.5 text-sm text-text-subtle">{mode.summary}</p>

      {open && hasDetail && (
        <dl id={detailId} className="space-y-2 border-t border-border bg-surface px-4 py-3">
          {mode.blocker && (
            <div>
              <dt className="text-2xs font-medium uppercase tracking-wide text-text-muted">
                Why not
              </dt>
              <dd className="mt-0.5 text-xs leading-relaxed text-text-subtle">
                {mode.blocker}
              </dd>
            </div>
          )}
          {mode.requires && (
            <div>
              <dt className="text-2xs font-medium uppercase tracking-wide text-text-muted">
                Would need
              </dt>
              <dd className="mt-0.5 text-xs leading-relaxed text-text-subtle">
                {mode.requires}
              </dd>
            </div>
          )}
        </dl>
      )}
    </div>
  )
}
