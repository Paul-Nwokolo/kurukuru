import { useProjects } from '../hooks/queries'
import { useSelectedProject } from '../lib/project'

/**
 * Header control scoping the dashboard to one project.
 *
 * "All projects" is the default and the first option, because a project is not
 * a boundary: seeing everything is the honest resting state, and narrowing is
 * the deliberate act. The title says so, since a selector that looks like a
 * tenant switcher is exactly the misreading to avoid.
 */
export function ProjectSelector() {
  const { data: projects } = useProjects()
  const [selected, setSelected] = useSelectedProject()

  // A project deleted in another tab (or by the CLI) leaves a stale id in
  // localStorage. Falling back to "all" beats filtering everything away and
  // showing an empty dashboard with no explanation.
  const known = projects?.some((p) => p.id === selected) ?? true
  const value = known && selected ? selected : ''

  return (
    <label
      className="flex items-center gap-2 text-xs"
      title="Scopes what this dashboard shows. Projects are organisational — they are not an access boundary."
    >
      <span className="text-text-subtle">Project</span>
      <select
        value={value}
        onChange={(e) => setSelected(e.target.value || null)}
        className="h-[var(--control-height)] rounded border border-border bg-surface-raised px-2 text-xs text-text transition-colors hover:border-border-strong focus:border-accent focus:outline-none"
      >
        <option value="">All projects</option>
        {(projects ?? []).map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
            {project.is_default ? ' (default)' : ''}
          </option>
        ))}
      </select>
    </label>
  )
}
