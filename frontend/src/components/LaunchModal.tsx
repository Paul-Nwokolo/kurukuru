import { useEffect, useMemo, useState } from 'react'
// Named import: js-yaml's ESM build has no default export, though its types
// declare one for CommonJS interop — so `import yaml from` typechecks and then
// fails at runtime with a blank page.
import { load as parseYaml } from 'js-yaml'
import { ChevronDown, Disc, HardDrive, Monitor, Rocket, Terminal } from 'lucide-react'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Alert } from '../ui/Feedback'
import {
  apiErrorMessage,
  type AccelChoice,
  type DisplayChoice,
  type GuestOS,
  type HostCapacity,
  type Instance,
} from '../api/client'
import {
  useFlavors,
  useHostCapacity,
  useImages,
  useProjects,
  useIsos,
  useKeyPairs,
  useLaunchInstance,
  useSettingValue,
} from '../hooks/queries'
import { useSelectedProject } from '../lib/project'
import { formatBytes, formatMemory } from '../lib/format'

const NAME_PATTERN = /^[a-z][a-z0-9-]{1,30}$/

/** Ties the pinned footer's submit button back to the form it lives outside. */
const LAUNCH_FORM_ID = 'launch-instance-form'

/** Sentinel for "no explicit image" — the server then resolves the built-in
 *  one. A real id would be wrong: the built-in row's id differs per install. */
const BUILTIN_IMAGE_CHOICE = '__builtin__'

/**
 * The launch flow is organised by *what the user wants*, not by which QEMU
 * flags it sets. Each mode maps onto the same code paths underneath — boot
 * source, cloud-init seeding and access method all follow from the choice
 * rather than being asked as separate mechanism-shaped questions.
 */
type LaunchMode = 'iso' | 'image'

interface ModeSpec {
  key: LaunchMode
  title: string
  subtitle: string
  icon: typeof Rocket
}

/*
 * Two ways to get a disk, not three.
 *
 * "Quick launch" was presented as a peer of the other two, which overstated
 * it: the backend has exactly one branch — `iso` set, or not — and quick
 * launch simply sends neither an ISO nor an image id, so the server resolves
 * the built-in Ubuntu image. It is Image mode with the default image
 * preselected, and it now says so.
 *
 * The distinction that actually matters, and that the old wording buried:
 * a **disk image boots ready** (cloud-init injects your key, SSH works in
 * ~30s), an **ISO installs** (you sit through the installer on the console,
 * and there is no SSH key because there is no cloud-init to inject one).
 */
const MODES: ModeSpec[] = [
  {
    key: 'image',
    title: 'Disk image',
    subtitle: 'Boots ready to use. Your key is injected; SSH in ~30 seconds.',
    icon: HardDrive,
  },
  {
    key: 'iso',
    title: 'Installer ISO',
    // Deliberately no "slower to run": ISO instances take the same accelerated
    // path as everything else now that the forced-software-emulation rule is
    // gone. Keeping the old warning would talk users out of a feature that no
    // longer costs anything.
    subtitle: 'You install the OS yourself through the console. No SSH key.',
    icon: Disc,
  },
]

/**
 * The guest families, and the one line each that changes what a user should
 * expect. Windows is not "Linux but Microsoft": it installs from media the user
 * supplies, it has no cloud-init, and its access story is the console until RDP
 * is switched on inside the guest.
 */
const GUEST_OS_CHOICES: { key: GuestOS; label: string; hint: string }[] = [
  {
    key: 'linux',
    label: 'Linux',
    hint: 'Cloud image or ISO. SSH key injected on cloud images.',
  },
  {
    key: 'windows',
    label: 'Windows',
    // Says the important thing first. The device selection is correct and the
    // installer boots, but Setup does not finish yet — and a picker that reads
    // as "supported" would be the product promising something it cannot do.
    hint: 'Setup does not finish yet — known issue. Your own ISO.',
  },
]

interface LaunchModalProps {
  open: boolean
  onClose: () => void
  onOpenConsole: (instance: Instance) => void
}

