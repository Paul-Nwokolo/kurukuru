# PHASE 10 BRIEF — Key Pairs, User-Data, Snapshots & Instance Detail

Read docs/ARCHITECTURE.md, docs/DECISIONS.md and docs/API.md first. This
phase closes the gaps a VirtualBox or EC2 user would immediately notice,
and gives the UI somewhere to put depth. No auth exists — nothing here may
assume users, tenants, or permissions.

Four parts, in order. A and B are small and unlock disproportionate value;
C is the biggest functional gap in the product; D is where it all lives.
Verify each part live before starting the next.

---

## PART A — Key pairs

### Goal
Stop hardcoding the single orchestrator key. Users bring their own keys,
choose which go on an instance, and can see what's installed.

### Backend
- New `KeyPair` table: id, name (unique), public_key (text),
  fingerprint (SHA256, computed), source ("orchestrator" | "imported" |
  "generated"), has_private_key (bool), private_key_path (nullable),
  created_at.
- Seed the existing orchestrator keypair as a row on first run (idempotent,
  reusing ssh_keys.py — do NOT regenerate or move it; existing instances
  depend on it).
- `GET /keypairs`, `GET /keypairs/{id}`, `DELETE /keypairs/{id}`
  (refuse deleting the orchestrator key -> 409).
- `POST /keypairs/import` {name, public_key} — validate it parses as an
  OpenSSH public key (ssh-rsa/ssh-ed25519/ecdsa-*); reject with a clear
  422 otherwise. Compute the fingerprint ourselves; do not shell out if a
  library can do it, but shelling to ssh-keygen -lf is acceptable —
  same subprocess conventions as everywhere (list args, timeout).
- `POST /keypairs/generate` {name} — generate a new ed25519 pair under the
  key dir, store the private key with 0600 (the Part A Linux fix applies),
  return the public key. The private key is NEVER returned over the API
  after creation — return its path instead. If we ever want download-once
  semantics, that's a later decision; say so in the response docs.
