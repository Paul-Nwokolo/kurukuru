import type { ReactNode } from 'react'

/**
 * The line above every table: what this page is, and the one action that
 * creates something.
 *
 * Standardising it is what lets the creation pattern be consistent. Volumes and
 * Projects used inline forms sitting permanently above their tables — three
 * inputs and a button of vertical space, held forever, for something done once
 * a week. Those became modals like Images, Key pairs and Instances already
 * were, and this component is where the trigger lives.
 */
export function ViewHeader({
  description,
  action,
}: {
  description?: ReactNode
  action?: ReactNode
}) {
  if (!description && !action) return null
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
      {description ? (
        <p className="max-w-3xl text-sm leading-relaxed text-text-muted">{description}</p>
      ) : (
        <span />
      )}
      {action}
    </div>
  )
}
