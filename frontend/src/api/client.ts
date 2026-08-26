/**
 * Single source of truth for all backend HTTP access.
 *
 * Components never import axios directly — they call the typed functions
 * exported here.
 *
 * **The default is this page's own origin**, because the backend serves this
 * bundle. That is the shipped shape: one process, one port, no configuration,
 * and it keeps working when the user changes the port. VITE_API_URL overrides
 * it and is what the Vite dev server needs, since there the dashboard is served
 * by Vite and the API is somewhere else.
 *
 * Everything the API serves lives under API_PREFIX and nothing else does. The
 * dashboard's own routes are the readable ones — `/images`, `/instances/:id` —
 * and they used to be the same eight URLs as the API's. On two origins that was
 * invisible; on one it is a conflict, so the API moved.
 */
import axios, { AxiosError } from 'axios'

/** Where the API is mounted. Mirrors API_PREFIX in backend/kurukuru/product.py. */
const API_PREFIX = '/api'

/** Origin serving the API — this page's own, unless told otherwise. */
export const API_ORIGIN =
  import.meta.env.VITE_API_URL?.replace(/\/$/, '') ||
  (typeof window !== 'undefined' ? window.location.origin : '')

/** Base every request is joined to. */
export const API_URL = `${API_ORIGIN}${API_PREFIX}`

// Default timeout for fast reads (health, list, flavors, create-202).
const http = axios.create({
  baseURL: API_URL,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
  // The session cookie is httpOnly, so script never sees it — but it has to be
  // *sent*, and the dev server is a different origin from the API.
  withCredentials: true,
})

/**
 * The CSRF token for this session.
 *
 * Held in memory rather than in a cookie or localStorage, deliberately. A
 * cookie would be attached automatically by the browser, which is precisely
 * what makes cookies unusable for this job; localStorage would survive a tab
 * that should have forgotten it.
 *
 * It is required on every state-changing request, and that is not belt and
 * braces. `SameSite` is computed from scheme and registrable domain — port is
 * not part of a site — so every localhost port is the same site as the API and
 * the cookie is sent to requests from any of them. Measured: a page on another
 * local port POSTing to the API is answered 403 (cookie sent, CSRF refused),
 * not 401. See docs/SECURITY.md.
 */
/**
 * The CSRF header, spelled once.
 *
 * Sent on every unsafe request and read off the login response, so it appeared
 * twice — and when the backend renamed it, only one of the two moved and login
 * started failing its own CSRF check. HTTP header names are case-insensitive,
 * but axios lower-cases response header keys, so the read below must use the
 * lower-case form; keeping both derived from one constant is what stops them
 * drifting again.
 */
export const CSRF_HEADER = 'X-Kurukuru-CSRF'
const CSRF_HEADER_LOWER = CSRF_HEADER.toLowerCase()

let csrfToken: string | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

export function getCsrfToken(): string | null {
  return csrfToken
}

const UNSAFE = new Set(['post', 'put', 'patch', 'delete'])

http.interceptors.request.use((config) => {
  if (csrfToken && UNSAFE.has((config.method ?? 'get').toLowerCase())) {
    config.headers.set(CSRF_HEADER, csrfToken)
  }
  return config
})

/**
 * Callbacks fired when the API says the session is gone.
 *
 * A 401 can arrive from any query at any time — a background poll is as likely
 * as a click. Rather than every call site handling it, the app subscribes once
 * and shows the login screen.
 */
type Listener = () => void
const unauthenticatedListeners = new Set<Listener>()

export function onUnauthenticated(listener: Listener): () => void {
  unauthenticatedListeners.add(listener)
  return () => unauthenticatedListeners.delete(listener)
}

/**
 * A sentence for the login screen to show about why it is being shown.
 *
 * Landing back at a login form with no explanation is the worst part of any
 * session-expiry flow — "did I get logged out, or did something break?". The
 * one case where the app *knows* the answer is when it caused it: a sign-out,
 * or a password change that deliberately invalidated everything. It is read
 * once and cleared, so a later ordinary expiry does not inherit a stale reason.
 */
let signOutNotice: string | null = null

export function setSignOutNotice(notice: string | null): void {
  signOutNotice = notice
}

export function takeSignOutNotice(): string | null {
  const notice = signOutNotice
  signOutNotice = null
  return notice
}