export function LaunchModal({ open, onClose, onOpenConsole }: LaunchModalProps) {
  const { data: presets } = useFlavors()
  const { data: isos, isLoading: isosLoading } = useIsos(open)
  const { data: images } = useImages()
  const { data: capacity } = useHostCapacity()
  const { data: keypairs } = useKeyPairs()
  const { data: projects } = useProjects()
  // Named in copy below, so read from the backend rather than assumed: both
  // move with an environment variable, and telling someone to drop an ISO in a
  // directory this install does not use is a bug that looks like a typo.
  const isoDir = useSettingValue('iso_dir')
  const guestUser = useSettingValue('default_vm_user')
  const launch = useLaunchInstance()
  // Defaults to whatever the header is scoped to, so launching while looking
  // at one project files it there. Overridable, because the header selector is
  // a *view* and this is a decision about the instance.
  const [scoped] = useSelectedProject()

  // Chosen first, and deliberately so: it inverts the correct answer for the
  // disk bus, the NIC, the graphics adapter and the CPU model all at once, so
  // every control below it depends on this one.
  const [guestOs, setGuestOs] = useState<GuestOS>('linux')
  const [mode, setMode] = useState<LaunchMode>('image')
  const [name, setName] = useState('')
  const [preset, setPreset] = useState('small')
  const [custom, setCustom] = useState(false)
  const [cpus, setCpus] = useState(1)
  const [memoryMb, setMemoryMb] = useState(1024)
  const [diskGb, setDiskGb] = useState(5)
  const [iso, setIso] = useState<string | null>(null)
  const [imageId, setImageId] = useState<string | null>(null)
  const [accel, setAccel] = useState<AccelChoice>('auto')
  // Defaulted per source rather than globally — see the Graphics block for the
  // measurements. Tracked so an explicit choice is never overwritten.
  const [display, setDisplay] = useState<DisplayChoice>('virtio')
  const [displayTouched, setDisplayTouched] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [touched, setTouched] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  // Set once a launch is accepted; the modal then explains how to get in.
  const [launched, setLaunched] = useState<Instance | null>(null)
  // null until the keypair list resolves, then defaults to the orchestrator key
  // — the same key every instance got before this selector existed.
  const [selectedKeys, setSelectedKeys] = useState<string[] | null>(null)
  const [userData, setUserData] = useState('')
  // null means "follow the header"; a string is a deliberate override.
  const [projectOverride, setProjectOverride] = useState<string | null>(null)
  const projectId = projectOverride ?? scoped

  const availableImages = useMemo(
    () => (images ?? []).filter((i) => i.status === 'Available'),
    [images],
  )
  const selectedImage = availableImages.find((i) => i.id === imageId) ?? null

  useEffect(() => {
    if (!open) return
    setGuestOs('linux')
    setMode('image')
    setName('')
    setPreset('small')
    setCustom(false)
    setIso(null)
    setImageId(null)
    setAccel('auto')
    setDisplay('virtio')
    setDisplayTouched(false)
    setAdvancedOpen(false)
    setTouched(false)
    setFormError(null)
    setLaunched(null)
    setSelectedKeys(null)
    setUserData('')
  }, [open])

  // Default the selection to the orchestrator key once the catalog arrives.
  // Left alone afterwards, so reopening the modal doesn't undo a user's choice
  // mid-session and a slow catalog can't clobber an early selection.
  useEffect(() => {
    if (!open || selectedKeys !== null || !keypairs) return
    const orchestrator = keypairs.find((k) => k.source === 'orchestrator')
    setSelectedKeys(orchestrator ? [orchestrator.id] : [])
  }, [open, keypairs, selectedKeys])

  // Follow the source's correct default until the user overrides it. An ISO
  // installer has no virtio-gpu driver loaded and would show nothing; a cloud
  // image sits in text mode and shows nothing *without* virtio.
  //
  // Windows is not a default here but a constraint: Setup has no virtio-gpu
  // driver at all, so "modern" is never right and the override is ignored.
  useEffect(() => {
    if (guestOs === 'windows') {
      setDisplay('std')
      return
    }
    if (!displayTouched) setDisplay(mode === 'iso' ? 'std' : 'virtio')
  }, [mode, displayTouched, guestOs])

  // Windows installs from its own medium — there is no Windows cloud image to
  // overlay — and it needs more room than any Linux preset offers. Switching
  // guest OS therefore moves the source and the size with it, rather than
  // leaving a form that can only be submitted to a 422.
  useEffect(() => {
    if (guestOs !== 'windows') return
    setMode('iso')
    setImageId(null)
    if (!custom) setPreset('windows')
  }, [guestOs, custom])

  // A preset fills the numbers in; they stay editable, which is the whole point
  // of presets being starting points rather than the only allowed shapes.
  useEffect(() => {
    const spec = presets?.find((p) => p.name === preset)
    if (spec && !custom) {
      setCpus(spec.cpus)
      setMemoryMb(spec.memory_mb)
      setDiskGb(spec.disk_gb)
    }
  }, [preset, presets, custom])

  if (!open) return null

  // Client-side only so the user sees a mistake before submitting; the backend
  // re-parses and is the authority (its 422 carries the line and column).
  const yamlError = ((): string | null => {
    if (!userData.trim()) return null
    try {
      const parsed = parseYaml(userData)
      if (parsed === null || parsed === undefined) return 'This parses to nothing.'
      if (typeof parsed !== 'object' || Array.isArray(parsed)) {
        return 'cloud-config must be a mapping of keys, e.g. "packages:".'
      }
      return null
    } catch (err) {
      // js-yaml's message is multi-line with a snippet; the first line is the
      // human-readable half.
      return err instanceof Error ? err.message.split('\n')[0] : 'Invalid YAML'
    }
  })()

  const nameValid = NAME_PATTERN.test(name)
  const showNameError = touched && name.length > 0 && !nameValid
  const sizeError = capacitySizeError(capacity, { cpus, memoryMb, diskGb })
  // In image mode a null selection is meaningful: the server resolves the
  // built-in Ubuntu image, which is precisely what the old "quick launch"
  // did. Only an ISO launch genuinely requires a choice.
  const modeReady = mode === 'iso' ? !!iso : true
  const canSubmit = nameValid && modeReady && !sizeError && !yamlError && !launch.isPending

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    setFormError(null)
    if (!nameValid) return
    if (!modeReady) {
      setFormError(mode === 'iso' ? 'Choose an ISO to boot from.' : 'Choose an image.')
      return
    }
    try {
      const created = await launch.mutateAsync({
        name,
        ...(custom ? {} : { preset }),
        cpus,
        memory_mb: memoryMb,
        disk_gb: diskGb,
        accel,
        display,
        guest_os: guestOs,
        ...(mode === 'iso' ? { iso } : {}),
        ...(mode === 'image' ? { image_id: imageId } : {}),
        // Omitted while the catalog is still loading, which the backend reads
        // as "the orchestrator key" — the pre-keypair default, so a fast
        // submit can never produce an instance with no way in.
        // Never sent for Windows: there is no cloud-init to inject a key with,
        // so naming keys would imply an access path that does not exist.
        ...(selectedKeys === null || guestOs === 'windows'
          ? {}
          : { keypair_ids: selectedKeys }),
        ...(mode !== 'iso' && guestOs !== 'windows' && userData.trim()
          ? { user_data: userData }
          : {}),
        // Omitted for "All projects", which the backend reads as the default
        // project — the same place every instance went before projects existed.
        ...(projectId ? { project_id: projectId } : {}),
      })
      setLaunched(created)
    } catch (err) {
      // The API's own 409 names the project holding the name and says that
      // instance names are unique across all of them — more useful than
      // anything this component could compose, so it is passed through.
      setFormError(apiErrorMessage(err))
    }
  }

  // --- Success step: the moment the access method matters most ------------ //
  if (launched) {
    // An image without cloud-init gets no key injected, exactly like an ISO.
    const sshCapable =
      mode === 'image' && (imageId === null || selectedImage?.has_cloud_init === true)
    return (
      <Shell
        title={`${launched.name} is starting`}
        icon={Rocket}
        onClose={onClose}
        footer={
          <Button intent="primary" onClick={onClose}>
            Done
          </Button>
        }
      >
        <div className="space-y-4 p-5">
          <p className="text-sm text-text-muted">
            Provisioning runs in the background — the row turns Running when it is ready.
          </p>

          {sshCapable ? (
            <div>
              <span className="mb-1.5 flex items-center gap-2 text-sm font-medium text-text-muted">
                <Terminal className="h-4 w-4 text-accent-text" /> Connect over SSH
              </span>
              <p className="text-xs text-text-subtle">
                Once it has an address, use the Copy SSH button on the instance row — the
                command it copies includes the orchestrator key.
              </p>
            </div>
          ) : (
            <div>
              <span className="mb-1.5 flex items-center gap-2 text-sm font-medium text-text-muted">
                <Monitor className="h-4 w-4 text-accent-text" /> Use the console
              </span>
              <p className="mb-2.5 text-xs text-text-subtle">
                No SSH key is injected for this kind of instance — the browser console is
                how you log in and finish setup.
              </p>
              <button
                type="button"
                onClick={() => {
                  onOpenConsole(launched)
                  onClose()
                }}
                className="inline-flex items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-fg hover:bg-accent-hover"
              >
                <Monitor className="h-4 w-4" />
                Open console
              </button>
            </div>
          )}
        </div>
        {/* One Done, in the Shell's pinned footer above. There used to be a
            second hand-rolled one right here, so the success step ended with
            two identical buttons stacked — a leftover from before the modal
            grew a footer prop. */}
      </Shell>
    )
  }

  return (
    <Shell
      title="Launch Instance"
      icon={Rocket}
      onClose={onClose}
      onSubmit={submit}
      footer={
        <>
          {/*
            What is about to be created, at the moment of committing to it.

            This was the quietest text in the design system sitting next to the
            most consequential button. It now carries the accent — the same one
            as Launch — so the specification and the action that commits to it
            read as one object rather than as a caption beside a button.
          */}
          <div className="mr-auto flex min-w-0 items-center gap-2 rounded border border-accent/30 bg-accent-quiet px-2.5 py-1.5">
            <span className="truncate text-sm font-semibold text-accent-text">
              {name || 'unnamed'}
            </span>
            <span className="data shrink-0 text-xs text-text-muted">
              {cpus} vCPU · {formatMemory(memoryMb)} · {diskGb} GB
            </span>
          </div>
          <Button onClick={onClose} disabled={launch.isPending}>
            Cancel
          </Button>
          <Button
            intent="primary"
            type="submit"
            form={LAUNCH_FORM_ID}
            disabled={!canSubmit}
            loading={launch.isPending}
          >
            Launch
          </Button>
        </>
      }
    >
      <div className="max-h-[65vh] space-y-5 overflow-y-auto p-5">
        {/* Step 1 — guest OS. First because it inverts the correct answer for
            almost everything below: disk bus, NIC, graphics and CPU model. */}
        <div>
          <span className="mb-1.5 block text-sm font-medium text-text-muted">
            Which operating system?
          </span>
          <div className="flex gap-2">
            {GUEST_OS_CHOICES.map((choice) => (
              <GuestOsCard
                key={choice.key}
                label={choice.label}
                hint={choice.hint}
                selected={guestOs === choice.key}
                onSelect={() => setGuestOs(choice.key)}
              />
            ))}
          </div>
          {guestOs === 'windows' && (
            <p className="mt-2 text-xs text-text-subtle">
              Windows installs from an ISO you supply — Microsoft's images cannot be
              redistributed. Get one from{' '}
              <span className="text-text">Microsoft's official download pages</span>, put
              it in the ISO directory, and pick it below. Expect the install to take
              considerably longer than a Linux cloud image, and to run it from the
              console.{' '}
              <span className="text-text">
                Windows Server and Windows 10 are supported; Windows 11 is not
              </span>{' '}
              — it requires a TPM, which QEMU cannot emulate on a Windows host.
            </p>
          )}
        </div>

        {/* Step 2 — intent. Windows has exactly one source, so the choice is
            not offered rather than offered-and-refused: presenting an option
            that can only produce a 422 is the thing the audit ruled out. */}
        {guestOs === 'windows' ? (
          <div>
            <span className="mb-1.5 block text-sm font-medium text-text-muted">
              Source
            </span>
            <div className="rounded-lg border border-border-strong bg-surface p-3">
              <span className="block text-sm font-semibold text-text">Installer ISO</span>
              <span className="block text-xs text-text-muted">
                The only source for Windows — there is no Windows cloud image to start
                from.
              </span>
            </div>
          </div>
        ) : (
          <div>
            <span className="mb-1.5 block text-sm font-medium text-text-muted">
              What do you want to do?
            </span>
            <div className="space-y-2">
              {MODES.map((m) => (
                <ModeCard
                  key={m.key}
                  spec={m}
                  selected={mode === m.key}
                  onSelect={() => setMode(m.key)}
                  subtitle={
                    m.key === 'image' && selectedImage
                      ? selectedImage.has_cloud_init
                        ? 'Ready to SSH — this image has cloud-init.'
                        : 'No SSH key injection; use the console to log in.'
                      : m.subtitle
                  }
                />
              ))}
            </div>
          </div>
        )}

        <div>
          <label
            htmlFor="instance-name"
            className="mb-1.5 block text-sm font-medium text-text-muted"
          >
            Name
          </label>
          <input
            id="instance-name"
            type="text"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => setTouched(true)}
            placeholder="my-instance"
            spellCheck={false}
            className={[
              'w-full rounded-md border bg-surface px-3 py-2 text-sm text-text placeholder:text-text-subtle outline-none transition-colors',
              showNameError
                ? 'border-danger/70 focus:border-danger'
                : 'border-border-strong focus:border-accent',
            ].join(' ')}
          />
          {showNameError && (
            <p className="mt-1.5 text-xs text-danger">
              Must start with a letter; lowercase letters, digits and hyphens only (2–31
              chars).
            </p>
          )}
        </div>

        {/* This used to say "Linux installers only, for now" and explain that a
            Windows ISO would boot to "no drives found". That is no longer true —
            picking Windows above now selects a SATA disk and an e1000e NIC that
            Setup has drivers for — and it was still being shown inside the
            Windows flow, telling the user the thing they had just chosen does
            not work. The remaining caveat is real and specific, so it is stated
            for the guest it applies to rather than as a blanket warning. */}
        {mode === 'iso' && guestOs === 'windows' && (
          <Alert tone="danger">
            <span className="font-semibold">
              Windows Setup does not currently finish.
            </span>{' '}
            The instance launches and boots the installer on hardware Windows has drivers
            for, then stops partway through Setup — this is a known, open problem, not
            something you have configured wrongly. Launch one if you want to help
            diagnose it; do not expect a usable VM. Windows 11 additionally requires a
            TPM, which QEMU cannot emulate on a Windows host at any version.
          </Alert>
        )}

        {mode === 'iso' && (
          <Picker
            label="ISO"
            loading={isosLoading}
            empty={
              <>
                No ISOs found. Drop <code>.iso</code> files into the backend's ISO
                directory{isoDir && <> (<code>{isoDir}</code>)</>} and reopen this
                dialog.
              </>
            }
            items={(isos ?? []).map((f) => ({
              id: f.name,
              label: f.name,
              hint: formatBytes(f.size_bytes),
            }))}
            selected={iso}
            onSelect={setIso}
          />
        )}
        {/* The built-in image is a first-class entry rather than an empty
            selection, because "launch with the default" is the commonest thing
            anyone does here — it is what "quick launch" used to be, and it
            should look like a choice, not like having chosen nothing. */}
        {mode === 'image' && (
          <Picker
            label="Image"
            loading={false}
            empty={<>No imported images yet. Add one from the Images page.</>}
            items={[
              {
                id: BUILTIN_IMAGE_CHOICE,
                label: 'Ubuntu 24.04 LTS (built-in)',
                hint: 'downloaded once, on first launch',
              },
              ...availableImages
                .filter((img) => img.source !== 'builtin')
                .map((img) => ({
                  id: img.id,
                  label: img.name,
                  hint: img.virtual_size_bytes ? formatBytes(img.virtual_size_bytes) : '',
                  warn: img.has_cloud_init ? undefined : 'no cloud-init',
                })),
            ]}
            selected={imageId ?? BUILTIN_IMAGE_CHOICE}
            onSelect={(id) => setImageId(id === BUILTIN_IMAGE_CHOICE ? null : id)}
          />
        )}

        {/* Resources */}
        <div>
          <div className="mb-1.5 flex items-center justify-between gap-3">
            <span className="text-sm font-medium text-text-muted">Size</span>
            {capacity && <CapacityStrip capacity={capacity} />}
          </div>
          <div className="grid grid-cols-4 gap-2">
            {(presets ?? []).map((spec) => {
              const tooBig = capacity ? presetExceeds(capacity, spec) : false
              return (
                <button
                  key={spec.name}
                  type="button"
                  disabled={tooBig}
                  aria-pressed={!custom && preset === spec.name}
                  title={
                    tooBig
                      ? `${spec.name} needs more than this host can currently allocate`
                      : `${spec.cpus} vCPU · ${formatMemory(spec.memory_mb)} RAM · ${spec.disk_gb} GB disk`
                  }
                  onClick={() => {
                    setCustom(false)
                    setPreset(spec.name)
                  }}
                  className={[
                    'rounded-lg border px-2 py-2 text-center text-xs capitalize transition-colors disabled:cursor-not-allowed disabled:opacity-40',
                    !custom && preset === spec.name
                      ? 'border-accent bg-accent-quiet text-accent-text'
                      : 'border-border-strong bg-surface text-text-muted hover:border-border-strong',
                  ].join(' ')}
                >
                  <span className="block font-semibold">{spec.name}</span>
                  <span className="text-2xs text-text-subtle">
                    {spec.cpus} · {formatMemory(spec.memory_mb)}
                  </span>
                </button>
              )
            })}
            <button
              type="button"
              aria-pressed={custom}
              onClick={() => setCustom(true)}
              className={[
                'rounded-lg border px-2 py-2 text-center text-xs transition-colors',
                custom
                  ? 'border-accent bg-accent-quiet text-accent-text'
                  : 'border-border-strong bg-surface text-text-muted hover:border-border-strong',
              ].join(' ')}
            >
              <span className="block font-semibold">Custom</span>
              <span className="text-2xs text-text-subtle">set your own</span>
            </button>
          </div>

          {custom && (
            <div className="mt-3 grid grid-cols-3 gap-3">
              <NumberField
                label="vCPUs"
                value={cpus}
                onChange={setCpus}
                min={1}
                max={capacity && !capacity.degraded ? capacity.cpu.max_per_instance : undefined}
              />
              <NumberField
                label="Memory (MB)"
                value={memoryMb}
                onChange={setMemoryMb}
                min={512}
                step={512}
                max={capacity && !capacity.degraded ? capacity.memory_mb.allocatable : undefined}
              />
              <NumberField
                label="Disk (GB)"
                value={diskGb}
                onChange={setDiskGb}
                min={1}
                max={capacity && !capacity.degraded ? capacity.disk_gb.allocatable : undefined}
              />
            </div>
          )}

          {sizeError && <p className="mt-2 text-xs text-danger">{sizeError}</p>}
        </div>

        {/* Project. A filing decision, not an access one — the note says so,
            because a field next to "Key pairs" in a launch dialog is exactly
            where someone would assume otherwise. */}
        <div>
          <label className="mb-1.5 block text-sm font-medium text-text-muted" htmlFor="project">
            Project
          </label>
          <select
            id="project"
            value={projectId ?? ''}
            onChange={(e) => setProjectOverride(e.target.value || null)}
            className="w-full rounded-md border border-border-strong bg-surface px-3 py-2 text-sm text-text focus:border-border-strong focus:outline-none"
          >
            <option value="">Default project</option>
            {(projects ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
                {project.is_default ? ' (default)' : ''}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-text-subtle">
            Groups this instance in the dashboard. It does not affect the name, which is
            unique across every project, nor who can reach the guest.
          </p>
        </div>

        {/* Windows gets the explanation the key-pair block would have occupied.
            The brief's rule, and the right one: absent controls should say why
            they are absent, because a missing field reads as a bug and a
            missing field with a reason reads as a design. */}
        {guestOs === 'windows' && (
          <div>
            <span className="mb-1.5 block text-sm font-medium text-text-muted">Access</span>
            <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs text-text-subtle">
              No SSH key and no key pairs — Windows has no cloud-init to inject one
              with, and no SSH server by default.{' '}
              <span className="text-text">You log in through the console</span>, and once
              the OS is up you can turn on Remote Desktop inside it and add a forward for
              port 3389 from the instance page.
            </p>
          </div>
        )}

        {/* Key pairs. Hidden for ISO instances, which get no cloud-init and so
            cannot receive a key however many are selected — offering the choice
            there would promise access the guest will not have. */}
        {mode !== 'iso' && (
          <div>
            <div className="mb-1.5 flex items-baseline justify-between">
              <span className="text-sm font-medium text-text-muted">Key pairs</span>
              <span className="text-xs text-text-subtle">
                {selectedKeys?.length ?? 0} selected
              </span>
            </div>
            <p className="mb-2 text-xs text-text-subtle">
              Whoever holds the matching private key can SSH in as{' '}
              <code className="rounded bg-surface px-1 py-0.5 text-text-muted">
                {guestUser ?? '…'}
              </code>
              .
            </p>

            {!keypairs?.length ? (
              <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs text-text-subtle">
                No key pairs yet — add one under Key pairs.
              </p>
            ) : (
              <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border border-border p-1">
                {keypairs.map((kp) => {
                  const checked = selectedKeys?.includes(kp.id) ?? false
                  return (
                    <label
                      key={kp.id}
                      className="flex cursor-pointer items-center gap-2.5 rounded px-2 py-1.5 hover:bg-surface"
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) =>
                          setSelectedKeys((prev) => {
                            const base = prev ?? []
                            return e.target.checked
                              ? [...base, kp.id]
                              : base.filter((id) => id !== kp.id)
                          })
                        }
                        className="h-3.5 w-3.5 rounded border-border-strong bg-surface accent-[var(--accent)]"
                      />
                      <span className="min-w-0 flex-1 truncate text-sm text-text">
                        {kp.name}
                        <span className="ml-2 text-xs text-text-subtle">{kp.key_type}</span>
                      </span>
                      <code
                        className="shrink-0 truncate data text-2xs text-text-subtle"
                        title={kp.fingerprint}
                      >
                        {kp.fingerprint.slice(0, 26)}…
                      </code>
                    </label>
                  )
                })}
              </div>
            )}

            {selectedKeys?.length === 0 && keypairs?.length ? (
              <p className="mt-1.5 text-xs text-transitional">
                With no keys selected this instance will be console-only — nothing will be
                able to SSH into it.
              </p>
            ) : null}
          </div>
        )}

        {/* Advanced — the accelerator is derived from the mode; this overrides it. */}
        <div className="rounded-lg border border-border">
          <button
            type="button"
            onClick={() => setAdvancedOpen((v) => !v)}
            aria-expanded={advancedOpen}
            className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium text-text-muted hover:text-text"
          >
            <span>Advanced</span>
            <ChevronDown
              className={`h-4 w-4 text-text-subtle transition-transform ${advancedOpen ? 'rotate-180' : ''}`}
            />
          </button>

          {advancedOpen && (
            <div className="space-y-4 border-t border-border p-3">
              {/* user-data. Hidden for ISO instances: an installer has no
                  cloud-init to read it, and the backend refuses it outright. */}
              {mode !== 'iso' && (
                <div>
                  <div className="mb-1.5 flex items-baseline justify-between">
                    <span className="text-xs font-medium text-text-muted">user-data</span>
                    {userData.trim() && (
                      <span
                        className={`text-xs ${yamlError ? 'text-danger' : 'text-accent-text'}`}
                      >
                        {yamlError ? 'invalid YAML' : 'valid YAML'}
                      </span>
                    )}
                  </div>
                  <textarea
                    value={userData}
                    onChange={(e) => setUserData(e.target.value)}
                    rows={6}
                    spellCheck={false}
                    placeholder={'packages:\n  - git\nruncmd:\n  - [systemctl, enable, nginx]'}
                    className="w-full resize-y rounded-md border border-border-strong bg-surface px-3 py-2 data text-xs text-text placeholder:text-text-subtle focus:border-accent focus:outline-none"
                  />
                  {yamlError ? (
                    <p className="mt-1.5 data text-xs text-danger">{yamlError}</p>
                  ) : (
                    <p className="mt-1.5 text-xs text-text-subtle">
                      Merged with the generated config rather than replacing it — the
                      default user, your selected keys and package refresh stay. Your
                      values win on conflict; lists are added to.{' '}
                      <a
                        href="https://cloudinit.readthedocs.io/en/latest/reference/examples.html"
                        target="_blank"
                        rel="noreferrer"
                        className="text-text-muted underline hover:text-text"
                      >
                        cloud-init examples
                      </a>
                    </p>
                  )}
                </div>
              )}

              {/*
                Graphics, and the correct answer inverts by source.

                A Linux cloud image sits in VGA text mode, which this
                hypervisor cannot render under hardware acceleration —
                measured: 720x400, one colour, black before you touch
                anything. So virtio is not an "advanced" option there, it is
                the only setting where the console works at all.

                An installer ISO is the opposite: it draws its own framebuffer
                and has no virtio-gpu driver loaded, so virtio would give it no
                picture whatsoever.
              */}
              <div>
                <span className="mb-1.5 block text-xs font-medium text-text-muted">
                  Graphics
                </span>
                {guestOs === 'windows' ? (
                  /* Not a choice for Windows. Setup carries no virtio-gpu
                     driver, so "modern" is not a trade-off there — it is a
                     blank screen for the whole install, with no way to tell it
                     from a hung VM. Offering an option that can only be wrong
                     is the case the audit ruled out. */
                  <div className="rounded-lg border border-border-strong bg-surface p-3">
                    <span className="block text-sm font-semibold text-text">
                      Standard (VGA)
                    </span>
                    <span className="block text-xs text-text-muted">
                      The only adapter Windows Setup can draw on — it has no virtio-gpu
                      driver, so anything else is a blank screen.
                    </span>
                  </div>
                ) : (
                <div className="grid grid-cols-2 gap-3">
                  <ChoiceCard
                    title="Standard (VGA)"
                    subtitle={
                      mode === 'iso'
                        ? 'Recommended for installers · needs no guest driver'
                        : 'Console shows black on a cloud image'
                    }
                    selected={display === 'std'}
                    onSelect={() => {
                      setDisplay('std')
                      setDisplayTouched(true)
                    }}
                  />
                  <ChoiceCard
                    title="Virtio GPU"
                    subtitle={
                      mode === 'iso'
                        ? 'An installer gets no picture at all'
                        : 'Recommended · the only setting the console renders on'
                    }
                    selected={display === 'virtio'}
                    onSelect={() => {
                      setDisplay('virtio')
                      setDisplayTouched(true)
                    }}
                  />
                </div>
                )}
                {guestOs !== 'windows' && (
                  <p className="mt-1.5 text-xs text-text-subtle">
                    {mode === 'iso'
                      ? 'An OS installer draws its own screen and has no virtio driver loaded yet, so Standard is the one that shows a picture.'
                      : 'A Linux cloud image stays in text mode, which this hypervisor cannot draw under hardware acceleration — Virtio GPU is what makes the console usable.'}
                  </p>
                )}
              </div>

              {/*
                Performance (hardware vs software emulation) was here and is
                deliberately gone.

                It did not vary by source type, hardware acceleration is
                correct essentially always, and its one real use — rendering a
                text-mode guest — is now what the Graphics default handles.
                Leaving it invited a ~30x slowdown as the fix for a problem
                Graphics already solves. Still settable through `accel` on the
                API for the case that genuinely needs it.
              */}
            </div>
          )}
        </div>

        {formError && (
          <div className="rounded-md border border-danger/40 bg-danger-quiet px-3 py-2 text-sm text-danger">
            {formError}
          </div>
        )}
      </div>

    </Shell>
  )
}

