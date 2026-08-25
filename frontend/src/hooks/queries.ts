/**
 * React Query hooks — the only place components touch the query cache.
 *
 * Polling cadence per the brief:
 *   - instances: every 3s (VMs change state server-side)
 *   - health:    every 15s
 * Every mutation invalidates the instances list so the table refetches
 * immediately after start/stop/terminate/launch.
 */
import {
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useSelectedProject } from '../lib/project'
import {
  createInstance,
  createSnapshot,
  deleteImage,
  deleteKeyPair,
  deleteSnapshot,
  generateKeyPair,
  getEngines,
  getImages,
  getFlavors,
  getHealth,
  getHostCapacity,
  getDiagnostics,
  addForward,
  attachVolume,
  cloneInstance,
  createProject,
  createVolume,
  deleteProject,
  deleteVolume,
  detachVolume,
  getEvents,
  getInstance,
  getInstanceEvents,
  getInstanceKeyPairs,
  getIsos,
  getKeyPairs,
  getSettings,
  getForwards,
  getNetworkModes,
  getNetworks,
  getProjects,
  createVolumeSnapshot,
  deleteVolumeSnapshot,
  getSnapshots,
  getVolumeSnapshots,
  restoreVolumeSnapshot,
  getVolumes,
  getSshKey,
  importImage,
  importKeyPair,
  listInstances,
  fetchImage,
  refreshInstances,
  removeForward,
  restartInstance,
  renameProject,
  restoreSnapshot,
  startInstance,
  stopInstance,
  terminateInstance,
  type CreateInstancePayload,
  type ImportImagePayload,
  getForwardPresets,
  type GuestOS,
} from '../api/client'

export const queryKeys = {
  health: ['health'] as const,
  flavors: ['flavors'] as const,
  engines: ['engines'] as const,
  isos: ['isos'] as const,
  images: ['images'] as const,
  capacity: ['host-capacity'] as const,
  sshKey: ['ssh-key'] as const,
  keypairs: ['keypairs'] as const,
  settings: ['settings'] as const,
  diagnostics: ['diagnostics'] as const,
  instances: (includeTerminated: boolean, projectId: string | null) =>
    ['instances', { includeTerminated, projectId }] as const,
  instance: (id: string) => ['instance', id] as const,
  instanceKeypairs: (id: string) => ['instance', id, 'keypairs'] as const,
  snapshots: (id: string) => ['instance', id, 'snapshots'] as const,
  events: ['events'] as const,
  projects: ['projects'] as const,
  volumes: ['volumes'] as const,
  volumeSnapshots: (id: string) => ['volume', id, 'snapshots'] as const,
  networks: ['networks'] as const,
  networkModes: ['network-modes'] as const,
  forwards: (id: string) => ['instance', id, 'forwards'] as const,
  instanceEvents: (id: string) => ['instance', id, 'events'] as const,
}

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: getHealth,
    refetchInterval: 15_000,
    retry: false,
    staleTime: 10_000,
  })
}

export function useFlavors() {
  return useQuery({
    queryKey: queryKeys.flavors,
    queryFn: getFlavors,
    staleTime: Infinity, // catalog is effectively static
  })
}

/**
 * Engine catalog. Refetched on a slow cadence rather than cached forever: a
 * driver's availability is live state (QEMU can be missing its base image, the
 * Multipass daemon can be down), unlike the static flavor catalog.
 */
export function useEngines() {
  return useQuery({
    queryKey: queryKeys.engines,
    queryFn: getEngines,
    staleTime: 60_000,
    retry: false,
  })
}

/**
 * The orchestrator keypair. Needed by every Copy SSH command (`ssh -i ...`),
 * so it is fetched once and held: the keypair is generated on first access and
 * then never changes.
 */
export function useSshKey() {
  return useQuery({
    queryKey: queryKeys.sshKey,
    queryFn: getSshKey,
    staleTime: Infinity,
  })
}

/**
 * Boot media. Refetched on open rather than cached forever — the user drops
 * files into the ISO directory outside the app, and a stale list would hide the
 * ISO they just added.
 */
