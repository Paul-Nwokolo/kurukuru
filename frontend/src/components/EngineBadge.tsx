import type { EngineName } from '../api/client'
import { Badge } from '../ui/Badge'

/**
 * Small per-row tag naming the hypervisor driver behind an instance.
 *
 * Deliberately quiet: the engine is a detail of *how* a VM runs, not what it
 * is, so it reads as a caption next to the name rather than competing with the
 * status badge. It used to be violet, which is now the accent — a tag that
 * shares a colour with the primary action reads as an action.
 *
 * It stays in the UI after the move to a single engine precisely because of
 * the retired rows — a Multipass instance from before the switch must still say
 * so, and be visibly different from anything that can be launched today.
 */
const TITLES: Record<EngineName, string> = {
  qemu: 'Managed directly by QEMU',
  multipass:
    'Retired engine — this instance predates the switch to QEMU. ' +
    'It can no longer be started or stopped, only terminated.',
}

export function EngineBadge({ engine }: { engine: EngineName }) {
  const title = TITLES[engine] ?? `Engine: ${engine}`
  // A retired engine reads dimmer than a live one; an engine the frontend does
  // not know about must still render as *something* rather than crash a table.
  const tone = engine === 'qemu' ? 'default' : 'quiet'
  return (
    <Badge tone={tone} title={title}>
      {engine}
    </Badge>
  )
}
