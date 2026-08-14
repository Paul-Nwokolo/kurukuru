/** Small formatting helpers shared across the table. */

/**
 * Parse a backend timestamp. The API serializes UTC times *without* a timezone
 * designator (e.g. "2026-08-06T21:37:24.07"), which browsers would otherwise
 * interpret as local time. Append "Z" when no offset/Z is present so these are
 * correctly read as UTC.
 */
function parseTimestamp(iso: string): number {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)
  return new Date(hasTz ? iso : `${iso}Z`).getTime()
}

/**
 * Compact relative time, e.g. "just now", "3m ago", "2h ago", "5d ago".
 * Falls back to a locale date for anything older than a week.
 */
export function relativeTime(iso: string): string {
  const then = parseTimestamp(iso)
  if (Number.isNaN(then)) return '—'
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (secs < 45) return 'just now'
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return new Date(iso).toLocaleDateString()
}

/**
 * Human-readable byte size, e.g. "3.5 GB", "595 MB".
 *
 * Binary units (1024) with decimal labels — matching what qemu-img prints, so
 * the dashboard and the CLI agree about how big an image is.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}

/** Instance memory, in whichever unit reads more naturally. */
export function formatMemory(mb: number): string {
  return mb >= 1024 && mb % 1024 === 0 ? `${mb / 1024} GB` : `${mb} MB`
}

/** Full timestamp for tooltips. */
export function absoluteTime(iso: string): string {
  const t = parseTimestamp(iso)
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString()
}

/**
 * How a volume's guest will name the disk.
 *
 * `/dev/vdb` is a *Linux* device name, and it was rendered unconditionally —
 * so a volume attached to a Windows guest advertised a path that guest has
 * never heard of, and sent the user looking for it. Windows numbers disks in
 * Disk Management instead, so that is what a Windows guest is told.
 *
 * Detached volumes have no guest and therefore no name yet; the em dash is the
 * honest answer, not a guess about what they will be attached to.
 */
export function deviceHint(volume: {
  device_hint: string | null
  windows_disk_hint: string | null
  attached_instance_guest_os: 'linux' | 'windows' | null
}): string {
  if (volume.attached_instance_guest_os === 'windows') {
    return volume.windows_disk_hint ?? '—'
  }
  return volume.device_hint ? `/dev/${volume.device_hint}` : '—'
}