export function useIsos(enabled = true) {
  return useQuery({
    queryKey: queryKeys.isos,
    queryFn: getIsos,
    enabled,
    staleTime: 5_000,
  })
}

/** Image catalog. Polled like instances because imports finish in the background. */
export function useImages() {
  const [projectId] = useSelectedProject()
  return useQuery({
    queryKey: [...queryKeys.images, projectId],
    queryFn: () => getImages(projectId),
    refetchInterval: 3_000,
  })
}

function useInvalidateImages() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: queryKeys.images })
}

export function useImportImage() {
  const invalidate = useInvalidateImages()
  return useMutation({
    mutationFn: (payload: ImportImagePayload) => importImage(payload),
    onSuccess: invalidate,
  })
}

export function useDeleteImage() {
  const invalidate = useInvalidateImages()
  return useMutation({
    mutationFn: (id: string) => deleteImage(id),
    onSuccess: invalidate,
  })
}

/**
 * Host capacity. Polled with the dashboard because it moves as instances start
 * and stop; the backend caches so this doesn't re-probe psutil each time.
 */
export function useHostCapacity() {
  return useQuery({
    queryKey: queryKeys.capacity,
    queryFn: getHostCapacity,
    refetchInterval: 5_000,
    retry: false,
  })
}

/**
 * Mutation key for the manual reconcile, so other hooks can tell one is in
 * flight without a second piece of global state to keep in step.
 */
export const RECONCILE_KEY = ['reconcile'] as const

/** Whether a manual reconcile is running right now. */
export function useIsReconciling(): boolean {
  return useIsMutating({ mutationKey: RECONCILE_KEY }) > 0
}

export function useInstances(includeTerminated: boolean) {
  // Scoped by the header selector. The project id is part of the query key, so
  // switching projects is a cache miss rather than a stale render of the
  // previous project's rows.
  const [projectId] = useSelectedProject()
  // The background poll is paused while a manual reconcile is in flight.
  // Without this the table can update mid-operation for an entirely unrelated
  // reason — a poll landing at the wrong moment — and the user attributes the
  // change to the button they just pressed. Pausing costs nothing: the
  // reconcile invalidates this query when it finishes, so the next render is
  // the reconciled state and it is attributable.
  const reconciling = useIsReconciling()
  return useQuery({
    queryKey: queryKeys.instances(includeTerminated, projectId),
    queryFn: () => listInstances(includeTerminated, projectId),
    refetchInterval: reconciling ? false : 3_000,
  })
}

/** Invalidate every instances list variant (terminated toggle both states). */
/**
 * Projects. Not polled: they change only when someone in this browser changes
 * them, and the counts they carry are refreshed by the mutations below.
 */
export function useProjects() {
  return useQuery({
    queryKey: queryKeys.projects,
    queryFn: getProjects,
    staleTime: 30_000,
  })
}

function useInvalidateProjects() {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: queryKeys.projects })
    // The counts on a project row come from what is filed under it, so any
    // resource list that changed makes them stale too.
    void qc.invalidateQueries({ queryKey: ['instances'] })
    void qc.invalidateQueries({ queryKey: queryKeys.images })
    void qc.invalidateQueries({ queryKey: queryKeys.keypairs })
  }
}

export function useCreateProject() {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: (payload: { name: string; description?: string | null }) =>
      createProject(payload),
    onSuccess: invalidate,
  })
}

export function useRenameProject() {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => renameProject(id, name),
    onSuccess: invalidate,
  })
}

export function useDeleteProject() {
  const invalidate = useInvalidateProjects()
  return useMutation({
    mutationFn: (id: string) => deleteProject(id),
    onSuccess: invalidate,
  })
}

/**
 * Volumes. Polled while any are Creating — allocation is backgrounded, so the
 * row's status is what settles.
 */
