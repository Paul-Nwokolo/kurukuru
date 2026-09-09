# PHASE 11 BRIEF — Event Log, Projects, Volumes & Networks

Read docs/ARCHITECTURE.md, docs/DECISIONS.md and docs/API.md first. This
phase adds the cloud primitives that are still missing and the audit trail
the detail view asked for. No auth exists — nothing here may assume users,
ownership, or permissions.

Four parts, in order. A is small and everything else generates events for
it, so it goes first. Verify each part live before starting the next, and
STOP after each part to report — do not run straight through.

---

## PART A — Event log

Your own Phase 10 assessment made the case: restore is the most
destructive operation in the product and leaves no trace; `updated_at` is
a single mutable field so a stop/start collapses and an Error's cause is
overwritten. Build it.

### Backend
- New `InstanceEvent` table, append-only: id, instance_id (indexed),
  occurred_at (indexed), kind (enum), actor (enum: "api" | "reconciler" |
  "system"), summary (short human line), detail (nullable, longer text
  or JSON string).
- Kinds at minimum: created, provisioning_started, provisioning_succeeded,
  provisioning_failed, started, stopped, terminated, force_terminated,
  errored, snapshot_created, snapshot_restored, snapshot_deleted,
  reconciled (state corrected out of band), image_import — extend as the
  code requires, but keep the enum closed so the UI can render each one.
- Write events from the places that ALREADY mutate status — do not invent
  a second code path. The reconciler is the second writer and matters
  most: an out-of-band correction is the event nobody can currently see,
  so record what it observed and what it changed.
- Never write an event inside a transaction that might roll back, and
  never let an event-write failure fail the operation it describes —
  log and continue. Events are observability, not control flow.
- `GET /instances/{id}/events` (paginated: `limit`, `before`).
  `GET /events` for a global feed, filterable by instance_id and kind.
- Retention: unbounded growth is a real problem for a long-lived local
  install. Add a setting (`event_retention_days`, default 90) and prune
  on startup; document that terminated instances keep their events until
  pruned. State the policy in docs.

### Frontend
- The detail view's Activity section becomes real: chronological list,
  icon per kind, relative time with absolute on hover, expandable detail.
  Remove the "only the most recent state change is recorded" caveat.
- Optional if cheap: a global Activity view in the sidebar.

### CLI
`iaas events [NAME_OR_ID] [--kind] [--limit] [--json]`.

---

## PART B — Projects (namespacing, not tenancy)

### Goal
Group related resources — "client-a", "terraform-testing", "training-lab"
— so the dashboard isn't one flat list. This is organisation, NOT
isolation: there is no auth, so a project boundary must never be described
or implemented as a security control. Say so in the docs explicitly.

### Backend
- New `Project` table: id, name (unique), description, is_default (bool),
  created_at. Seed a "default" project on first run.
- Add nullable `project_id` to Instance, Image, KeyPair, Snapshot (via
  its instance), and any future resource. Migrate existing rows to the
  default project — the additive-migration pattern applies, and the real
  iaas.db must converge with all rows intact.
- CRUD routes. Deleting a project: refuse if it holds non-Terminated
  instances (409, naming them); otherwise move remaining resources to
  default rather than deleting them, and say so in the confirm text.
- Every list endpoint gains an optional `project_id` filter. Creation
  endpoints accept `project_id`, defaulting to the default project.
- Name uniqueness for instances is currently global and scoped to
  non-Terminated rows. DECIDE and record: does it become per-project, or
  stay global? Per-project is more cloud-like and probably right, but it
  interacts with hypervisor-level names (QEMU instance dirs, VM names),
  which are global on disk. If you go per-project, the on-disk name needs
  disambiguating — recommend an approach with evidence before
  implementing, and do not break existing instances.

### Frontend
- Project selector in the header (or sidebar top), persisted in
  localStorage. Selecting one filters instances, images, and key pairs.
  An "All projects" option must exist.
- A Projects management view: list, create, rename, delete with the
  confirm semantics above.
- Launch modal: project defaults to the selected one, overridable.

### CLI
`iaas projects ls|create|rm`, plus a global `--project` flag and an
`IAAS_PROJECT` env var that scope the other commands.

---

## PART C — Volumes (additional disks)

### Goal
The EBS story. No Type-2 hypervisor does this well, and QEMU supports it
directly.

### Backend
- New `Volume` table: id, name (unique per project), size_gb,
  format ("qcow2"), path, status (Creating | Available | Attached | Error),
  attached_instance_id (nullable), device_hint (e.g. "vdb"), project_id,
  created_at.
