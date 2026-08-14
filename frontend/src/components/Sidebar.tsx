import {
  Activity,
  Database,
  Disc,
  FolderOpen,
  HardDrive,
  KeyRound,
  Network,
  Server,
  Settings,
} from 'lucide-react'
import { Wordmark } from '../ui/Wordmark'
import { Link } from './Link'
import { VIEWS, type NavKey } from '../lib/router'

export type { NavKey }

/**
 * Nine flat items was a list of features in the order they were built. These
 * are the same nine destinations under four quiet headings — grouping, not
 * nesting: nothing collapses and nothing is one click further away.
 *
 * The headings say what the product thinks these things are:
 *   Compute        the thing you run
 *   Resources      the things you attach to it
 *   Configuration  the things that describe how it runs
 *   System         the things that tell you what happened
 */
const SECTIONS: {
  label: string
  items: { key: NavKey; label: string; icon: typeof Server }[]
}[] = [
  {
    label: 'Compute',
    items: [{ key: 'instances', label: 'Instances', icon: Server }],
  },
  {
    label: 'Resources',
    items: [
      { key: 'images', label: 'Images', icon: HardDrive },
      { key: 'isos', label: 'ISOs', icon: Disc },
      { key: 'volumes', label: 'Volumes', icon: Database },
      { key: 'keypairs', label: 'Key pairs', icon: KeyRound },
    ],
  },
  {
    label: 'Configuration',
    items: [
      { key: 'networks', label: 'Networks', icon: Network },
      { key: 'projects', label: 'Projects', icon: FolderOpen },
    ],
  },
  {
    label: 'System',
    items: [
      { key: 'activity', label: 'Activity', icon: Activity },
      { key: 'settings', label: 'Settings', icon: Settings },
    ],
  },
]

interface SidebarProps {
  /** null while a detail page is open — no tab is the current one. */
  active: NavKey | null
}

/**
 * Real links, not buttons.
 *
 * Each item is an `<a href>` that navigates in-app on a plain click and falls
 * through to the browser on a modified one — so ctrl-click opens Networks in a
 * new tab, the status bar previews the destination on hover, and the address
 * bar says where you are. As buttons calling `setState`, none of that was
 * true: every tab was the same URL, a reload dropped you back on Instances,
 * and Back left the app.
 */
export function Sidebar({ active }: SidebarProps) {
  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-surface">
      <div className="px-4 py-4">
        <Wordmark />
      </div>

      <nav className="mt-1 flex flex-1 flex-col gap-5 overflow-y-auto px-2 pb-4">
        {SECTIONS.map((section) => (
          <div key={section.label}>
            <div className="px-2 pb-1 text-2xs font-medium uppercase tracking-wider text-text-subtle">
              {section.label}
            </div>
            <div className="flex flex-col gap-px">
              {section.items.map(({ key, label, icon: Icon }) => {
                const isActive = key === active
                return (
                  <Link
                    key={key}
                    to={VIEWS[key].path}
                    aria-current={isActive ? 'page' : undefined}
                    className={[
                      'flex items-center gap-2.5 rounded px-2 py-1.5 text-sm no-underline transition-colors',
                      isActive
                        ? 'bg-accent-quiet font-medium text-accent-text'
                        : 'text-text-muted hover:bg-surface-overlay hover:text-text',
                    ].join(' ')}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    {label}
                  </Link>
                )
              })}
            </div>
          </div>
        ))}
      </nav>
      {/* The old footer nav (Volumes · Snapshots · Events · Projects) is gone:
          every one of those is a destination above it now, so it was a second
          navigation that could only ever disagree with the first. */}
    </aside>
  )
}
