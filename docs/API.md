# API reference

Base URL: `http://localhost:8000`. No authentication — see the security note in
the [README](../README.md#project-status).

Interactive docs are served at `/docs`; the machine-readable schema is at
`/openapi.json`. This document is written against that schema. The declared
status codes below are the ones FastAPI advertises; the **Errors** tables list
what the handlers actually raise, which is more specific.

Note that FastAPI reports request-validation failures as `422` with a
*structured* `detail` (a list of field errors). Application-level refusals also
use `422`, but with `detail` as a plain sentence. Both shapes appear below.

---

## System

### `GET /health`

Liveness, plus whether the hypervisor underneath is usable. A control plane that
answers "ok" while its engine is broken sends people debugging the wrong layer.

```json
{
  "status": "ok",
  "service": "Local IaaS Orchestrator",
  "engine": {
    "name": "qemu",
    "available": true,
    "version": "QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-gbd0a254583)",
    "accel": "whpx",
    "accel_available": true,
    "base_image_present": true
  }
}
```

`status` is `"degraded"` when the engine is unavailable. `version` is `null` if
the binary could not be run.

### `GET /engines`

The compute-engine catalog and each driver's live status. QEMU reports its
acceleration mode here.

```json
[{"name": "qemu", "available": true, "acceleration": "whpx", "accelerated": true,
  "cpu_model": "qemu64", "base_image": "...", "base_image_present": true}]
```

### `GET /diagnostics`

Facts about the machine the **backend** runs on, for clients that cannot see it
— `iaas doctor` is the one that exists. Every field here is a property of the
backend's host: whether its instance store is writable, how much room is left on
that volume, whether the orchestrator's keypair exists. A client that answered
these from its own filesystem would be describing the wrong machine the moment
the two are not the same.

```json
{
  "api": {"version": "0.1.0"},
  "python": "3.13.7",
  "engine": {
    "available": true,
    "version": "QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-gbd0a254583)",
    "accel": "whpx", "accel_available": true,
    "base_image": "C:\\Users\\you\\.local-iaas\\qemu\\base-images\\noble-server-cloudimg-amd64.img",
    "base_image_present": true
  },
  "instance_store": {
    "path": "C:\\Users\\you\\.local-iaas\\qemu\\instances",
    "exists": true, "writable": true, "free_bytes": 39504084992
  },
  "ssh_key": {"present": true, "private_key_path": "...", "error": null}
}
```

Facts, not verdicts: no pass/fail and no remedies, so each client applies its own
idea of healthy. It always answers — a broken engine becomes
`{"available": false, "error": "..."}` rather than a 500, since this is most
needed exactly when something is wrong.

**Scope.** This API has no authentication, so the payload is held to the
minimum `doctor` reads. No key contents — the private key is never read, and the
public half is `GET /ssh-key`'s job; no configuration dump; and no path that
`/engines` or `/ssh-key` does not already report. The three paths present
(`base_image`, `instance_store.path`, `ssh_key.private_key_path`) are each one
`doctor` prints, because a remedy that cannot say where it looked is not a
remedy.

`instance_store` reports the deepest *existing* ancestor's writability and free
space when the directory has not been created yet, because on a fresh install
the useful answer is about the volume it will live on. Requesting `ssh_key`
generates the keypair on first access, as `GET /ssh-key` does.

### `GET /host/capacity`

What the host can still give a new instance. Totals, what is already committed,
and what remains allocatable — the launch form needs all three to explain a
limit rather than merely enforce one. Cached for `IAAS_CAPACITY_CACHE_SECONDS`.

```json
{
  "cpu":       {"total": 14, "committed": 0, "allocatable": 28, "max_per_instance": 14},
  "memory_mb": {"total": 15828, "committed": 0, "allocatable": 13780, "max_per_instance": 13780},
  "disk_gb":   {"total": 473, "committed": 0, "allocatable": 38, "max_per_instance": 38},
  "memory_available_mb": 1693,
  "accel_available": true,
  "accel": "whpx",
  "degraded": false,
  "warnings": []
}
```

- CPU `allocatable` exceeds `total` because vCPUs timeshare
  (`IAAS_CPU_OVERSUBSCRIBE_FACTOR`), but `max_per_instance` never exceeds the
  real core count.
- Memory `allocatable` = total − host reserve − committed.
- Disk `allocatable` is *free space*, not total minus committed: qcow2 overlays
  are sparse, so committed size is not consumed space.
- `degraded: true` means the probe failed (psutil missing or raising). Limits are
  then permissive and `warnings` explains why.

### `GET /flavors`

Sizing presets. Starting points, not limits — a client may send any numbers.

```json
{"small": {"cpus": 1, "memory_mb": 1024, "disk_gb": 5},
 "medium": {...}, "large": {...}}
```

Numbers only. This endpoint used to return each size twice — `memory_mb` **and**
a pre-formatted `memory: "1G"`, likewise `disk_gb`/`disk` — with nothing keeping
the two spellings in agreement. The formatted pair was removed; clients format
sizes themselves, which the dashboard already did everywhere else.

### `GET /isos`

Boot media in `IAAS_ISO_DIR`, newest first. Files are placed there by hand;
there is no upload endpoint.

```json
[{"name": "alpine-virt-3.21.7-x86_64.iso", "size_bytes": 67108864,
  "modified_at": "2026-08-08T21:06:15.847615+00:00"}]
```

### `GET /ssh-key`

The orchestrator's keypair. Only the *public* key contents are returned;
`private_key_path` is a path for `ssh -i`.

```json
{"public_key": "ssh-ed25519 AAAA... local-iaas-orchestrator",
 "private_key_path": "C:\\Users\\you\\.local-iaas\\keys\\id_ed25519",
 "key_path": "...", "ssh_user": "iaas"}
```

`key_path` is a deprecated alias for `private_key_path`. The keypair is
generated on first access.

| Code | Meaning |
|---|---|
| 503 | The keypair could not be created or read (e.g. `ssh-keygen` missing) |

---

## Instances

### `POST /instances` → 202

Accepts a launch request and provisions in the background. Returns the `Pending`
record immediately; poll `GET /instances/{id}` for progress.

**Request** — every field optional except `name`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | — | `^[a-z][a-z0-9-]{1,30}$`. Unique among non-Terminated instances |
| `preset` | string | `small` | `small`/`medium`/`large`. Fills in sizing when the numbers are omitted |
| `flavor` | string | `small` | Deprecated alias for `preset` |
| `cpus` | int | from preset | Overrides the preset |
| `memory_mb` | int | from preset | Overrides the preset |
| `disk_gb` | int | from preset | Overrides the preset |
| `engine` | string | `qemu` | Only `qemu`. `multipass` is retired and refused with an explanation |
| `iso` | string | `null` | Filename within the ISO directory. Makes this an ISO instance |
| `image_id` | string | `null` | Image to back the disk. Defaults to the built-in Ubuntu image |
| `accel` | string | `auto` | `auto`/`whpx`/`tcg`. `auto` resolves to hardware acceleration |
| `display` | string | `std` | `std` (works everywhere) or `virtio` (needed for a console on text-mode guests) |
| `keypair_ids` | list | orchestrator key | Keys to install. Omitted = the orchestrator key alone (the pre-key-pair behaviour). An explicit `[]` means none, producing a console-only instance |
| `user_data` | string | `null` | Raw cloud-config, **merged** with the generated one rather than replacing it. Refused for ISO instances |

**Response**: an instance record (see `GET /instances/{id}`).

**Errors**

| Code | Meaning |
|---|---|
| 409 | An instance with that name already exists and is not Terminated. Detail names the blocking state |
| 409 | The chosen image is not `Available` (still importing, or errored) |
| 422 | Sizing exceeds allocatable capacity. **Detail contains the arithmetic** |
| 422 | Sizing below a floor (1 vCPU, 512 MB, 1 GB) |
| 422 | Disk smaller than the backing image's virtual size — growing is fine, shrinking is not |
| 422 | Unknown preset, unknown image, or an ISO that is missing or outside the ISO directory |
| 422 | `engine: "multipass"` — retired, with an explanation |
| 422 | An unknown `keypair_ids` entry |
| 422 | `user_data` that is not valid YAML (the detail carries the parser's line and column), is not a mapping, or was sent for an ISO instance |
| 503 | The hypervisor is unavailable |

Example capacity refusal:

```json
{"detail": "Requested 60000 MB but only 13780 MB allocatable (2048 MB host reserve, 0 MB committed to running instances)."}
```

**How `user_data` is merged.** The supplied document is parsed and combined
with the generated cloud-config structurally — never concatenated as text.
Mappings merge recursively; on a leaf conflict the user's value wins; lists are
concatenated with duplicates dropped. That last rule is the one with teeth:
replacing `users:` (the natural reading of "the user wins") would drop the
account the SSH keys were installed on, and the instance would come up with no
way in. The stored `user_data` on the instance is what was *supplied*, not the
merged result.

### `GET /instances` → 200

Query: `include_terminated` (bool, default `false`).

### `GET /instances/{id}` → 200

```json
{
  "id": "…", "name": "web-one", "status": "Running",
  "flavor": "custom", "cpus": 3, "memory_mb": 3072, "disk_gb": 12,
  "engine": "qemu", "boot_source": "image",
  "ip_address": "127.0.0.1", "ssh_port": 2200, "vnc_port": 5900,
  "qmp_port": 4400, "pid": 23824,
  "accel": "whpx", "display": "std", "ssh_enabled": true,
  "iso": null, "image_id": "…",
  "console_supported": true,
  "console_caveat": "Guests that stay in VGA text mode…",
  "degraded": false, "degraded_reason": null,
  "error_message": null, "ssh_user": "iaas",
  "created_at": "…", "updated_at": "…"
}
```

Derived fields worth understanding:

- **`console_supported`** — whether the console can be *opened*. True for any
  QEMU row whose accelerator has been resolved. It is not a promise that pixels
  will appear. **Renamed from `console_available`**, which read as a promise the
  field never made; whether a picture actually arrives depends on what the guest
  does after boot, and `console_caveat` is what speaks to that.
- **`console_caveat`** — set only for the combination that can legitimately come
  up blank (standard graphics under hardware acceleration). Advisory, because
  whether a guest leaves VGA text mode is a property of the guest.
- **`degraded`** / **`degraded_reason`** — `Running`, expected an address, still
  has none after `IAAS_DEGRADED_AFTER_SECONDS`. Never set for console-only
  instances.
- **`ssh_enabled`** — whether the orchestrator's key reached this guest. False
  for ISO installs and images without cloud-init.
- **`flavor`** — the preset the instance *started from*, or `"custom"`. A label;
  `cpus`/`memory_mb`/`disk_gb` are the truth.

| Code | Meaning |
|---|---|
| 404 | No such instance |

### `GET /instances/{id}/keypairs` → 200

Which keys are in this guest's `authorized_keys`, as recorded at launch.

```json
[{"keypair_id": "…", "name": "my-laptop", "fingerprint": "SHA256:…", "deleted": false}]
```

`deleted` marks a key whose catalog row is gone. The key is still in the guest —
removing the record never reached into the VM.

### `POST /instances/{id}/start` → 200

Only from `Stopped`. Blocks until the VM is up (can take minutes under software
emulation), then returns the reconciled record.

| Code | Meaning |
|---|---|
| 404 | No such instance |
| 409 | Not in `Stopped` state — detail names the current one |
| 409 | The row's engine is retired; the instance can only be terminated |
| 502 | The hypervisor rejected the operation; the row is marked `Error` |
| 503 | The hypervisor is unavailable |

### `POST /instances/{id}/stop` → 200

Only from `Running`. Sends an ACPI powerdown, waits, then force-kills after
`IAAS_QEMU_SHUTDOWN_TIMEOUT_SECONDS`. Ports stay pinned; the pid clears.

Same error codes as `start`, with the 409 requiring `Running`.

### `DELETE /instances/{id}` → 200

Query: `force` (bool, default `false`).

Destroys the VM and its instance directory, then marks the row `Terminated`.
Allowed from **any** state. The audit row is retained, not deleted.

`force=true` clears the row even when the driver fails to destroy the VM. It
still *attempts* the destroy — it only ignores the failure — so the normal path
is unchanged and no VM is skipped. Without it, a driver failure is a 502 and the
row is marked `Error`, which is the right default: a row claiming `Terminated`
while its VM still runs is a lie, and the usual fix is to retry once the
hypervisor recovers. `force` exists for the case that never recovers, and the
leak it can cause is the caller's to accept.

Idempotent: already-terminated rows return unchanged without touching the
hypervisor. That is load-bearing — another instance may have reused the name, and
destroying by name here would tear down *its* VM.

An instance on a retired engine is force-terminated: the row is cleared without a
driver call, because there is no hypervisor left to ask.

| Code | Meaning |
|---|---|
| 404 | No such instance |
| 502 | The hypervisor failed to destroy it; the row is marked `Error` |
| 503 | The hypervisor is unavailable |

### `POST /instances/refresh` → 200

Runs a reconciliation pass immediately and returns the live (non-Terminated)
set. The same pass runs on a timer.

| Code | Meaning |
|---|---|
| 502 / 503 | An engine failed or was unavailable |

### `WS /instances/{id}/console`

Not in the OpenAPI schema — WebSocket routes are not described there.

Binary frames, byte-transparent, proxied to the VM's VNC socket. Refusals arrive
as close codes **after** the socket is accepted, with a human-readable reason:

| Close code | Meaning |
|---|---|
| 1000 | Normal close |
| 4404 | No such instance |
| 4409 | Not a QEMU instance, not `Running`, or no VNC port recorded |
| 4502 | The VNC socket could not be reached — the VM may have stopped |

---

## Key pairs

SSH public keys that can be installed on new instances. The orchestrator's own
keypair is adopted as a row on first run — never regenerated, because every
instance launched before this existed trusts it.

### `GET /keypairs` · `GET /keypairs/{id}`

```json
{"id": "…", "name": "orchestrator", "public_key": "ssh-ed25519 AAAA… local-iaas-orchestrator",
 "fingerprint": "SHA256:O7RU0qL/HgPheAh6AwKPMPMyCBzx0p2W2duNePzv57c",
 "key_type": "ed25519", "source": "orchestrator", "has_private_key": true,
 "private_key_path": "C:\Users\you\.local-iaas\keys\id_ed25519", "created_at": "…"}
```

`source` is `orchestrator`, `imported` or `generated`. `private_key_path` is a
path, never contents — **no endpoint returns a private key**, at creation or
afterwards.

### `POST /keypairs/import` → 201

```json
{"name": "my-laptop", "public_key": "ssh-ed25519 AAAAC3Nza… you@machine"}
```

The key is parsed in-process: base64 decoded, and the algorithm embedded in the
blob checked against the one on the line. That catches a truncated paste and a
body belonging to a different algorithm, neither of which base64 validation
alone would notice. The fingerprint is computed the way OpenSSH computes it.

| Code | Meaning |
|---|---|
| 409 | A key pair with that name already exists |
| 422 | Not a usable public key — the detail names the actual problem (a pasted *private* key is called out specifically) |

### `POST /keypairs/generate` → 201

```json
{"name": "deploy-key"}
```

Creates an ed25519 pair on the backend, private half written 0600. The response
carries the public key and the private key's **path**. Download-once semantics
would need somewhere to hold the secret until collection and some notion of who
is collecting — a decision for the phase that introduces authentication.

| Code | Meaning |
|---|---|
| 409 | A key pair with that name already exists |
| 503 | `ssh-keygen` is missing or failed |

### `DELETE /keypairs/{id}` → 204

Allowed even when instances were launched with it. **The key is already written
into those guests' `authorized_keys`; deleting the row removes our record, not
the access.** Instance associations survive, marked `deleted`, so the detail
view can still say what was installed. A generated private key file is left on
disk — it may be the only way into a running VM.

| Code | Meaning |
|---|---|
| 409 | The orchestrator key cannot be deleted |
| 404 | No such key pair |

---

## Snapshots

qcow2 *internal* snapshots of an instance's disk, taken **while the instance is
stopped**. That constraint is measured, not conservative: WHPX refuses to save
VM state at all, and the one live mechanism that succeeds records zero bytes of
it. See [DECISIONS #15](DECISIONS.md).

### `POST /instances/{id}/snapshots` → 202

```json
{"name": "before-upgrade", "description": "clean install"}
```

Runs in the background — the work scales with how far the overlay has diverged.
Poll the list for `status`.

```json
{"id": "…", "instance_id": "…", "name": "before-upgrade", "description": "clean install",
 "size_bytes": 0, "status": "Creating", "error_message": null, "created_at": "…"}
```

`status` is `Creating` → `Available` | `Error`, or `Deleting`. `size_bytes` is
the *VM state* size, which is 0 for a stopped-instance snapshot: the disk data
it preserves shows up as the overlay growing, not as a figure the hypervisor
attributes to the snapshot.

| Code | Meaning |
|---|---|
| 409 | The instance is not Stopped — the detail explains why that is required |
| 409 | A snapshot of that name already exists on this instance |
| 409 | The engine does not support snapshots |
| 422 | A name that would confuse `qemu-img` (leading `-`, newlines, tabs) |

### `GET /instances/{id}/snapshots` → 200

### `POST /instances/{id}/snapshots/{sid}/restore` → 200

Returns the disk to that snapshot, **discarding everything written since**.
Synchronous: applying a qcow2 snapshot rewrites metadata rather than copying
data.

| Code | Meaning |
|---|---|
| 409 | The instance is not Stopped, or the snapshot is not Available |
| 502 | The hypervisor refused |

### `DELETE /instances/{id}/snapshots/{sid}` → 202

Backgrounded; the row disappears once the snapshot is gone. Terminating an
instance deletes its snapshots with it — they live inside the overlay.

---

## Projects

A project groups instances, images and key pairs so a dashboard holding three
clients' work is not one flat list.

> **A project is not a security boundary.** There is no authentication in this
> system, so there is nothing to isolate from: any caller that can reach the
> port can list every project, read every resource and act on all of it.
> `project_id` on a list endpoint filters a **view**. It grants and withholds
> nothing, and no client should present it as though it does.

**Instance names are unique across all projects**, because the name is the
hypervisor's identity — the on-disk directory, the QEMU process label, the
cloud-init `instance-id` and the guest hostname. Two projects each holding a
`web` would share one disk. See [DECISIONS #20](DECISIONS.md) for the
measurements.

A `default` project is seeded on first run, and every resource created before
projects existed was migrated into it. Omitting `project_id` when creating
anything files it there, which is exactly where it would have gone before.

### `GET /projects` → 200

Default first, then oldest to newest. Each row carries counts of what is filed
under it; `instance_count` excludes Terminated rows, which are audit history.

```json
[
  {
    "id": "6d68…", "name": "default", "description": null,
    "is_default": true, "created_at": "2026-08-12T21:04:10Z",
    "instance_count": 0, "image_count": 1, "keypair_count": 2
  }
]
```

### `POST /projects` → 201

`{"name": "client-a", "description": "billable work"}`. **409** on a duplicate
name.

### `PATCH /projects/{id}` → 200

Rename or re-describe; both fields optional. **409** on a duplicate name.
`is_default` is not settable — moving it would strand the resources that fall
back to it.

### `DELETE /projects/{id}` → 204

Deletes the label, never what it held.

- **409** while live instances are filed under it, naming them. Terminate or
  move them first.
- Otherwise its images, key pairs and terminated instance rows are **moved to
  the default project**.
- **409** on the default project itself.

### Filtering and creating

Every list endpoint takes an optional `project_id`:

```
GET /instances?project_id=cbd5…
GET /images?project_id=cbd5…
GET /keypairs?project_id=cbd5…
```

Omitting it lists everything. The orchestrator key pair is returned by every
filtered `/keypairs` query regardless of its project — it is the default key
for launches in every project, so hiding it would offer a launch with no way
into the guest. It is the only exemption.

`POST /instances`, `POST /images/import`, `POST /keypairs/import` and
`POST /keypairs/generate` accept `project_id`; an unknown one is a **422**.

---

## Networks and port forwards

Every instance is on QEMU's **user-mode NAT**: the guest gets outbound access
through the host and has no address of its own, so **a forwarded host port is
the only way in**.

Host-only and bridged are modelled but not selectable. That is a measured
limitation, not a to-do — see [DECISIONS #24](DECISIONS.md):

| Mode | Why not | What it would need |
|---|---|---|
| **bridged** | The tap backend has nothing to open | **Windows:** Administrator, to install the tap-windows6 kernel driver. **Linux:** root or `CAP_NET_ADMIN`, plus a pre-made bridge and a setuid `qemu-bridge-helper` with an `/etc/qemu/bridge.conf` ACL |
| **host-only** | QEMU's multicast socket backend fails on Windows (`can't bind ip=230.0.0.1`); the working alternative makes one VM the switch for the rest | A switch process this project does not have |

### `GET /networks` → 200

Read-only. With one mode available a second network would behave identically to
the first.

### `GET /networks/modes` → 200

What ships and what each deferred mode requires, per platform. Served rather
than hard-coded in clients so the API, dashboard and docs cannot drift.

```json
{
  "available": [{"mode": "user", "label": "User-mode NAT", "summary": "…"}],
  "deferred": [{"mode": "bridged", "label": "Bridged", "blocker": "…", "requires": "…"}]
}
```

### `GET /instances/{id}/forwards` → 200

Everything that reaches this guest, SSH first.

```json
[
  {"id": "ssh", "host_port": 2200, "guest_port": 22, "protocol": "tcp",
   "derived": true, "description": "SSH — created with the instance and pinned…"},
  {"id": "9f2c…", "host_port": 18080, "guest_port": 80, "protocol": "tcp",
   "derived": false, "description": null}
]
```

**`derived: true` marks the SSH forward.** It is not stored in the forwards
table — it comes from the instance's pinned `ssh_port` — and it **cannot be
deleted** (409). Clients must render it read-only. It is listed anyway so the
answer to "what is exposed?" is complete.

### `POST /instances/{id}/forwards` → 201

`{"host_port": 18080, "guest_port": 80, "protocol": "tcp"}`

**Applied immediately, running or not.** On a running instance QEMU starts
listening before the request returns; on a stopped one it is replayed at launch.

**Every forward binds to `127.0.0.1`.** This is a local IaaS, so a guest's ports
have no business being reachable from the LAN — a forwarded service answers on
this machine only, not from another device on the network.

Collisions are refused with **422** naming what holds the port — this
instance's SSH port, another instance's pinned port, an existing forward (named
by instance), or one of the pools this system allocates from:

```
Host port 2250 is inside the SSH port pool (2200-2299), which this system
allocates from when it launches instances. Forwarding it would make some
future launch fail. Pick a port outside every pool.
```

**409** if the instance is Terminated.

### `DELETE /instances/{id}/forwards/{forward_id}` → 204

**409** for `forward_id=ssh`, with an explanation. Terminating an instance drops
its forwards — the VM's SLIRP stack is gone, so the ports are already free.

---

## Volumes

Additional disks. A volume is a blank qcow2 that **outlives the instances it is
attached to** — terminating an instance detaches its volumes and leaves them
`Available`, data intact.

> **Attach and detach require a stopped instance.** Not for tidiness: this
> hypervisor's `device_del` removes a disk without waiting for the guest. Tested
> against a mounted filesystem it reported success, took the device away in
> under a second, and left the guest with `Input/output error` and an aborted
> ext4 journal. See [DECISIONS #22](DECISIONS.md).

**A new volume is unformatted.** Nothing here partitions, formats or mounts it.
In the guest:

```bash
lsblk                        # confirm the device name
sudo mkfs.ext4 /dev/vdb      # ONCE per volume — this erases it
sudo mkdir -p /mnt/data && sudo mount /dev/vdb /mnt/data
```

For anything you care about, mount by **UUID**, not by device path: `blkid /dev/vdb` gives the UUID, and a `/etc/fstab` entry written as `UUID=… /mnt/data ext4 defaults 0 2` survives a volume being detached, reordered, or moved to another instance. Device order is stable here, but the path is a position and the UUID is the disk.

**Snapshots do not include volumes.** A qcow2 snapshot lives inside the
instance's own overlay, so restoring rolls the OS back while an attached
volume's data moves on. The snapshot dialog says so, naming the volumes.

```json
{
  "id": "94f3…", "name": "data-one", "size_gb": 1, "format": "qcow2",
  "path": "C:\Users\you\.local-iaas\qemu\volumes\94f3….qcow2",
  "status": "Attached", "error_message": null,
  "attached_instance_id": "8d7f…", "attached_instance_name": "dbhost",
  "attached_instance_guest_os": "linux",
  "attach_order": 0, "device_hint": "vdb", "windows_disk_hint": "Disk 1",
  "project_id": "6d68…", "created_at": "2026-08-12T23:49:00Z"
}
```

`status` is `Creating` → `Available` → `Attached`, or `Error`.

`attach_order` is 0-based and decides both the position of the `-drive`
argument and therefore the guest's device name. Detaching renumbers the rest so
the hints keep matching.

Two hints derive from it, because one name does not fit every guest:

| Field | For | Value |
|---|---|---|
| `device_hint` | Linux guests | `vdb`, `vdc`, … — the root disk is `vda` |
| `windows_disk_hint` | Windows guests | `Disk 1`, `Disk 2`, … as Disk Management numbers them — the root disk is Disk 0 |

Both are always present; `attached_instance_guest_os` says which one to show.
That field exists because a client that rendered `/dev/vdb` at a Windows user
would be naming something that guest does not have, and they will go looking
for it. All three are null while the volume is detached — it has no guest yet,
and guessing what one would call it is inventing.

Both are **hints**: the guest names its own devices, so `lsblk` (or Disk
Management) is the authority.

Volume names are unique **per project**, unlike instance names — the file is
named by uuid, so nothing outside the database depends on the name.

### `POST /volumes` → 202

`{"name": "data", "size_gb": 10, "project_id": null}`. Allocates in the
background; poll for `Available`.

| Code | Meaning |
|---|---|
| 409 | A volume with that name already exists in the project |
| 422 | Size exceeds free disk, or is not positive. The message carries the arithmetic |

### `GET /volumes` → 200

Optional `project_id` and `instance_id` filters.

### `GET /volumes/{id}` → 200

### `POST /volumes/{id}/attach` → 200

`{"instance_id": "…"}`. Takes effect on the instance's **next start**.

| Code | Meaning |
|---|---|
| 409 | The instance is not Stopped, or the volume is already attached, or it is not Available |
| 404 | Unknown volume or instance |

### `POST /volumes/{id}/detach` → 200

The volume keeps its data and becomes `Available`. **409** if the instance is
not stopped, or the volume is not attached.

### `DELETE /volumes/{id}` → 204

Deletes the volume **and its file**. **409** while attached — the only
operation here that destroys data, and the only one that cannot be reached
without a deliberate detach first.

---

## Events

An append-only log of what the system has done. Every other table holds current
state: `instances.updated_at` is one field, so a stop followed by a start leaves
no trace of the stop, and a second error overwrites the first. Restore was worse
— it rewrites a disk and changes no row at all.

There is no endpoint that writes an event. An event describes something this
system did, and one that could be posted from outside would describe something
it might not have.

```json
{
  "id": "0f3a…",
  "instance_id": "8d7f…",
  "instance_name": "web-01",
  "occurred_at": "2026-08-12T17:50:45.123456",
  "kind": "reconciled",
  "actor": "reconciler",
  "summary": "Corrected to Stopped (record said Running)",
  "detail": "status: Running -> Stopped\npid: 53444 -> none"
}
```

`kind` is a closed enum: `created`, `provisioning_started`,
`provisioning_succeeded`, `provisioning_failed`, `started`, `stopped`,
`terminated`, `force_terminated`, `errored`, `snapshot_created`,
`snapshot_restored`, `snapshot_deleted`, `reconciled`, `image_import`.

`actor` is `api`, `reconciler` or `system`. It is **not** a user field —
there is no authentication, and a column that could only ever say "admin"
would imply an accountability this system does not have. `reconciler` means
nobody asked: the background pass found the hypervisor disagreeing with the
record and corrected it.

`instance_id` is null for events that belong to no single instance (an image
import). Those appear in the global feed only.

### `GET /events` → 200

The global feed, newest first.

| Query | Meaning |
|---|---|
| `instance_id` | Only this instance's events |
| `kind` | Only this kind |
| `limit` | 1–500, default 50 |
| `before` | Only events strictly older than this UTC timestamp |

Pagination is keyset, not offset: pass the last row's `occurred_at` back as
`before`. The feed grows at the head, so an offset page 2 would shift under the
reader between requests and show the same row twice.

### `GET /instances/{id}/events` → 200

One instance's history, same parameters minus `instance_id`. **404** on an
unknown instance rather than an empty list, so a typo is distinguishable from
an instance that has no events. Terminated instances still answer — their row
is retained, and their history with it.

### Retention

Events are pruned once at startup, by age alone: `IAAS_EVENT_RETENTION_DAYS`,
default 90, `0` to keep everything. A **terminated instance keeps every one of
its events until they age out** — the moment an instance is destroyed is the
moment its history is most likely to be wanted, so tying retention to the
instance's lifetime would delete exactly the wrong rows.

Nothing back-dates events. An instance that existed before this version has an
empty history rather than a reconstructed one.

---

## Images

### `POST /images/import` → 202

Registers a disk image that already exists on the **backend's** filesystem. Not
an upload: images run to gigabytes, and the backend shares a machine with the
user. The file is copied into the image store, so the source may be moved or
deleted afterwards.

```json
{"name": "My Ubuntu image", "path": "C:\\images\\disk.qcow2", "has_cloud_init": true}
```

`has_cloud_init` decides the access story for instances built from it: false
means no SSH key can be injected, so they are console-only.

Probing (`qemu-img info --output=json`) happens in the background job; status
moves `Importing` → `Available` or `Error`.

| Code | Meaning |
|---|---|
| 409 | An image with that name already exists |
| 422 | The path does not exist, is not a file, or is unreadable by the backend |

A file `qemu-img` cannot parse does not fail the request — it becomes an `Error`
row carrying qemu-img's own message.

### `GET /images` → 200 · `GET /images/{id}` → 200

```json
{"id": "…", "name": "Ubuntu 24.04 LTS (cloud)", "filename": "noble-server-cloudimg-amd64.img",
 "format": "qcow2", "source": "builtin", "virtual_size_bytes": 3758096384,
 "actual_size_bytes": 624239616, "has_cloud_init": true, "status": "Available",
 "error_message": null, "created_at": "…"}
```

`source` is `builtin` or `imported`. The built-in Ubuntu image is registered at
startup.

### `DELETE /images/{id}` → 204

| Code | Meaning |
|---|---|
| 404 | No such image |
| 409 | The built-in image cannot be deleted |
| 409 | In use by non-Terminated instances — detail names them. Removing a backing file under a live overlay corrupts that instance's disk irrecoverably |

---

## Common workflows

### Launch and poll to Running

```bash
ID=$(curl -sX POST http://localhost:8000/instances \
  -H 'Content-Type: application/json' \
  -d '{"name":"web-one","preset":"small"}' | jq -r .id)

until [ "$(curl -s http://localhost:8000/instances/$ID | jq -r .status)" = "Running" ]; do
  sleep 3
done
curl -s http://localhost:8000/instances/$ID | jq '{ip_address, ssh_port, ssh_user}'
```

```powershell
$i = Invoke-RestMethod -Method Post http://localhost:8000/instances `
     -ContentType application/json `
     -Body '{"name":"web-one","preset":"small"}'

do {
  Start-Sleep 3
  $r = Invoke-RestMethod "http://localhost:8000/instances/$($i.id)"
} until ($r.status -in 'Running','Error')
$r | Select-Object status, ip_address, ssh_port, ssh_user
```

Then SSH in — note the key and the port:

```
ssh -i "$HOME\.local-iaas\keys\id_ed25519" -p <ssh_port> iaas@127.0.0.1
```

### Launch with custom sizing

```bash
curl -sX POST http://localhost:8000/instances -H 'Content-Type: application/json' \
  -d '{"name":"big-one","cpus":4,"memory_mb":8192,"disk_gb":40}'
```

Check the ceiling first with `GET /host/capacity`; an over-capacity request is
refused with the arithmetic.

### Launch from an ISO

```bash
# 1. Put the .iso in ~/.local-iaas/isos/, then list what the backend sees:
curl -s http://localhost:8000/isos | jq -r '.[].name'

# 2. Launch. No cloud-init, no SSH key — the console is the way in.
curl -sX POST http://localhost:8000/instances -H 'Content-Type: application/json' \
  -d '{"name":"alpine","iso":"alpine-virt-3.21.7-x86_64.iso","disk_gb":8}'
```

Then open the console from the dashboard. `ip_address` stays null by design.

### Import an image and launch from it

```bash
IMG=$(curl -sX POST http://localhost:8000/images/import \
  -H 'Content-Type: application/json' \
  -d '{"name":"My image","path":"/srv/images/mine.qcow2","has_cloud_init":true}' | jq -r .id)

# Wait for the background copy + probe.
until [ "$(curl -s http://localhost:8000/images/$IMG | jq -r .status)" = "Available" ]; do
  sleep 2
done

curl -sX POST http://localhost:8000/instances -H 'Content-Type: application/json' \
  -d "{\"name\":\"from-mine\",\"image_id\":\"$IMG\"}"
```

### Force-terminate a stuck instance

`DELETE` works from any state, including `Error`, and including rows whose engine
no longer exists:

```bash
curl -sX DELETE http://localhost:8000/instances/$ID | jq -r .status   # Terminated
```

If the hypervisor itself is wedged the call returns 502 and the row is marked
`Error`; retry once the hypervisor recovers.

### A console that renders on a cloud image

Cloud images sit in VGA text mode and show nothing under hardware acceleration.
Ask for the virtio display:

```bash
curl -sX POST http://localhost:8000/instances -H 'Content-Type: application/json' \
  -d '{"name":"visible","display":"virtio"}'
```