function notifyUnauthenticated(): void {
  csrfToken = null
  unauthenticatedListeners.forEach((listener) => listener())
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) notifyUnauthenticated()
    return Promise.reject(error)
  },
)

// start/stop/delete block synchronously on the backend while a VM boots or
// tears down — which can take well over 15s, and minutes under software
// emulation. These requests get a timeout aligned with the backend's launch
// window so the mutation resolves with the real result instead of a spurious
// client-side timeout.
const HYPERVISOR_OP_TIMEOUT = 610_000

// --- Authentication ------------------------------------------------------ //

export interface User {
  id: string
  username: string
  is_owner: boolean
  created_at: string
}

export interface ApiTokenRow {
  id: string
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface FirstRunStatus {
  configured: boolean
  /** What the command-line tool is called on this install. */
  cli_name: string
}

/**
 * The name of the CLI, as this backend reports it.
 *
 * Null until `getFirstRunStatus` has answered. The product name is not settled,
 * so no part of the dashboard spells it: copy that names a command reads it
 * from here, and copy that cannot render without it renders nothing rather than
 * guessing. A hardcoded fallback would be the one thing that survives a rename
 * and be wrong — silently, in the instructions someone is following because
 * they are already stuck.
 */
let cliNameValue: string | null = null

export function cliName(): string | null {
  return cliNameValue
}

export async function getFirstRunStatus(): Promise<FirstRunStatus> {
  const { data } = await http.get<FirstRunStatus>('/auth/first-run')
  if (data.cli_name) cliNameValue = data.cli_name
  return data
}

export async function login(username: string, password: string): Promise<User> {
  const response = await http.post<User>('/auth/login', { username, password })
  // The CSRF token comes back in a header rather than a cookie, so that the
  // browser cannot replay it on its own.
  setCsrfToken(response.headers[CSRF_HEADER_LOWER] ?? null)
  return response.data
}

export async function logout(): Promise<void> {
  // Deliberately not awaited for the outcome: whether the server accepted it or
  // the session had already expired, this client is signed out either way.
  await http.post('/auth/logout').catch(() => undefined)
  setCsrfToken(null)
  // A 401 is not coming — logout succeeds — so the "session is gone" event is
  // published here. It is the same event, and it is equally true.
  notifyUnauthenticated()
}

export async function whoami(): Promise<User> {
  const { data } = await http.get<User>('/auth/whoami')
  return data
}

/** Recover the CSRF token after a reload, without asking for the password. */
export async function refreshCsrfToken(): Promise<string> {
  const { data } = await http.get<{ csrf_token: string }>('/auth/csrf')
  setCsrfToken(data.csrf_token)
  return data.csrf_token
}

/** A command as the user should type it, e.g. `kurukuru auth login`. Null when the
 *  backend has not been asked yet — callers omit the sentence rather than
 *  invent a name. */
export function cliCommand(rest: string): string | null {
  const name = cliName()
  return name ? `${name} ${rest}` : null
}

export async function changePassword(
  current_password: string,
  new_password: string,
): Promise<void> {
  await http.post('/auth/password', { current_password, new_password })
  // Every session died, including this one.
  setCsrfToken(null)
}

export async function getApiTokens(): Promise<ApiTokenRow[]> {
  const { data } = await http.get<ApiTokenRow[]>('/auth/tokens')
  return data
}

export async function createApiToken(name: string): Promise<ApiTokenRow & { token: string }> {
  const { data } = await http.post<ApiTokenRow & { token: string }>('/auth/tokens', { name })
  return data
}

export async function revokeApiToken(id: string): Promise<ApiTokenRow> {
  const { data } = await http.delete<ApiTokenRow>(`/auth/tokens/${id}`)
  return data
}

/** A single-use permit to open one instance's console. */
export async function createConsoleTicket(instanceId: string): Promise<string> {
  const { data } = await http.post<{ ticket: string; expires_at: string }>(
    `/instances/${instanceId}/console/ticket`,
  )
  return data.ticket
}

// --- Domain types (mirror backend/app/models.py) ------------------------- //

export type InstanceStatus =
  | 'Pending'
  | 'Provisioning'
  | 'Running'
  | 'Stopped'
  | 'Terminated'
  | 'Error'

/**
 * Registry key of the backend driver that owns an instance.
 *
 * 'qemu' is the only engine that can be launched. 'multipass' is retired and
 * appears solely on historical rows, which the UI must still label correctly.
 */
export type EngineName = 'qemu' | 'multipass'

export interface Instance {
  id: string
  name: string
  /** Preset the instance started from, or "custom". */
  flavor: string
  cpus: number | null
  memory_mb: number | null
  disk_gb: number | null
  engine: EngineName
  status: InstanceStatus
  ip_address: string | null
  created_at: string
  updated_at: string
  error_message: string | null
  ssh_user: string
  /**
   * Host-side runtime detail. QEMU VMs live behind loopback port forwards, so
   * `ssh_port` is how you actually reach them. Rows with no injected SSH key
   * (ISO installs, images without cloud-init) leave the address fields null —
   * the console is their only way in.
   */
  ssh_port: number | null
  vnc_port: number | null
  qmp_port: number | null
  pid: number | null
  /** Accelerator the VM runs under ("whpx" | "tcg"); null until resolved. */
  accel: string | null
  /** Guest display adapter ("std" | "virtio"); null on pre-Phase-7 rows. */
  display: string | null
  /** Boot media filename, when this instance was created from an ISO. */
  iso: string | null
  image_id: string | null
  boot_source: 'image' | 'iso'
  /** Grouping label. Organisational only — never a permission. */
  project_id: string | null
  /** Guest OS family. Drives the OS-specific advice the UI gives. */
  guest_os: GuestOS
  /**
   * Whether a console can be *opened* (QEMU row, accelerator resolved). It is
   * not a promise that a picture will appear — see console_caveat.
   */
  console_supported: boolean
  /**
   * Set only for the combination that can legitimately come up blank: a guest
   * that stays in VGA text mode on standard graphics under hardware
   * acceleration. Advisory — the console is still offered, because whether a
   * guest leaves text mode isn't knowable in advance.
   */
  console_caveat: string | null
  /**
   * Running, but not reachable the way it promised to be — see
   * degraded_reason for which way. Two causes today: no address ever appeared,
   * or the VM's QMP control socket stopped answering while its process stayed
   * alive. The second is not a stopped instance and must not read as one.
   */
  degraded: boolean
  degraded_reason: string | null
  /** Whether the orchestrator's SSH key reached this guest. */
  ssh_enabled: boolean | null
  /** The user's own cloud-config as supplied at launch, if any. */
  user_data: string | null
}

export interface EngineInfo {
  name: EngineName
  available: boolean
  /** QEMU only: "whpx" (hardware) or "tcg" (software emulation fallback). */
  acceleration?: string | null
  accelerated?: boolean
  cpu_model?: string | null
  base_image_present?: boolean
  error?: string
}

/** A sizing preset. Numbers only — the UI formats them for display. */
export interface FlavorSpec {
  cpus: number
  memory_mb: number
  disk_gb: number
}

/** GET /flavors returns { name: {...} } — reshaped to a list. */
export interface Flavor extends FlavorSpec {
  name: string
}

export interface ResourceCapacity {
  total: number
  committed: number
  allocatable: number
  /** Largest a single new instance may request. */
  max_per_instance: number
}

/** GET /host/capacity — totals, commitments and what remains allocatable. */
export interface HostCapacity {
  cpu: ResourceCapacity
  memory_mb: ResourceCapacity
  disk_gb: ResourceCapacity
  memory_available_mb: number
  accel_available: boolean
  accel: string | null
  /** True when the probe failed and limits are permissive guesses. */
  degraded: boolean
  warnings: string[]
}

export async function getHostCapacity(): Promise<HostCapacity> {
  const { data } = await http.get<HostCapacity>('/host/capacity')
  return data
}

export interface Health {
  status: string
  service: string
}

export interface SshKeyInfo {
  public_key: string
  /** Path to the orchestrator private key — every VM trusts only this key. */
  private_key_path: string
  /** @deprecated Original name for private_key_path; kept for compatibility. */
  key_path: string
  ssh_user: string
}

// --- Error helper -------------------------------------------------------- //

/**
 * Extract a human-readable message from a FastAPI error response.
 * FastAPI puts the message in `detail` (string) or, for validation errors,
 * an array of `{msg, loc}` objects.
 */
export function apiErrorMessage(err: unknown): string {
  const axiosErr = err as AxiosError<{ detail?: unknown }>
  const detail = axiosErr.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
      .join('; ')
  }
  if (axiosErr.message) return axiosErr.message
  return 'Unexpected error'
}