- `POST /volumes` — create a blank qcow2 of the requested size
  (`qemu-img create`), background job, capacity-checked against free disk
  exactly as instance disks are.
- `POST /volumes/{id}/attach` {instance_id} and `/detach`.
  Attach/detach on a STOPPED instance only for this phase (hot-plug via
  QMP `device_add`/`device_del` is possible but adds failure modes; assess
  and recommend, do not implement unless it is clearly reliable).
  409 with a clear message otherwise.
- Attached volumes become additional `-drive ... if=virtio` arguments at
  launch, persisted in the instance runtime so a restart re-attaches the
  same volumes in the same order. Order stability matters — the guest's
  device names depend on it.
- `DELETE /volumes/{id}` — refuse while attached (409); delete the file
  otherwise. Deleting an instance must NOT delete its attached volumes;
  detach them and leave them Available (this is the AWS behaviour and the
  safe one for data).
- Guest-side note for docs: a new volume is raw and unformatted. The UI
  and docs should tell the user they need to partition/format/mount it
  (`lsblk`, `mkfs.ext4 /dev/vdb`, `mount`) — do not attempt to do this
  for them.

### Frontend
- Volumes tab: table (Name, Size, Status, Attached to, Created, Actions).
  Create modal. Attach/detach with instance picker.
- Instance detail view: a Volumes section listing attached volumes with
  detach, plus attach-existing.

### CLI
`iaas volumes ls|create|attach|detach|rm`.

---

## PART D — Networks

### Goal
Move beyond user-mode NAT with port forwards. This is the part most likely
to hit platform differences — treat it as an investigation with an
implementation, not a pure build.

### Investigate first, report before implementing
QEMU networking modes and what each requires on Windows and Linux:
- **user (SLIRP)** — current default, works everywhere, no LAN presence.
- **bridged (tap)** — VM appears on the LAN with its own IP. On Linux
  needs a tap device and bridge, typically root or CAP_NET_ADMIN. On
  Windows it typically needs a driver/tap adapter and elevation. Assess
  honestly whether this is achievable without elevation; if not, say so
  and describe what the user would have to do.
- **host-only / internal** — VMs can talk to each other and the host but
  not the LAN. Often the most valuable mode for the target audience
  (multi-VM labs) and usually the most achievable.
- **port forwarding on user networking** — already implicitly used for
  SSH; exposing arbitrary forwards is cheap and useful.

Recommend a scope: which modes ship this phase, which are deferred, and
what each requires of the user. Prefer shipping host-only + configurable
port forwards well over shipping bridged badly.

### Then implement the recommended scope
- New `Network` table: id, name, mode, cidr (where applicable), project_id,
  status, created_at. A default "user" network representing today's
  behaviour so existing instances map onto the model.
- Instances gain a network reference and, for user mode, a list of port
  forwards (host_port, guest_port, protocol) — with the SSH forward
  represented in that model rather than special-cased, if that can be done
  without breaking existing rows.
- Capacity/port allocation must not collide with the existing allocator.
- Frontend: Networks tab, network choice in the launch modal, forwards
  editable on a stopped instance, and the instance detail view showing
  the effective network configuration.
- Document clearly what each mode means for reachability, and that
  bridged (if shipped) may require elevation.

---

## Cross-cutting
- Migrations additive via init_db; the real iaas.db converges with all
  rows intact — tested as test_migrations.py does.
- Every new endpoint in docs/API.md; every decision (per-project naming,
  snapshot-of-attached-volume semantics, network scope) in
  docs/DECISIONS.md.
- Projects are NOT a security boundary. Never imply otherwise in UI copy,
  API docs, or error messages.
- Snapshot semantics with volumes attached: a qcow2 snapshot covers the
  instance's own overlay only, NOT attached volumes. This is a real
  footgun — surface it in the snapshot confirm dialog and document it.
- All suites green on Windows; frontend typechecks, lints, builds. Check
  exit codes directly, never through a pipe.
- Live verification per part. Final combined flow: create a project,
  create a volume in it, launch an instance in that project with the
  volume attached and on a non-default network, format and mount the
  volume in the guest, snapshot, restore, and confirm the event log tells
  the whole story.

## Report back
Per part: what shipped, live evidence, deviations with reasoning, the
final route list. For Part B: the naming decision with its rationale. For
Part D: the investigation findings before the implementation.

## Out of scope
Auth, RBAC, user groups, multi-host, the design pass, packaging.
