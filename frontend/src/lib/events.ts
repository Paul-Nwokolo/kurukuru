import {
  AlertCircle,
  AlertTriangle,
  Camera,
  CheckCircle2,
  Copy,
  Database,
  HardDrive,
  Loader,
  Network,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Server,
  Square,
  Trash2,
  XCircle,
} from 'lucide-react'
import type { EventKind } from '../api/client'

/**
 * Icon per event kind. **Monochrome, except for failure.**
 *
 * These were five colours deep — cyan creates, green succeeds, magenta
 * restores, amber reconciles — next to text that already said what happened.
 * The colour was decorative, and it competed with the status dots elsewhere on
 * the page, which are not decorative at all.
 *
 * So: every icon inherits the surrounding text colour, and exactly two kinds
 * are red. If something in this feed is coloured, it went wrong.
 */
const ICONS: Record<EventKind, typeof Server> = {
  created: Plus,
  provisioning_started: Loader,
  provisioning_succeeded: CheckCircle2,
  provisioning_failed: XCircle,
  started: Play,
  stopped: Square,
  terminated: Trash2,
  force_terminated: Trash2,
  errored: AlertCircle,
  snapshot_created: Camera,
  snapshot_restored: RotateCcw,
  snapshot_deleted: Trash2,
  restarted: RotateCw,
  cloned: Copy,
  volume_attached: Database,
  volume_detached: Database,
  port_forward_added: Network,
  port_forward_removed: Network,
  reconciled: RefreshCw,
  image_import: HardDrive,
  // The backend restarted this VM on its own, unasked — deliberately a
  // different pictogram from `restarted` (a plain RotateCw), so a user
  // scanning the feed can tell "you did this" from "the backend did this to
  // your VM without being asked". A heuristic workaround for an upstream
  // QEMU/WHPX defect; see docs/WINDOWS.md.
  auto_restarted: AlertTriangle,
}

/** The only kinds that earn a colour: the ones that are a failure. */
const FAILURES = new Set<EventKind>(['provisioning_failed', 'errored'])

export function eventStyle(kind: EventKind): { Icon: typeof Server; tone: string } {
  return {
    Icon: ICONS[kind],
    tone: FAILURES.has(kind) ? 'text-danger' : 'text-text-subtle',
  }
}