/* ------------------------------------------------------------------------- */
/* Limits mirrored from the backend — advisory only. The server re-checks every */
/* launch, so this exists to catch mistakes before a round trip, never as the   */
/* only guard. When capacity is degraded it enforces nothing, matching the      */
/* backend's choice to stay permissive rather than block on a broken probe.     */
/* ------------------------------------------------------------------------- */
function capacitySizeError(
  capacity: HostCapacity | undefined,
  want: { cpus: number; memoryMb: number; diskGb: number },
): string | null {
  if (!capacity || capacity.degraded) return null
  if (want.cpus > capacity.cpu.max_per_instance) {
    return `Only ${capacity.cpu.max_per_instance} vCPUs are allocatable right now.`
  }
  if (want.memoryMb > capacity.memory_mb.allocatable) {
    return `Only ${capacity.memory_mb.allocatable} MB of memory is allocatable right now.`
  }
  if (want.diskGb > capacity.disk_gb.allocatable) {
    return `Only ${capacity.disk_gb.allocatable} GB is free on the instance store.`
  }
  return null
}

function presetExceeds(
  capacity: HostCapacity,
  spec: { cpus: number; memory_mb: number; disk_gb: number },
): boolean {
  if (capacity.degraded) return false
  return (
    spec.cpus > capacity.cpu.max_per_instance ||
    spec.memory_mb > capacity.memory_mb.allocatable ||
    spec.disk_gb > capacity.disk_gb.allocatable
  )
}