export function useVolumes(instanceId?: string) {
  const [projectId] = useSelectedProject()
  return useQuery({
    queryKey: [...queryKeys.volumes, projectId ?? null, instanceId ?? null],
    queryFn: () => getVolumes({ projectId, instanceId }),
    refetchInterval: (query) =>
      query.state.data?.some((v) => v.status === 'Creating') ? 2_000 : false,
  })
}

function useInvalidateVolumes() {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: queryKeys.volumes })
    // An attach or detach writes an event and changes what the instance boots
    // with, so the detail view's other panels are stale too.
    void qc.invalidateQueries({ queryKey: ['instance'] })
    void qc.invalidateQueries({ queryKey: queryKeys.events })
  }
}

export function useCreateVolume() {
  const invalidate = useInvalidateVolumes()
  return useMutation({
    mutationFn: (payload: { name: string; size_gb: number; project_id?: string | null }) =>
      createVolume(payload),
    onSuccess: invalidate,
  })
}

export function useAttachVolume() {
  const invalidate = useInvalidateVolumes()
  return useMutation({
    mutationFn: ({ id, instanceId }: { id: string; instanceId: string }) =>
      attachVolume(id, instanceId),
    onSuccess: invalidate,
  })
}

export function useDetachVolume() {
  const invalidate = useInvalidateVolumes()
  return useMutation({
    mutationFn: (id: string) => detachVolume(id),
    onSuccess: invalidate,
  })
}

export function useDeleteVolume() {
  const invalidate = useInvalidateVolumes()
  return useMutation({
    mutationFn: (id: string) => deleteVolume(id),
    onSuccess: invalidate,
  })
}

export function useNetworks() {
  return useQuery({ queryKey: queryKeys.networks, queryFn: getNetworks, staleTime: 30_000 })
}

/** Static for the life of the build — it describes the host, not the data. */
export function useNetworkModes() {
  return useQuery({
    queryKey: queryKeys.networkModes,
    queryFn: getNetworkModes,
    staleTime: Infinity,
  })
}

export function useForwards(instanceId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.forwards(instanceId ?? ''),
    queryFn: () => getForwards(instanceId as string),
    enabled: Boolean(instanceId),
  })
}

/**
 * The forward presets for one guest family.
 *
 * A catalog, so it is cached indefinitely: it is a property of the backend
 * build, not of anything that changes while the page is open.
 */
export function useForwardPresets(guestOs: GuestOS | undefined) {
  return useQuery({
    queryKey: ['forward-presets', guestOs ?? 'any'],
    queryFn: () => getForwardPresets(guestOs),
    staleTime: Infinity,
  })
}

function useInvalidateForwards(instanceId: string | undefined) {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: queryKeys.forwards(instanceId ?? '') })
    void qc.invalidateQueries({ queryKey: queryKeys.events })
    void qc.invalidateQueries({ queryKey: ['instance'] })
  }
}

export function useAddForward(instanceId: string | undefined) {
  const invalidate = useInvalidateForwards(instanceId)
  return useMutation({
    mutationFn: (payload: {
      host_port: number
      guest_port: number
      protocol?: string
      description?: string | null
    }) => addForward(instanceId as string, payload),
    onSuccess: invalidate,
  })
}

export function useRemoveForward(instanceId: string | undefined) {
  const invalidate = useInvalidateForwards(instanceId)
  return useMutation({
    mutationFn: (forwardId: string) => removeForward(instanceId as string, forwardId),
    onSuccess: invalidate,
  })
}

function useInvalidateInstances() {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: ['instances'] })
    // Every one of these mutations writes an event. The activity list is the
    // one panel where a stale cache reads as "it didn't happen".
    void qc.invalidateQueries({ queryKey: ['instance'] })
    void qc.invalidateQueries({ queryKey: queryKeys.events })
  }
}

export function useLaunchInstance() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    mutationFn: (payload: CreateInstancePayload) => createInstance(payload),
    onSuccess: invalidate,
  })
}

export function useRestartInstance() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    mutationFn: (id: string) => restartInstance(id),
    onSuccess: invalidate,
  })
}

export function useCloneInstance() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => cloneInstance(id, name),
    onSuccess: invalidate,
  })
}