- InstanceCreate gains `keypair_ids: list[str] | None`. Default when
  omitted: the orchestrator key (preserving today's behaviour exactly).
  cloud_init.py already builds ssh_authorized_keys as a list — pass all
  selected public keys. Instance records which keypairs it launched with
  (join table or a JSON column; a join table is cleaner and lets the
  detail view list them).
- Deleting a keypair that instances reference: allow it, but the instance
  rows keep the historical association (the key is already baked into the
  guest — deleting the record does not remove it from the VM). Say this
  plainly in the delete confirmation and in docs.

### Frontend
- New "Key pairs" sidebar item: table (Name, Type, Fingerprint truncated
  with copy, Source badge, Created, Actions: Delete with confirm).
- "Import key" modal (name + paste public key, with a hint about
  `~/.ssh/id_ed25519.pub`) and "Generate key" modal (name, then show the
  resulting private key path with a note that it is not downloadable).
- Launch modal: a key pair multi-select, defaulting to the orchestrator
  key. Show fingerprints. Explain in one line that these keys will be
  able to SSH in as the default user.

---

## PART B — Custom user-data, ISOs and Settings tabs

### Custom cloud-init / user-data
For the IaC audience this may be the single highest-value feature here.
- `InstanceCreate.user_data: str | None` — raw cloud-config supplied by
  the user. When present, MERGE it with the generated payload rather than
  replacing it: our user/keys/package_update remain the base, user keys
  are deep-merged on top, with the user's values winning on conflict.
  Parse their input with yaml.safe_load first and 422 on invalid YAML,
  naming the parse error. Never string-concatenate YAML.
  If merging proves genuinely ambiguous for a key, prefer the user's value
  and note the decision in DECISIONS.md.
- Reject user-data for ISO instances (no cloud-init consumer) -> 422.
- Optional: a `UserDataTemplate` table (id, name, description, content)
  with CRUD so scripts are reusable, plus selection in the launch modal.
  Implement if Part B is otherwise light; skip and note if not.
- Launch modal: an "Advanced / user-data" disclosure with a monospace
  textarea, a YAML-validity indicator, and a link to cloud-init docs.

### ISOs tab
`GET /isos` exists with no UI. Add a simple table (Name, Size, Modified)
and a note explaining where to place ISO files (the iso_dir path, shown
literally and copyable). No upload.

### Settings tab
Replace the "soon" placeholder with a real read-mostly view: instance
store path, ISO dir, key dir, base image and its status, host reserve,
oversubscribe factor, accelerator and version, API version. Anything
genuinely safe to edit at runtime may be editable; everything else is
displayed with the IAAS_ env var name that controls it, so users know how
to change it. Reuse /diagnostics and /host/capacity rather than adding
endpoints where possible.

---

## PART C — Snapshots

The biggest functional gap. Desktop hypervisor users expect this.

### Backend
- New `Snapshot` table: id, instance_id, name, description,
  size_bytes (nullable), status (Creating | Available | Error | Deleting),
  error_message, created_at.
- Use qcow2 INTERNAL snapshots (`qemu-img snapshot -c/-l/-a/-d` on the
  overlay) for stopped instances. For running instances, QMP supports
  savevm-style operations — assess feasibility with the QMP commands this
  QEMU build exposes and choose ONE of:
  (a) stopped-only snapshots (simple, honest, matches "power off to snapshot")
  (b) running snapshots via QMP if reliable on both platforms
  Recommend with evidence rather than attempting both. If (a), the API
  must 409 with a clear message on a running instance.
- Engine ABC gains snapshot methods (create/list/restore/delete) —
  QEMU-specific strings stay inside QemuEngine as always.
- Routes: `POST /instances/{id}/snapshots` (202 + background job for
  create; snapshots of large disks are slow), `GET .../snapshots`,
  `POST .../snapshots/{sid}/restore`, `DELETE .../snapshots/{sid}`.
- Restore semantics: instance must be Stopped -> 409 otherwise. Restoring
  discards changes since the snapshot — the confirm text must say so.
- Deleting an instance deletes its snapshots (they live in the overlay).
- Capacity: snapshots consume real disk inside the overlay. Surface their
  size where known, and make sure the disk accounting in host_capacity
  doesn't now under-report.

### Frontend
Snapshots live in the instance detail view (Part D), not the main table.
Row action "Snapshot" opens a small create modal (name, description).

### CLI
`iaas snapshot create|ls|restore|rm` following the existing conventions
(--json, --wait, exit codes, no prompt when not a TTY).

---

## PART D — Instance detail view

Everything above needs somewhere to live, and one table row is no longer
enough.

- New route `/instances/:id` in the dashboard (add a router — react-router
  is fine to introduce here; keep it minimal).
- Layout: header (name, status badge, primary actions Start/Stop/Console/
  SSH-copy/Terminate), then sections:
  - **Overview**: engine, boot source, image, accel, display, cpus/memory/
    disk, addresses and ports, created/updated, error_message and
    console_caveat when set.
  - **Access**: the SSH command with copy, the key pairs installed on this
    instance, Open console button (with the caveat if present).
  - **Snapshots**: list with create/restore/delete.
  - **User-data**: the cloud-config this instance was launched with,
    read-only, monospace. (Store it on the row so it can be shown.)
  - **Activity**: a simple chronological list of what happened to this
    instance. If no event log exists, derive what you can from status
    transitions and timestamps rather than inventing one — and note in
    the report whether a real event table is warranted (it probably is,
    for a later phase).
- Clicking a row in the main table navigates here. Keep the table's inline
  actions working.
- Loading skeleton, 404 state, and live polling consistent with the table.

---

## Cross-cutting
- Migrations additive via the existing init_db mechanism; the real
  iaas.db must converge with all rows intact, tested as test_migrations.py
  does.
- Every new endpoint documented in docs/API.md; every new decision (the
  user-data merge rule, the snapshot mode choice, key deletion semantics)
  recorded in docs/DECISIONS.md.
- No auth exists: nothing here may imply users, ownership, or permissions.
- All suites green on Windows; frontend typechecks, lints, builds.
- Live verification per part, including at least one full flow that
  combines them: import a key, launch with that key plus custom user-data,
  SSH in with the imported key, snapshot, change something, restore,
  confirm the change is gone.

## Report back
Per part: what shipped, live evidence, deviations with reasoning, and the
final route list. For Part C specifically: which snapshot mode you chose
and the evidence behind it.

## Out of scope (later phases)
Projects/namespacing, volumes, networks, user groups/RBAC, the design
pass, packaging.