/* ------------------------------------------------------------------------- */
/* Pieces                                                                      */
/* ------------------------------------------------------------------------- */
/**
 * The launch dialog's shell.
 *
 * **Sectioning, not steps.** A wizard was considered and rejected: most
 * launches are "type a name and go" — the defaults (small preset, built-in
 * image, orchestrator key) are the right answer almost always — and steps
 * would turn a one-field task into four screens. Instead the form keeps its
 * headings, and the footer is pinned by the shared Modal so the Launch button
 * is reachable no matter how far the Advanced section is opened. That was the
 * actual defect: the dialog could grow taller than the viewport and scroll its
 * own submit button out of reach.
 *
 * The footer also carries a live summary of what will be created, so the
 * decisions made further up are visible at the moment of committing to them.
 */
function Shell({
  title,
  icon,
  onClose,
  onSubmit,
  footer,
  children,
}: {
  title: string
  icon: typeof Rocket
  onClose: () => void
  onSubmit?: (e: React.FormEvent) => void
  footer?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Modal open onClose={onClose} title={title} icon={icon} size="lg" footer={footer}>
      {onSubmit ? (
        // The submit button lives in the pinned footer, outside this form, so
        // it is wired back with the `form` attribute.
        <form id={LAUNCH_FORM_ID} onSubmit={onSubmit}>
          {children}
        </form>
      ) : (
        children
      )}
    </Modal>
  )
}