/** HTTP status code of a failed request, if any (e.g. 409 for duplicate name). */
export function apiErrorStatus(err: unknown): number | undefined {
  return (err as AxiosError).response?.status
}

// --- Endpoints ----------------------------------------------------------- //

export async function getHealth(): Promise<Health> {
  const { data } = await http.get<Health>('/health')
  return data
}

export async function getSshKey(): Promise<SshKeyInfo> {
  const { data } = await http.get<SshKeyInfo>('/ssh-key')
  return data
}

export async function getFlavors(): Promise<Flavor[]> {
  const { data } = await http.get<Record<string, FlavorSpec>>('/flavors')
  return Object.entries(data).map(([name, spec]) => ({ name, ...spec }))
}

export async function listInstances(
  includeTerminated = false,
  projectId?: string | null,
): Promise<Instance[]> {
  const params: Record<string, unknown> = {}
  if (includeTerminated) params.include_terminated = true
  // Omitted, not null: "all projects" is the absence of a filter, and sending
  // project_id=null would ask the API for rows filed under nothing.
  if (projectId) params.project_id = projectId
  const { data } = await http.get<Instance[]>('/instances', {
    params: Object.keys(params).length ? params : undefined,
  })
  return data
}

export async function getInstance(id: string): Promise<Instance> {
  const { data } = await http.get<Instance>(`/instances/${id}`)
  return data
}

