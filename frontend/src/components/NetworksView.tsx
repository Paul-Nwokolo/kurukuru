import { Check, Lock, Network as NetworkIcon } from 'lucide-react'
import { useNetworkModes, useNetworks } from '../hooks/queries'

/**
 * Networks, and an honest account of what this build cannot do.
 *
 * The deferred modes are presented as information about *this machine* — the
 * driver and the privilege each would need — rather than as greyed-out buttons.
 * A disabled control tells you that you cannot do something; this tells you
 * why, and what would change it. The text comes from the API so it cannot
 * drift from what the backend actually measured.
 */
export function NetworksView() {
  const { data: networks } = useNetworks()
  const { data: modes } = useNetworkModes()

  return (
    <div className="space-y-5">
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

      <div className="space-y-3">
        <h2 className="text-sm font-semibold text-text-muted">Modes</h2>

        {(modes?.available ?? []).map((mode) => (
          <div
            key={mode.mode}
            className="rounded-lg border border-healthy/30 bg-healthy-quiet px-4 py-3"
          >
            <div className="flex items-center gap-2 text-sm font-medium text-healthy">
              <Check className="h-4 w-4" />
              {mode.label}
              <span className="font-mono text-xs text-healthy/70">{mode.mode}</span>
            </div>
            <p className="mt-1 text-sm text-text-muted">{mode.summary}</p>
          </div>
        ))}

        {(modes?.deferred ?? []).map((mode) => (
          <div key={mode.mode} className="rounded-lg border border-border bg-surface-overlay px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-text-muted">
              <Lock className="h-4 w-4 text-text-subtle" />
              {mode.label}
              <span className="font-mono text-xs text-text-subtle">{mode.mode}</span>
              <span className="ml-auto rounded bg-surface-overlay px-1.5 py-0.5 text-2xs uppercase tracking-wide text-text-muted">
                not available
              </span>
            </div>
            <p className="mt-1 text-sm text-text-muted">{mode.summary}</p>
            {mode.blocker && (
              <p className="mt-2 text-xs text-text-subtle">
                <span className="text-text-muted">Why not: </span>
                {mode.blocker}
              </p>
            )}
            {mode.requires && (
              <p className="mt-1 text-xs text-text-subtle">
                <span className="text-text-muted">Would need: </span>
                {mode.requires}
              </p>
            )}
          </div>
        ))}
      </div>

      <p className="text-xs text-text-subtle">
        Every instance is on user-mode NAT and reached through forwarded host ports.
        Add a forward from an instance's detail page — it takes effect immediately, with
        no restart.
      </p>
    </div>
  )
}