export function useFetchImage() {
  const invalidate = useInvalidateImages()
  return useMutation({
    mutationFn: (payload: {
      name: string
      url: string
      sha256?: string | null
      has_cloud_init: boolean
    }) => fetchImage(payload),
    onSuccess: invalidate,
  })
}

export function useStartInstance() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    mutationFn: (id: string) => startInstance(id),
    onSuccess: invalidate,
  })
}

export function useStopInstance() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    mutationFn: (id: string) => stopInstance(id),
    onSuccess: invalidate,
  })
}

export function useTerminateInstance() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    mutationFn: (id: string) => terminateInstance(id),
    onSuccess: invalidate,
  })
}

export function useRefreshInstances() {
  const invalidate = useInvalidateInstances()
  return useMutation({
    // Keyed so useIsReconciling can see it; see useInstances.
    mutationKey: RECONCILE_KEY,
    mutationFn: () => refreshInstances(),
    onSuccess: invalidate,
  })
}

/**
 * One instance, for the detail view. Polled on the same cadence as the table so
 * a VM's state changes appear at the same speed wherever you're looking.
 */
export function useInstance(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.instance(id ?? ''),
    queryFn: () => getInstance(id as string),
    enabled: Boolean(id),
    refetchInterval: 3_000,
    retry: false, // a 404 is an answer, not a failure worth retrying
  })
}

/**
 * Key pairs. Effectively static between user actions, so no polling — the
 * mutations below invalidate it.
 */
export function useKeyPairs() {
  const [projectId] = useSelectedProject()
  return useQuery({
    queryKey: [...queryKeys.keypairs, projectId],
    queryFn: () => getKeyPairs(projectId),
    staleTime: 30_000,
  })
}

/** Which keys were installed on one instance. Fixed at launch; never polls. */
export function useInstanceKeyPairs(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.instanceKeypairs(id ?? ''),
    queryFn: () => getInstanceKeyPairs(id as string),
    enabled: Boolean(id),
    staleTime: Infinity,
  })
}

function useInvalidateKeyPairs() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: queryKeys.keypairs })
}

export function useImportKeyPair() {
  const invalidate = useInvalidateKeyPairs()
  return useMutation({
    mutationFn: (payload: { name: string; public_key: string }) => importKeyPair(payload),
    onSuccess: invalidate,
  })
}

export function useGenerateKeyPair() {
  const invalidate = useInvalidateKeyPairs()
  return useMutation({
    mutationFn: (name: string) => generateKeyPair(name),
    onSuccess: invalidate,
  })
}

export function useDeleteKeyPair() {
  const invalidate = useInvalidateKeyPairs()
  return useMutation({
    mutationFn: (id: string) => deleteKeyPair(id),
    onSuccess: invalidate,
  })
}

/**
 * Effective backend configuration. Static until the backend restarts — which
 * is exactly when any of it can change — so it is fetched once and held.
 */
export function useSettings() {
  return useQuery({
    queryKey: queryKeys.settings,
    queryFn: getSettings,
    staleTime: Infinity,
  })
}

/**
 * One setting's value by key, or null while it loads.
 *
 * For copy that has to name a real path or a real default rather than the
 * shipped one — the ISO directory and the guest username both move with
 * `IAAS_ISO_DIR` and `IAAS_DEFAULT_VM_USER`, and instructions naming the
 * default on an install that changed it send the user to the wrong place.
 */
export function useSettingValue(key: string): string | null {
  const { data } = useSettings()
  if (!data) return null
  for (const group of data) {
    const found = group.settings.find((setting) => setting.key === key)
    if (found) return String(found.value)
  }
  return null
}

/** Host-side facts for the Settings view. Slow poll: disk and accel do move. */
export function useDiagnostics() {
  return useQuery({
    queryKey: queryKeys.diagnostics,
    queryFn: getDiagnostics,
    refetchInterval: 30_000,
    retry: false,
  })
}

/**
 * An instance's snapshots. Polled while any are in flight: create and delete
 * are backgrounded on the server, so the row's status is what settles.
 */