/**
 * WebSocket URL for an instance's VNC console.
 *
 * Built from API_ORIGIN, which is this page's own origin unless VITE_API_URL
 * overrides it — so in a packaged install the socket is same-origin, and in dev
 * it points at wherever the backend actually is. The dev server proxies this
 * exact path, so the DEV branch this used to carry is no longer a difference.
 */
export function consoleWsUrl(instanceId: string, ticket: string): string {
  const url = new URL(
    `${API_PREFIX}/instances/${encodeURIComponent(instanceId)}/console`,
    API_ORIGIN,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  // The browser WebSocket API cannot set request headers, so the credential
  // has to travel in the URL. That is exactly why it is a ticket rather than
  // the session or an API token: single-use, 30 seconds, bound to this one
  // instance and to the session that minted it. A URL that leaks into a log is
  // then worth nothing by the time anyone reads it.
  url.searchParams.set('ticket', ticket)
  return url.toString()
}

/**
 * Close codes the console endpoint uses to explain a refusal (RFC 6455 reserves
 * 4000-4999 for applications). Mirrors backend/kurukuru/console.py.
 */
export const CONSOLE_CLOSE = {
  UNAUTHENTICATED: 4401,
  NOT_FOUND: 4404,
  CONFLICT: 4409,
  VNC_UNAVAILABLE: 4502,
} as const

export type ImageStatus = 'Importing' | 'Available' | 'Error'

export interface Image {
  id: string
  name: string
  filename: string
  format: 'qcow2' | 'raw' | 'vmdk' | 'vdi'
  source: 'builtin' | 'imported'
  virtual_size_bytes: number | null
  actual_size_bytes: number | null
  has_cloud_init: boolean
  status: ImageStatus
  error_message: string | null
  created_at: string
}

export async function getImages(projectId?: string | null): Promise<Image[]> {
  const { data } = await http.get<Image[]>('/images', {
    params: projectId ? { project_id: projectId } : undefined,
  })
  return data
}

export interface ImportImagePayload {
  name: string
  /** Path on the *backend's* filesystem — this is a registration, not an upload. */
  path: string
  has_cloud_init: boolean
}

export async function importImage(payload: ImportImagePayload): Promise<Image> {
  const { data } = await http.post<Image>('/images/import', payload)
  return data
}

export async function deleteImage(id: string): Promise<void> {
  await http.delete(`/images/${id}`, { timeout: HYPERVISOR_OP_TIMEOUT })
}

export interface IsoFile {
  name: string
  size_bytes: number
  modified_at: string
}

/** Boot media the backend can see; placed in its ISO directory by hand. */
export async function getIsos(): Promise<IsoFile[]> {
  const { data } = await http.get<IsoFile[]>('/isos')
  return data
}

export async function getEngines(): Promise<EngineInfo[]> {
  const { data } = await http.get<EngineInfo[]>('/engines')
  return data
}

// --- Settings & diagnostics ---------------------------------------------- //

/** One configuration value, with the env var that controls it. */
export interface SettingEntry {
  key: string
  label: string
  value: string | number | boolean | null
  /** The IAAS_ environment variable to set. Restart required. */
  env: string
  /**
   * Presentation hint. `path` gets the app's one path treatment — mono,
   * truncated, copyable — because a client cannot tell a path from any other
   * string by looking at it. Optional: an older backend sends nothing.
   */
  kind?: 'text' | 'path'
}

export interface SettingGroup {
  name: string
  description: string
  settings: SettingEntry[]
}

export async function getSettings(): Promise<SettingGroup[]> {
  const { data } = await http.get<{ groups: SettingGroup[] }>('/settings')
  return data.groups
}

/** Host-side facts: what the CLI's `doctor` command reads. */
/**
 * A named guest port offered as one click instead of two numbers.
 *
 * Fetched rather than hardcoded, for the reason the network-mode catalog is:
 * the API, the UI and the docs cannot then drift into disagreeing about what
 * is offered. A preset fills the form in — it creates nothing, and the ordinary
 * create endpoint still does the work and the collision check.
 */
export interface ForwardPreset {
  key: string
  label: string
  guest_port: number
  protocol: 'tcp' | 'udp'
  description: string
  guest_os: GuestOS | null
}

export async function getForwardPresets(guestOs?: GuestOS): Promise<ForwardPreset[]> {
  const { data } = await http.get<ForwardPreset[]>('/forwards/presets', {
    params: guestOs ? { guest_os: guestOs } : undefined,
  })
  return data
}

/** One thing the hypervisor build either can or cannot do. */
export interface QemuCapability {
  key: string
  label: string
  available: boolean
  /** What was observed, verbatim — more use than "unsupported". */
  detail: string
  /** What the user loses. Null when nothing does. */
  consequence: string | null
}

export interface QemuSupport {
  version: string | null
  raw: string | null
  /** A development snapshot rather than a release. */
  prerelease: boolean
  /** 'ok' | 'untested' | 'too-old' | 'prerelease' | 'unknown' */
  status: string
  /** Whether the build sits inside the tested range. Advisory only. */
  supported: boolean
  /** Ready-to-show sentences; every one is a caveat, never a blocker. */
  warnings: string[]
  capabilities: QemuCapability[]
}

export interface Diagnostics {
  api: { version: string }
  python: string
  engine: {
    available: boolean
    version?: string | null
    accel?: string | null
    accel_available?: boolean
    base_image?: string
    base_image_present?: boolean
    error?: string
    /**
     * Version verdict plus capability probes. The two are separate because a
     * version number cannot see whether the build was compiled with TPM
     * support or whether UEFI works under the accelerator in use — and those
     * are what decide whether a given guest can be installed at all.
     */
    support?: QemuSupport | null
  }
  instance_store: {
    path: string
    exists: boolean
    writable: boolean
    free_bytes: number | null
  }
  ssh_key: { present: boolean; private_key_path: string | null; error: string | null }
}

export async function getDiagnostics(): Promise<Diagnostics> {
  const { data } = await http.get<Diagnostics>('/diagnostics')
  return data
}

// --- Key pairs ----------------------------------------------------------- //

export type KeyPairSource = 'orchestrator' | 'imported' | 'generated'

export interface KeyPair {
  id: string
  name: string
  public_key: string
  /** OpenSSH's SHA256:... spelling. */
  fingerprint: string
  /** Display label: ed25519, rsa, ecdsa-256... */
  key_type: string
  source: KeyPairSource
  has_private_key: boolean
  /**
   * Where the private half lives on the backend. The contents are never
   * returned by any endpoint — this is what goes behind `ssh -i`.
   */
  private_key_path: string | null
  created_at: string
}

/** One key installed on an instance, as recorded when it launched. */
export interface InstalledKeyPair {
  keypair_id: string
  name: string
  fingerprint: string
  /** The catalog row is gone; the key is still in the guest's authorized_keys. */
  deleted: boolean
}

export async function getKeyPairs(projectId?: string | null): Promise<KeyPair[]> {
  const { data } = await http.get<KeyPair[]>('/keypairs', {
    params: projectId ? { project_id: projectId } : undefined,
  })
  return data
}

export async function importKeyPair(payload: {
  name: string
  public_key: string
}): Promise<KeyPair> {
  const { data } = await http.post<KeyPair>('/keypairs/import', payload)
  return data
}

export async function generateKeyPair(name: string): Promise<KeyPair> {
  const { data } = await http.post<KeyPair>('/keypairs/generate', { name })
  return data
}

export async function deleteKeyPair(id: string): Promise<void> {
  await http.delete(`/keypairs/${id}`)
}

export async function getInstanceKeyPairs(id: string): Promise<InstalledKeyPair[]> {
  const { data } = await http.get<InstalledKeyPair[]>(`/instances/${id}/keypairs`)
  return data
}

// --- Volume snapshots ----------------------------------------------------- //
// A separate resource from an instance snapshot, not a variant of one. They
// capture different files and neither includes the other, which is why the two
// live under different routes and are typed apart rather than sharing a shape
// with a discriminator.

export interface VolumeSnapshot {
  id: string
  volume_id: string
  name: string
  description: string | null
  size_bytes: number | null
  status: SnapshotStatus
  error_message: string | null
  created_at: string
}

export async function getVolumeSnapshots(volumeId: string): Promise<VolumeSnapshot[]> {
  const { data } = await http.get<VolumeSnapshot[]>(`/volumes/${volumeId}/snapshots`)
  return data
}

export async function createVolumeSnapshot(
  volumeId: string,
  payload: { name: string; description?: string | null },
): Promise<VolumeSnapshot> {
  const { data } = await http.post<VolumeSnapshot>(`/volumes/${volumeId}/snapshots`, payload)
  return data
}

export async function restoreVolumeSnapshot(
  volumeId: string,
  snapshotId: string,
): Promise<VolumeSnapshot> {
  const { data } = await http.post<VolumeSnapshot>(
    `/volumes/${volumeId}/snapshots/${snapshotId}/restore`,
  )
  return data
}

export async function deleteVolumeSnapshot(
  volumeId: string,
  snapshotId: string,
): Promise<VolumeSnapshot> {
  const { data } = await http.delete<VolumeSnapshot>(
    `/volumes/${volumeId}/snapshots/${snapshotId}`,
  )
  return data
}

// --- Snapshots ------------------------------------------------------------ //

export type SnapshotStatus = 'Creating' | 'Available' | 'Error' | 'Deleting'

export interface Snapshot {
  id: string
  instance_id: string
  name: string
  description: string | null
  /** VM-state size; zero for a stopped-instance snapshot (see DECISIONS #15). */
  size_bytes: number | null
  status: SnapshotStatus
  error_message: string | null
  created_at: string
}

export async function getSnapshots(instanceId: string): Promise<Snapshot[]> {
  const { data } = await http.get<Snapshot[]>(`/instances/${instanceId}/snapshots`)
  return data
}

export async function createSnapshot(
  instanceId: string,
  payload: { name: string; description?: string | null },
): Promise<Snapshot> {
  const { data } = await http.post<Snapshot>(`/instances/${instanceId}/snapshots`, payload)
  return data
}

export async function restoreSnapshot(
  instanceId: string,
  snapshotId: string,
): Promise<Snapshot> {
  const { data } = await http.post<Snapshot>(
    `/instances/${instanceId}/snapshots/${snapshotId}/restore`,
    undefined,
    { timeout: HYPERVISOR_OP_TIMEOUT },
  )
  return data
}

export async function deleteSnapshot(
  instanceId: string,
  snapshotId: string,
): Promise<Snapshot> {
  const { data } = await http.delete<Snapshot>(
    `/instances/${instanceId}/snapshots/${snapshotId}`,
  )
  return data
}

/**
 * Closed on purpose — the UI renders an icon and a colour per kind, so an
 * unrecognised value would arrive as a blank row. Kept in step with the
 * backend's `EventKind` enum.
 */
export type EventKind =
  | 'created'
  | 'provisioning_started'
  | 'provisioning_succeeded'
  | 'provisioning_failed'
  | 'started'
  | 'stopped'
  | 'terminated'
  | 'force_terminated'
  | 'errored'
  | 'snapshot_created'
  | 'snapshot_restored'
  | 'snapshot_deleted'
  | 'restarted'
  | 'cloned'
  | 'volume_attached'
  | 'volume_detached'
  | 'port_forward_added'
  | 'port_forward_removed'
  | 'reconciled'
  | 'image_import'

/** Who acted. There is no authentication, so this is never a person. */
export type EventActor = 'api' | 'reconciler' | 'system'

export interface InstanceEvent {
  id: string
  /** Null for events that belong to no single instance (an image import). */
  instance_id: string | null
  instance_name: string
  occurred_at: string
  kind: EventKind
  actor: EventActor
  summary: string
  /** Longer text — an error's full message, or a reconciler diff. */
  detail: string | null
}

export async function getInstanceEvents(
  instanceId: string,
  limit = 50,
): Promise<InstanceEvent[]> {
  const { data } = await http.get<InstanceEvent[]>(`/instances/${instanceId}/events`, {
    params: { limit },
  })
  return data
}

export async function getEvents(limit = 100): Promise<InstanceEvent[]> {
  const { data } = await http.get<InstanceEvent[]>('/events', { params: { limit } })
  return data
}

/**
 * A grouping label. **Not** an isolation boundary — there is no auth, so
 * filtering by project changes what a view shows, never what a caller may do.
 */
export interface Project {
  id: string
  name: string
  description: string | null
  is_default: boolean
  created_at: string
  instance_count: number
  image_count: number
  keypair_count: number
}

export async function getProjects(): Promise<Project[]> {
  const { data } = await http.get<Project[]>('/projects')
  return data
}

export async function createProject(payload: {
  name: string
  description?: string | null
}): Promise<Project> {
  const { data } = await http.post<Project>('/projects', payload)
  return data
}

export async function renameProject(id: string, name: string): Promise<Project> {
  const { data } = await http.patch<Project>(`/projects/${id}`, { name })
  return data
}

export async function deleteProject(id: string): Promise<void> {
  await http.delete(`/projects/${id}`)
}

export interface IaasNetwork {
  id: string
  name: string
  mode: 'user' | 'host_only' | 'bridged'
  cidr: string | null
  is_default: boolean
  project_id: string | null
  created_at: string
  instance_count: number
}

/** What a deferred mode would require of the operator. Served, not hard-coded,
 *  so the UI and the API cannot disagree about what to install. */
export interface NetworkModeInfo {
  mode: string
  label: string
  summary: string
  blocker?: string
  requires?: string
}

export interface NetworkModes {
  available: NetworkModeInfo[]
  deferred: NetworkModeInfo[]
}

export interface PortForward {
  id: string
  instance_id: string
  host_port: number
  guest_port: number
  protocol: 'tcp' | 'udp'
  description: string | null
  created_at: string | null
  /** The SSH forward: derived from the instance's pinned port, not stored.
   *  Must render read-only — it cannot be deleted. */
  derived: boolean
}

export async function getNetworks(): Promise<IaasNetwork[]> {
  const { data } = await http.get<IaasNetwork[]>('/networks')
  return data
}

export async function getNetworkModes(): Promise<NetworkModes> {
  const { data } = await http.get<NetworkModes>('/networks/modes')
  return data
}

export async function getForwards(instanceId: string): Promise<PortForward[]> {
  const { data } = await http.get<PortForward[]>(`/instances/${instanceId}/forwards`)
  return data
}

export async function addForward(
  instanceId: string,
  payload: { host_port: number; guest_port: number; protocol?: string; description?: string | null },
): Promise<PortForward> {
  const { data } = await http.post<PortForward>(`/instances/${instanceId}/forwards`, payload)
  return data
}

export async function removeForward(instanceId: string, forwardId: string): Promise<void> {
  await http.delete(`/instances/${instanceId}/forwards/${forwardId}`)
}

export type VolumeStatus = 'Creating' | 'Available' | 'Attached' | 'Error'

/** An additional disk. Outlives the instances it is attached to. */
export interface Volume {
  id: string
  name: string
  size_gb: number
  format: string
  path: string
  status: VolumeStatus
  error_message: string | null
  attached_instance_id: string | null
  attached_instance_name: string | null
  /** Guest OS of the instance it is attached to; null when detached. */
  attached_instance_guest_os: GuestOS | null
  /** Position in the instance's drive order; decides the guest device name. */
  attach_order: number | null
  /** The **Linux** device name — `vdb`, `vdc`. A hint; `lsblk` is authority. */
  device_hint: string | null
  /** The same disk as Windows Disk Management numbers it — `Disk 1`. */
  windows_disk_hint: string | null
  project_id: string | null
  created_at: string
}

export async function getVolumes(params?: {
  projectId?: string | null
  instanceId?: string
}): Promise<Volume[]> {
  const query: Record<string, string> = {}
  if (params?.projectId) query.project_id = params.projectId
  if (params?.instanceId) query.instance_id = params.instanceId
  const { data } = await http.get<Volume[]>('/volumes', {
    params: Object.keys(query).length ? query : undefined,
  })
  return data
}

export async function createVolume(payload: {
  name: string
  size_gb: number
  project_id?: string | null
}): Promise<Volume> {
  const { data } = await http.post<Volume>('/volumes', payload)
  return data
}

export async function attachVolume(id: string, instanceId: string): Promise<Volume> {
  const { data } = await http.post<Volume>(`/volumes/${id}/attach`, {
    instance_id: instanceId,
  })
  return data
}

export async function detachVolume(id: string): Promise<Volume> {
  const { data } = await http.post<Volume>(`/volumes/${id}/detach`)
  return data
}

export async function deleteVolume(id: string): Promise<void> {
  await http.delete(`/volumes/${id}`)
}

/** Guest OS family. Only `linux` can be provisioned today — the API refuses
 *  `windows` with a message naming what is missing. */
export type GuestOS = 'linux' | 'windows'

export async function restartInstance(id: string): Promise<Instance> {
  const { data } = await http.post<Instance>(`/instances/${id}/restart`, undefined, {
    timeout: HYPERVISOR_OP_TIMEOUT,
  })
  return data
}

export async function cloneInstance(id: string, name: string): Promise<Instance> {
  const { data } = await http.post<Instance>(`/instances/${id}/clone`, { name })
  return data
}

export async function fetchImage(payload: {
  name: string
  url: string
  sha256?: string | null
  has_cloud_init: boolean
}): Promise<Image> {
  const { data } = await http.post<Image>('/images/fetch', payload)
  return data
}

export type AccelChoice = 'auto' | 'whpx' | 'tcg'
export type DisplayChoice = 'std' | 'virtio'

export interface CreateInstancePayload {
  name: string
  /** Named starting point; explicit fields below override it. */
  preset?: string
  cpus?: number
  memory_mb?: number
  disk_gb?: number
  /** @deprecated Older alias for `preset`. */
  flavor?: string
  engine?: EngineName
  /** 'auto' lets the backend pick; it resolves to hardware acceleration. */
  accel?: AccelChoice
  /** Guest display adapter; 'std' works without guest drivers. */
  display?: DisplayChoice
  /**
   * Guest family. Selects the disk bus, NIC model, graphics adapter and CPU
   * model together — Windows Setup has inbox drivers for none of the
   * paravirtualised devices Linux uses. Omitted means Linux, which is what
   * every instance created before this existed is.
   */
  guest_os?: GuestOS
  /** Filename within the backend's ISO directory; makes this an ISO instance. */
  iso?: string | null
  image_id?: string | null
  /**
   * Keys to install in the guest. Omitted means the orchestrator key alone —
   * exactly what every instance got before key pairs existed. An explicit []
   * means no keys at all (console-only).
   */
  keypair_ids?: string[]
  /**
   * Raw cloud-config, merged onto the generated one rather than replacing it.
   * Refused for ISO instances, which have no cloud-init to read it.
   */
  user_data?: string | null
}

export async function createInstance(payload: CreateInstancePayload): Promise<Instance> {
  const { data } = await http.post<Instance>('/instances', payload)
  return data
}

export async function startInstance(id: string): Promise<Instance> {
  const { data } = await http.post<Instance>(`/instances/${id}/start`, undefined, {
    timeout: HYPERVISOR_OP_TIMEOUT,
  })
  return data
}

export async function stopInstance(id: string): Promise<Instance> {
  const { data } = await http.post<Instance>(`/instances/${id}/stop`, undefined, {
    timeout: HYPERVISOR_OP_TIMEOUT,
  })
  return data
}

export async function terminateInstance(id: string): Promise<Instance> {
  const { data } = await http.delete<Instance>(`/instances/${id}`, {
    timeout: HYPERVISOR_OP_TIMEOUT,
  })
  return data
}

/** POST /instances/refresh — forces reconciliation with the hypervisor. */
export async function refreshInstances(): Promise<Instance[]> {
  const { data } = await http.post<Instance[]>('/instances/refresh')
  return data
}