/** The guest-OS picker's card. Side-by-side rather than stacked, because there
 *  are two and the choice is a fork rather than a list. */
function GuestOsCard({
  label,
  hint,
  selected,
  onSelect,
}: {
  label: string
  hint: string
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={[
        'flex-1 rounded-lg border p-3 text-left transition-colors',
        selected
          ? 'border-accent bg-accent-quiet ring-1 ring-accent/40'
          : 'border-border-strong bg-surface hover:border-border-strong',
      ].join(' ')}
    >
      <span className="block text-sm font-semibold text-text">{label}</span>
      <span className="block text-xs text-text-muted">{hint}</span>
    </button>
  )
}

function ModeCard({
  spec,
  subtitle,
  selected,
  onSelect,
}: {
  spec: ModeSpec
  subtitle: string
  selected: boolean
  onSelect: () => void
}) {
  const Icon = spec.icon
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={[
        'flex w-full items-start gap-3 rounded-lg border p-3 text-left transition-colors',
        selected
          ? 'border-accent bg-accent-quiet ring-1 ring-accent/40'
          : 'border-border-strong bg-surface hover:border-border-strong',
      ].join(' ')}
    >
      <Icon
        className={`mt-0.5 h-4 w-4 shrink-0 ${selected ? 'text-accent-text' : 'text-text-subtle'}`}
      />
      <span>
        <span className="block text-sm font-semibold text-text">{spec.title}</span>
        <span className="block text-xs text-text-muted">{subtitle}</span>
      </span>
    </button>
  )
}

