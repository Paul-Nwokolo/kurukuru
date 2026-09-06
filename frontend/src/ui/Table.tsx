import type { ReactNode } from 'react'

/**
 * One table.
 *
 * Six tables in this app had six header treatments, three row heights and two
 * different ideas about where the Actions column goes. This is the standard
 * they all move to: a bordered card, a quiet uppercase header, no zebra
 * striping (the row rule does that job without adding a second background),
 * and hover feedback because rows are clickable in several places.
 *
 * Alignment is a rule rather than a per-table choice: anything numeric or
 * size-like is right-aligned and set in mono, because columns of figures are
 * read by comparing them vertically.
 */
/**
 * `min-w` is what makes a narrow window survivable.
 *
 * The scroll container was always here; the table inside it was `w-full` with
 * no floor, so it never had anything to scroll — it just squeezed. Six columns
 * sharing 700px does not fail loudly, it fails as an address column reading
 * `127.0.0…` and a size column wrapped over three lines, which looks like a
 * data problem rather than a width one.
 *
 * With a floor, the table keeps its designed proportions and the card scrolls
 * sideways instead. Sideways scroll is a real cost, so the floor is set at the
 * width the columns actually need and not a round number above it.
 */
export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface-raised">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[52rem] text-sm">{children}</table>
      </div>
    </div>
  )
}

export function THead({ children }: { children: ReactNode }) {
  return (
    <thead className="border-b border-border bg-surface text-left">
      <tr>{children}</tr>
    </thead>
  )
}

export function TH({
  children,
  align = 'left',
  className = '',
}: {
  children?: ReactNode
  align?: 'left' | 'right'
  className?: string
}) {
  return (
    <th
      scope="col"
      className={[
        'px-4 py-2 text-2xs font-medium uppercase tracking-wide text-text-subtle',
        align === 'right' ? 'text-right' : 'text-left',
        className,
      ].join(' ')}
    >
      {children}
    </th>
  )
}

export function TBody({ children }: { children: ReactNode }) {
  return <tbody className="divide-y divide-border">{children}</tbody>
}

export function TR({
  children,
  onClick,
  className = '',
}: {
  children: ReactNode
  onClick?: () => void
  className?: string
}) {
  return (
    <tr
      onClick={onClick}
      className={[
        'transition-colors',
        onClick ? 'cursor-pointer hover:bg-surface-overlay' : 'hover:bg-surface-overlay/60',
        className,
      ].join(' ')}
    >
      {children}
    </tr>
  )
}

export function TD({
  children,
  align = 'left',
  /** Identifiers, ports, paths, sizes — anything compared character by
   *  character. Sets mono and tabular figures. */
  data = false,
  className = '',
  colSpan,
  title,
}: {
  children?: ReactNode
  align?: 'left' | 'right'
  data?: boolean
  className?: string
  colSpan?: number
  /** Hover explanation for a value the cell had to abbreviate. */
  title?: string
}) {
  return (
    <td
      colSpan={colSpan}
      title={title}
      className={[
        'px-4 py-2.5 align-middle',
        align === 'right' ? 'text-right' : '',
        data ? 'data text-xs text-text-muted' : '',
        className,
      ].join(' ')}
    >
      {children}
    </td>
  )
}

/** The trailing column. Right-aligned, controls only. */
export function TActions({ children }: { children: ReactNode }) {
  return (
    <td className="px-4 py-2.5">
      <div className="flex items-center justify-end gap-0.5">{children}</div>
    </td>
  )
}

/**
 * Loading rows.
 *
 * A skeleton rather than a spinner because the shape of what is coming is
 * already known: showing it means the layout does not jump when the data
 * lands, and the eye has somewhere to rest in the meantime.
 */
export function TableSkeleton({ columns, rows = 4 }: { columns: number; rows?: number }) {
  return (
    <TBody>
      {Array.from({ length: rows }, (_, row) => (
        <tr key={row}>
          {Array.from({ length: columns }, (_, column) => (
            <td key={column} className="px-4 py-3">
              <div
                className="h-3 animate-pulse rounded-sm bg-surface-overlay"
                style={{ width: `${column === 0 ? 55 : 30 + ((column * 13) % 35)}%` }}
              />
            </td>
          ))}
        </tr>
      ))}
    </TBody>
  )
}
