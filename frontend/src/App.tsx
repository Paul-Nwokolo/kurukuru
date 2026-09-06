import { useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { Sidebar } from './components/Sidebar'
import { Header } from './components/Header'
import { BackendBanner } from './components/BackendBanner'
import { InstancesTable } from './components/InstancesTable'
import { ImagesTable } from './components/ImagesTable'
import { KeyPairsTable } from './components/KeyPairsTable'
import { IsosTable } from './components/IsosTable'
import { SettingsView } from './components/SettingsView'
import { ActivityView } from './components/ActivityView'
import { ProjectsView } from './components/ProjectsView'
import { VolumesTable } from './components/VolumesTable'
import { NetworksView } from './components/NetworksView'
import { InstanceDetail } from './components/InstanceDetail'
import { VIEWS, instanceIdFromPath, usePath, viewFromPath } from './lib/router'
import { useIsDesktop } from './lib/breakpoint'
import { Button } from './ui/Button'
import { Checkbox } from './ui/Field'
import { LaunchModal } from './components/LaunchModal'
import { useInstances } from './hooks/queries'
import type { Instance } from './api/client'

export default function App() {
  const [includeTerminated, setIncludeTerminated] = useState(false)
  const [launchOpen, setLaunchOpen] = useState(false)
  // Lifted out of the table so the launch success step can open a console
  // for the instance it just created.
  const [consoleTarget, setConsoleTarget] = useState<Instance | null>(null)

  // The URL is the state. Which tab is open, and which instance's detail page,
  // are both read from the path rather than held beside it — so a reload,
  // Back, and a pasted link all land where they say they will.
  const path = usePath()
  const detailId = instanceIdFromPath(path)
  const view = viewFromPath(path)

  const { data: instances, isLoading } = useInstances(includeTerminated)

  /*
   * The navigation drawer, below `lg` only.
   *
   * Three things close it, and all three are the bug you get from omitting
   * them: navigating (otherwise you tap Volumes and the drawer stays parked
   * over the page you asked for), Escape (the same key that dismisses every
   * other overlay in the app), and crossing back up over the breakpoint —
   * without which, widening the window leaves `open` true, and narrowing it
   * again re-opens a drawer nobody asked for.
   */
  const isDesktop = useIsDesktop()
  const [navOpen, setNavOpen] = useState(false)

  useEffect(() => setNavOpen(false), [path])
  useEffect(() => {
    if (isDesktop) setNavOpen(false)
  }, [isDesktop])
  useEffect(() => {
    if (!navOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setNavOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  // `?console=<id>` opens straight into that instance's console. This is the
  // deep link `iaas console <name>` hands to the browser: the CLI knows the id
  // but has no VNC client of its own, so it delegates to the dashboard rather
  // than growing one. The parameter is consumed once — cleared from the URL as
  // soon as it is honoured — so closing the modal and reloading doesn't fight
  // the user by reopening it.
  const [pendingConsoleId, setPendingConsoleId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get('console'),
  )

  useEffect(() => {
    if (!pendingConsoleId || !instances) return
    const target = instances.find((i) => i.id === pendingConsoleId)
    if (target) setConsoleTarget(target)
    // Cleared whether or not it matched: an id that isn't in the list (already
    // terminated, or from another backend) will never match on a later render
    // either, and leaving it set would retry forever.
    setPendingConsoleId(null)
    const url = new URL(window.location.href)
    url.searchParams.delete('console')
    window.history.replaceState({}, '', url)
  }, [pendingConsoleId, instances])

  return (
    <div className="flex h-screen w-full overflow-hidden bg-surface text-text">
      <Sidebar
        active={detailId ? null : view}
        open={navOpen}
        onClose={() => setNavOpen(false)}
        isDesktop={isDesktop}
      />

      {/* Backdrop for the drawer. Rendered only when it can be seen, so there
          is never an invisible element sitting over a desktop layout. */}
      {!isDesktop && navOpen && (
        <div
          className="animate-backdrop fixed inset-0 z-30 bg-surface/80 backdrop-blur-sm"
          onMouseDown={() => setNavOpen(false)}
          aria-hidden
        />
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <Header
          title={detailId ? 'Instance' : VIEWS[view].title}
          onOpenNav={isDesktop ? undefined : () => setNavOpen(true)}
        />

        <BackendBanner />

        {/* Keyed on the view so a tab change replays the entrance — it marks
            "this is different content" without delaying it. */}
        <main key={detailId ?? view} className="animate-view flex-1 overflow-y-auto px-6 py-6">
          {detailId ? (
            <InstanceDetail instanceId={detailId} />
          ) : view === 'instances' ? (
            <>
              {/* View toolbar */}
              <div className="mb-4 flex items-center justify-between gap-4">
                <Checkbox
                  checked={includeTerminated}
                  onChange={(e) => setIncludeTerminated(e.target.checked)}
                  label="Show terminated"
                />

                <Button intent="primary" icon={Plus} onClick={() => setLaunchOpen(true)}>
                  Launch Instance
                </Button>
              </div>

              <InstancesTable
                instances={instances ?? []}
                isLoading={isLoading}
                onLaunch={() => setLaunchOpen(true)}
                consoleTarget={consoleTarget}
                onConsoleTargetChange={setConsoleTarget}
              />
            </>
          ) : view === 'images' ? (
            <ImagesTable />
          ) : view === 'isos' ? (
            <IsosTable />
          ) : view === 'volumes' ? (
            <VolumesTable />
          ) : view === 'networks' ? (
            <NetworksView />
          ) : view === 'keypairs' ? (
            <KeyPairsTable />
          ) : view === 'projects' ? (
            <ProjectsView />
          ) : view === 'activity' ? (
            <ActivityView />
          ) : (
            <SettingsView />
          )}
        </main>
      </div>

      <LaunchModal
        open={launchOpen}
        onClose={() => setLaunchOpen(false)}
        onOpenConsole={setConsoleTarget}
      />
    </div>
  )
}