function ChoiceCard({
  title,
  subtitle,
  selected,
  onSelect,
}: {
  title: string
  subtitle: string
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={[
        'flex flex-col gap-0.5 rounded-lg border p-3 text-left transition-colors',
        selected
          ? 'border-accent bg-accent-quiet ring-1 ring-accent/40'
          : 'border-border-strong bg-surface hover:border-border-strong',
      ].join(' ')}
    >
      <span className="text-sm font-semibold text-text">{title}</span>
      <span className="text-xs text-text-muted">{subtitle}</span>
    </button>
  )
}

function Picker({
  label,
  items,
  selected,
  onSelect,
  loading,
  empty,
}: {
  label: string
  items: { id: string; label: string; hint: string; warn?: string }[]
  selected: string | null
  onSelect: (id: string) => void
  loading: boolean
  empty: React.ReactNode
}) {
  return (
    <div>
      <span className="mb-1.5 block text-sm font-medium text-text-muted">{label}</span>
      <div className="rounded-lg border border-border bg-surface/60 p-2">
        {loading ? (
          <p className="p-1 text-xs text-text-subtle">Loading…</p>
        ) : items.length === 0 ? (
          <p className="p-1 text-xs text-text-subtle">{empty}</p>
        ) : (
          <div className="max-h-40 space-y-1 overflow-y-auto">
            {items.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => onSelect(item.id)}
                aria-pressed={selected === item.id}
                className={[
                  'flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs transition-colors',
                  selected === item.id
                    ? 'bg-healthy/15 text-accent-text'
                    : 'text-text-muted hover:bg-surface-overlay',
                ].join(' ')}
              >
                <span className="truncate">
                  {item.label}
                  {item.warn && (
                    <span className="ml-1.5 text-transitional/80">· {item.warn}</span>
                  )}
                </span>
                <span className="ml-3 shrink-0 text-text-subtle">{item.hint}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  min: number
  max?: number
  step?: number
}) {
  const over = max !== undefined && value > max
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-text-muted">{label}</span>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        className={[
          'w-full rounded-md border bg-surface px-2 py-1.5 text-sm text-text outline-none',
          over ? 'border-danger/70' : 'border-border-strong focus:border-accent',
        ].join(' ')}
      />
      {max !== undefined && (
        <span className="mt-1 block text-2xs text-text-subtle">max {max}</span>
      )}
    </label>
  )
}

