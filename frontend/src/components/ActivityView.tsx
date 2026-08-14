import { useState } from 'react'
import { Link } from './Link'
import type { EventKind, InstanceEvent } from '../api/client'
import { useEvents } from '../hooks/queries'
import { absoluteTime, relativeTime } from '../lib/format'
import { eventStyle } from '../lib/events'

/**
 * The whole install's history, newest first.
 *
 * The per-instance view answers "what happened to this VM". This one answers
 * the question you have before you know which VM to blame — and it is also the
 * only place events with no instance (an image import) are visible at all.
 */
export function ActivityView() {
  const { data: events, isLoading, isError } = useEvents()
  const [kind, setKind] = useState<EventKind | 'all'>('all')

  if (isLoading) return <p className="text-sm text-text-subtle">Loading…</p>
  if (isError) return <p className="text-sm text-text-subtle">The event log could not be read.</p>

  const all = events ?? []
  const shown = kind === 'all' ? all : all.filter((e) => e.kind === kind)
  // Only kinds that actually occurred: a filter listing fourteen options of
  // which twelve match nothing is a worse control than one listing two.
  const present = [...new Set(all.map((e) => e.kind))].sort()

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <FilterChip active={kind === 'all'} onClick={() => setKind('all')}>
          All
        </FilterChip>
        {present.map((k) => (
          <FilterChip key={k} active={kind === k} onClick={() => setKind(k)}>
            {k.replace(/_/g, ' ')}
          </FilterChip>
        ))}
      </div>

      {shown.length === 0 ? (
        <p className="rounded-lg border border-border bg-surface-overlay px-4 py-8 text-center text-sm text-text-subtle">
          Nothing recorded yet.
        </p>
      ) : (
        <ol className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface-overlay">
          {shown.map((event) => (
            <Row key={event.id} event={event} />
          ))}
        </ol>
      )}

      {all.length >= 100 && (
        <p className="text-xs text-text-subtle">
          Showing the 100 most recent. Older entries are kept until they pass the retention
          window (see Settings).
        </p>
      )}
    </div>
  )
}

function Row({ event }: { event: InstanceEvent }) {
  const [open, setOpen] = useState(false)
  const { Icon, tone } = eventStyle(event.kind)
  const hasDetail = Boolean(event.detail)

  return (
    <li>
      <div
        className={`flex items-start gap-3 px-4 py-2.5 text-sm ${
          hasDetail ? 'cursor-pointer hover:bg-surface-overlay' : ''
        }`}
        onClick={hasDetail ? () => setOpen(!open) : undefined}
      >
        <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${tone}`} />
        <span className="w-40 shrink-0 truncate text-text-muted">
          {event.instance_id ? (
            // Stops the row's expand toggle from firing on the way out.
            <span onClick={(e) => e.stopPropagation()}>
              <Link
                to={`/instances/${event.instance_id}`}
                className="hover:text-text hover:underline"
              >
                {event.instance_name || event.instance_id}
              </Link>
            </span>
          ) : (
            <span className="text-text-subtle">—</span>
          )}
        </span>
        <span className="min-w-0 flex-1 text-text">
          {event.summary}
          {event.actor === 'reconciler' && (
            <span
              className="ml-2 rounded bg-surface-overlay px-1.5 py-0.5 text-2xs uppercase tracking-wide text-text-muted"
              title="Nobody asked for this — the reconciler found the hypervisor disagreeing with the record and corrected it."
            >
              auto
            </span>
          )}
          {hasDetail && <span className="ml-2 text-xs text-text-subtle">{open ? '−' : '+'}</span>}
        </span>
        <span className="shrink-0 text-xs text-text-subtle" title={absoluteTime(event.occurred_at)}>
          {relativeTime(event.occurred_at)}
        </span>
      </div>
      {open && event.detail && (
        <pre className="overflow-x-auto whitespace-pre-wrap bg-surface px-4 py-2.5 pl-11 font-mono text-xs leading-relaxed text-text-muted">
          {event.detail}
        </pre>
      )}
    </li>
  )
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
        active
          ? 'bg-accent text-text-inverse'
          : 'bg-surface text-text-muted ring-1 ring-border hover:text-text'
      }`}
    >
      {children}
    </button>
  )
}
