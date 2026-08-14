import type { ReactNode } from 'react'

/**
 * State, and only state.
 *
 * Every status in the product reduces to one of four conditions, and each maps
 * to exactly one colour. The vocabulary is deliberately this small: an
 * instance that is Running, an image that is Available and a volume that is
 * Attached are all "this is working", and giving them three different greens
 * would be three ways of saying the same thing.
 *
 *   healthy      — working as intended (Running, Available, Attached)
 *   transitional — in flight, will settle on its own (Provisioning, Creating)
 *   danger       — failed, needs a human (Error)
 *   neutral      — real but inert (Stopped, Terminated). Grey, not a colour:
 *                  a stopped VM is not a warning.
 */
export type StatusTone = 'healthy' | 'transitional' | 'danger' | 'neutral'

const TONES: Record<StatusTone, { dot: string; text: string }> = {
  healthy: { dot: 'bg-healthy', text: 'text-healthy' },
  transitional: { dot: 'bg-transitional', text: 'text-transitional' },
  danger: { dot: 'bg-danger', text: 'text-danger' },
  neutral: { dot: 'bg-text-subtle', text: 'text-text-muted' },
}

/**
 * Every status string the API can produce, mapped to a tone.
 *
 * One table for instances, images, volumes, snapshots and networks. When a new
 * resource arrives with a status of "Available", it is already correct here —
 * which is the point of keeping this centralised rather than per-table.
 */
const TONE_BY_STATUS: Record<string, StatusTone> = {
  // Instances
  Running: 'healthy',
  Pending: 'transitional',
  Provisioning: 'transitional',
  Stopped: 'neutral',
  Terminated: 'neutral',
  Error: 'danger',
  // A Running instance that never published an address. Amber, not green: a
  // green dot on something unreachable is the dashboard lying.
  Degraded: 'transitional',
  // Images
  Importing: 'transitional',
  Available: 'healthy',
  // Volumes
  Creating: 'transitional',
  Attached: 'healthy',
  // Snapshots
  Deleting: 'transitional',
}

/** Statuses that are still moving, and so get a pulsing dot. */
const PULSING = new Set(['Pending', 'Provisioning', 'Importing', 'Creating', 'Deleting'])

function toneFor(status: string): StatusTone {
  return TONE_BY_STATUS[status] ?? 'neutral'
}

interface StatusBadgeProps {
  status: string
  /** Overrides the text while keeping the status's tone — lets a table say
   *  "Attached to web-01" without inventing a second visual language. */
  label?: ReactNode
  /** Dot only, for dense rows where the column header already says what it is. */
  dotOnly?: boolean
}

export function StatusBadge({ status, label, dotOnly = false }: StatusBadgeProps) {
  const tone = TONES[toneFor(status)]
  const pulse = PULSING.has(status)

  return (
    <span className="inline-flex items-center gap-2" title={dotOnly ? status : undefined}>
      <span className="relative flex h-2 w-2 shrink-0">
        {pulse && (
          <span
            className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${tone.dot}`}
          />
        )}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${tone.dot}`} />
      </span>
      {!dotOnly && (
        <span className={`text-xs font-medium ${tone.text}`}>{label ?? status}</span>
      )}
    </span>
  )
}