export function useSnapshots(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.snapshots(id ?? ''),
    queryFn: () => getSnapshots(id as string),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const rows = query.state.data
      const settling = rows?.some((s) => s.status === 'Creating' || s.status === 'Deleting')
      return settling ? 2_000 : false
    },
  })
}

function useInvalidateSnapshots(id: string | undefined) {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: queryKeys.snapshots(id ?? '') })
    void qc.invalidateQueries({ queryKey: queryKeys.instanceEvents(id ?? '') })
    void qc.invalidateQueries({ queryKey: queryKeys.events })
  }
}

/**
 * One instance's history, newest first.
 *
 * Polled, unlike the snapshot list, because the writers are not all in this
 * browser: the reconciler records corrections on its own schedule, and a
 * provisioning job finishes minutes after the request that started it. A panel
 * that only refreshed on user action would show a launch that never completed.
 */
export function useInstanceEvents(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.instanceEvents(id ?? ''),
    queryFn: () => getInstanceEvents(id as string),
    enabled: Boolean(id),
    refetchInterval: 10_000,
  })
}

/** The global feed. Same reasoning, same cadence. */
export function useEvents() {
  return useQuery({
    queryKey: queryKeys.events,
    queryFn: () => getEvents(),
    refetchInterval: 10_000,
  })
}

export function useCreateSnapshot(instanceId: string | undefined) {
  const invalidate = useInvalidateSnapshots(instanceId)
  return useMutation({
    mutationFn: (payload: { name: string; description?: string | null }) =>
      createSnapshot(instanceId as string, payload),
    onSuccess: invalidate,
  })
}

export function useRestoreSnapshot(instanceId: string | undefined) {
  const invalidate = useInvalidateSnapshots(instanceId)
  return useMutation({
    mutationFn: (snapshotId: string) => restoreSnapshot(instanceId as string, snapshotId),
    onSuccess: invalidate,
  })
}

export function useDeleteSnapshot(instanceId: string | undefined) {
  const invalidate = useInvalidateSnapshots(instanceId)
  return useMutation({
    mutationFn: (snapshotId: string) => deleteSnapshot(instanceId as string, snapshotId),
    onSuccess: invalidate,
  })
}

/**
 * A volume's snapshots. Same polling rule as an instance's, and deliberately a
 * separate cache key: these are different rows about a different file, and
 * invalidating one must not imply anything about the other.
 */
export function useVolumeSnapshots(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.volumeSnapshots(id ?? ''),
    queryFn: () => getVolumeSnapshots(id as string),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const rows = query.state.data
      const settling = rows?.some((s) => s.status === 'Creating' || s.status === 'Deleting')
      return settling ? 2_000 : false
    },
  })
}

function useInvalidateVolumeSnapshots(id: string | undefined) {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: queryKeys.volumeSnapshots(id ?? '') })
    // The snapshot's data lives inside the volume file, so the volume list's
    // reported sizes move with it.
    void qc.invalidateQueries({ queryKey: queryKeys.volumes })
  }
}

export function useCreateVolumeSnapshot(volumeId: string | undefined) {
  const invalidate = useInvalidateVolumeSnapshots(volumeId)
  return useMutation({
    mutationFn: (payload: { name: string; description?: string | null }) =>
      createVolumeSnapshot(volumeId as string, payload),
    onSuccess: invalidate,
  })
}

export function useRestoreVolumeSnapshot(volumeId: string | undefined) {
  const invalidate = useInvalidateVolumeSnapshots(volumeId)
  return useMutation({
    mutationFn: (snapshotId: string) => restoreVolumeSnapshot(volumeId as string, snapshotId),
    onSuccess: invalidate,
  })
}

export function useDeleteVolumeSnapshot(volumeId: string | undefined) {
  const invalidate = useInvalidateVolumeSnapshots(volumeId)
  return useMutation({
    mutationFn: (snapshotId: string) => deleteVolumeSnapshot(volumeId as string, snapshotId),
    onSuccess: invalidate,
  })
}
