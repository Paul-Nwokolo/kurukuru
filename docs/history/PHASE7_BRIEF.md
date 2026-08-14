# PHASE 7 BRIEF — Custom Resources & Launch Experience

Read ROADMAP_v2.md for context. Single-engine (QEMU) as of the Multipass
retirement. This phase makes resources user-defined and host-aware, and
reframes the launch flow around user intent rather than QEMU mechanics.

Three parts, in order. Part A is the substance; B and C are the UX
consequences of it.

---

## PART A — Custom resources with host capacity detection

### Goal
Users type the CPU/memory/disk they want, bounded by what the host can
actually give. Presets remain as one-click starting points.

### Capacity detection (`app/host_capacity.py`, new)
- Add `psutil` to requirements.txt. Report:
  - cpu_total (logical cores), memory_total_bytes, memory_available_bytes
  - disk_total/free bytes for the filesystem holding qemu_dir
  - accel_available: whpx probe result (reuse the Phase 5 engine probe)
- Compute ALLOCATABLE, not raw totals — this is the important part:
  - host_reserve_memory_bytes (setting, default 2 GiB) held back for the OS
  - subtract memory already committed to our own non-Terminated,
    non-Stopped instances (sum of their configured memory)
  - CPU: allow oversubscription up to a factor (setting
    cpu_oversubscribe_factor, default 2.0) since vCPUs timeshare; cap any
    single VM at cpu_total
  - Disk: qcow2 overlays are sparse, so committed size != used size. Bound
    a new disk by current free space, and report both figures.
- `GET /host/capacity` returns totals, allocatable, committed, and the
  per-instance maximums the UI should enforce. Cache briefly (2-3s) so
  dashboard polling doesn't hammer psutil.

### Resource model
- Replace the fixed Flavor enum on the request path with explicit fields on
  InstanceCreate: cpus: int, memory_mb: int, disk_gb: int — plus optional
  preset: str for one-click defaults that simply fill those numbers.
- Keep presets in config.py (small/medium/large as today) as a catalog
  served by the existing GET /flavors — they become defaults, not limits.
- Instance table: store cpus/memory_mb/disk_gb per row (migrate existing
  rows by expanding their flavor via the preset catalog; keep the flavor
  column as a nullable label for history). InstanceRead exposes the numbers
  and the label.
- Validation, server-side and authoritative (the UI must not be the only
  guard): minimums (1 cpu, 512 MB, disk >= backing image virtual size),
  maximums from allocatable capacity. Exceeding capacity -> 422 with a
  message naming the limit and the current figure, e.g. "requested 12288 MB
  but only 9216 MB allocatable (2048 MB host reserve, 4096 MB committed to
  running instances)".
- Disk: qcow2 overlay size must be >= the backing image's virtual size;
  growing is fine, shrinking is not — reject with a clear message.

### Frontend
- Launch modal resource section: preset chips (Small/Medium/Large/Custom).
  Custom reveals three inputs (CPUs, Memory GB, Disk GB) with sliders or
  steppers, each showing its live max from GET /host/capacity and inline
  validation before submit.
- A compact capacity strip: "Host: 16 GB RAM · 9.2 GB allocatable · 6 CPUs"
  with a bar showing committed vs available. Refresh with the poll.
- Presets that exceed current capacity are disabled with a reason tooltip
  rather than hidden.

---

## PART B — Launch flow reframed by intent

### Goal
Replace mechanism-named options ("Cloud image" vs "ISO", "whpx" vs "tcg")
with three outcome-named choices. Same code paths underneath.

### The three modes (first step of the Launch modal)
1. **Quick launch** (default) — built-in Ubuntu cloud image, cloud-init +
   SSH key injected, hardware acceleration, no console.
   Sub-text: "Ready to SSH in about 30 seconds. No setup."
2. **Install from ISO** — pick from GET /isos, blank disk, no cloud-init,
   software acceleration so the console renders, console-first access.
   Sub-text: "Install any OS through the browser console. Slower to run."
3. **Existing disk image** — pick an imported image. If the image has
   has_cloud_init, behave like Quick launch (SSH + hardware accel);
   otherwise like ISO mode (console + software accel).
   Sub-text follows whichever applies, shown after selection.

### Rules
- Accelerator is DERIVED from the mode, never asked as a peer question.
  Keep `accel` on InstanceCreate as an override, but move it behind an
  "Advanced" disclosure with a plain-language warning that hardware
  acceleration disables console output on this host.
- The instance row must show which mode produced it (reuse boot_source /
  console_available / ssh_enabled — no new state if existing fields cover
  it) so access affordances stay honest.
- After launch, the modal's success state tells the user how to get in:
  the SSH command (Quick/cloud-init images) or "Open console" (ISO/no
  cloud-init). This is the moment the access method matters most.

---

## PART C — Cleanups carried from earlier phases

Only after A and B are verified. Each is small and independently testable.
1. Reconciler transaction boundary: the load-decide-write cycle needs a
   consistent boundary generally, not the per-site re-read patched in
   Phase 6. Make it one pattern, tested against a concurrent DELETE.
2. "Degraded" indicator: an instance Running with no IP and ssh_enabled
   for > 90s (setting) renders as Degraded (amber) with an explanation,
   not as healthy green. Does not apply to console-only instances.
3. Code-split the noVNC bundle out of the main chunk (dynamic import on
   the console route).
4. `GET /health` reports engine status: qemu binary present, version,
   accel_available, and the last successful engine call — so a broken
   hypervisor is one glance, not a debugging session.

---

## Cross-cutting
- Migrations additive via the existing init_db mechanism; the real iaas.db
  must converge with all rows intact (test as test_migrations.py does).
- Capacity checks are advisory-but-enforced: never let the UI be the only
  validator, and never hard-fail a launch that the host could actually
  satisfy — if psutil is unavailable, log a warning and fall back to
  permissive limits rather than blocking all launches.
- All suites green; frontend typechecks, lints, builds.

## Report back
Per part: what shipped, live verification, deviations with reasoning. For
Part A specifically: the detected capacity figures on this host and a
demonstration that an over-capacity request is rejected with the explanatory
422 rather than failing at the hypervisor.

## Out of scope (Phase 8+)
Golden images / qcow2 rebase, snapshot trees, networking modes, live
resize of a running VM, per-user quotas, the WHPX display investigation
(tracked separately).