/** Compact "what's left" readout, with a bar for committed vs available. */
function CapacityStrip({ capacity }: { capacity: HostCapacity }) {
  if (capacity.degraded) {
    return (
      <span className="text-xs text-transitional/80" title={capacity.warnings.join(' ')}>
        Host capacity unknown
      </span>
    )
  }
  const { memory_mb: mem, cpu } = capacity
  const usedPct = mem.total > 0 ? Math.min(100, (mem.committed / mem.total) * 100) : 0
  const freePct =
    mem.total > 0 ? Math.min(100 - usedPct, (mem.allocatable / mem.total) * 100) : 0
  return (
    <span className="flex shrink-0 items-center gap-2 text-xs text-text-subtle">
      <span>
        {Math.round(mem.total / 1024)} GB RAM · {Math.round(mem.allocatable / 1024)} GB free
        · {cpu.total} CPUs
      </span>
      {/*
        A meter, not a progress bar.

        `role="progressbar"` describes a task advancing towards completion, and
        nothing here is advancing — this is a measurement inside a known range,
        which is exactly what `role="meter"` is for. Calling it progress would
        tell a screen reader the host is 43% of the way through something.

        The bar is also not decorative, which is why it needs a role at all:
        the text beside it gives total and free, but *committed* appears
        nowhere else, and committed is not total minus free — the host's own
        usage is the difference. `aria-valuetext` carries the reading in the
        units a person thinks in, because "43" on its own says nothing about
        what was measured. The segments are hidden: they are how the value is
        drawn, not two more values.
      */}
      <span
        role="meter"
        aria-label="Host memory committed to instances"
        aria-valuemin={0}
        aria-valuemax={mem.total}
        aria-valuenow={mem.committed}
        // Rounded to GB the same way the visible line beside it is, not with
        // `formatMemory` — that only reaches GB on exact multiples of 1024,
        // which flavour sizes always are and host totals never are, so it
        // would have read "15828 MB" next to a label saying "15 GB RAM".
        aria-valuetext={
          `${Math.round(mem.committed / 1024)} GB of ${Math.round(mem.total / 1024)} GB ` +
          `committed, ${Math.round(mem.allocatable / 1024)} GB free`
        }
        className="flex h-1.5 w-14 overflow-hidden rounded-full bg-surface-overlay"
      >
        <span aria-hidden className="bg-transitional/70" style={{ width: `${usedPct}%` }} />
        <span aria-hidden className="bg-healthy/60" style={{ width: `${freePct}%` }} />
      </span>
    </span>
  )
}
