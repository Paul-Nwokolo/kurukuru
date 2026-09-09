# DOCS BRIEF — Documentation & Consolidation Pass

Not a feature phase. The goal is that a competent engineer who has never
seen this repo can understand what it is, run it, and contribute — and
that the project's own decisions stop living only in chat history.

Read the actual code before writing anything. Where the code and the old
briefs disagree, the CODE is correct — the briefs are historical intent,
not documentation. Several are superseded (PHASE5_BRIEF.md is dead,
ROADMAP.md v1 is superseded by ROADMAP_v2.md).

Write plainly. No marketing voice, no emoji, no "blazing fast". Prefer
short sentences and concrete facts. Assume the reader is a systems person.

## 1. README.md (repo root) — the front door
Currently there is only a Phase 1 backend README, which is badly out of
date. Replace it with a root README covering:
- One-paragraph description: what this is and who it's for. Frame it as a
  local cloud platform (API-first VM lifecycle, images, cloud-init,
  capacity-aware sizing) rather than "a VM manager".
- A screenshot placeholder (`docs/images/dashboard.png`) with a TODO note.
- Feature list, honest: what works today, marked by maturity where
  relevant (e.g. Windows-validated, Linux/macOS untested).
- Requirements: Python 3.11+, Node 18+, QEMU (state the version tested),
  Windows Hypervisor Platform on Windows, disk/RAM guidance.
- Quickstart: exact commands for backend and frontend, both PowerShell and
  bash. Include the .env note (IAAS_ prefix, utf-8-sig, extra="ignore").
- First launch walkthrough: open the dashboard, launch an instance, SSH in
  or open the console.
- Troubleshooting section drawn from real incidents in this project:
  QEMU/binary not on PATH and the IAAS_* override; WHPX not enabled;
  black console on cloud images (the VGA text-mode issue and the Modern
  graphics fix); insufficient memory and how capacity limits report it;
  where logs live.
- Project status and non-goals. State plainly that it is early, single-host,
  and has no authentication — someone must not deploy this on a shared
  network thinking it is secure.
- License placeholder (TODO — not yet chosen) and a note that QEMU is
  invoked as a separate process, not linked.

## 2. docs/ARCHITECTURE.md
- The layering: React -> FastAPI -> ComputeEngine ABC -> QemuEngine ->
  QEMU processes. Explain why the engine seam exists and cite the evidence
  that it works (a Multipass engine was added and later removed with
  changes confined below the seam).
- The state machine: every InstanceStatus, the legal transitions, who
  performs them (routes vs the provisioning background job vs the
  reconciler), and which states are terminal.
- The reconciler: DB as desired state, the three-phase pattern
  (decide -> slow I/O with no transaction open -> read-decide-write in one
  boundary), and why the boundary is where it is.
- Concurrency and invariants worth stating explicitly: name uniqueness is
  application-enforced and scoped to non-Terminated rows; capacity counts
  pending/provisioning instances as committed; hypervisor calls are only
  ever reached from a row fetched by primary key.
- QEMU specifics: per-instance directory layout, qcow2 overlay over a
  backing image, NoCloud seed ISO, QMP over localhost TCP, detached
  process spawning on Windows, port allocation (ssh/vnc/qmp).
- The console path: VNC bound to 127.0.0.1, the FastAPI WebSocket-to-TCP
  bridge, noVNC in the browser, and why nothing parses RFB.
- A short "how to add an engine" section — the practical proof the
  abstraction is real.

## 3. docs/API.md
Generate from the actual routes (the OpenAPI schema is the source of
truth; do not hand-wave). For each endpoint: method, path, purpose,
request shape, response shape, and the meaningful error codes with what
they mean (especially the 409s and the explanatory 422s). Include a short
"common workflows" section with curl/Invoke-RestMethod examples: launch
and poll to Running, launch from ISO, import an image, force-terminate.

## 4. docs/DECISIONS.md
An architecture decision log, one entry each, in the format:
Context / Decision / Consequences. Keep each to a few sentences. Cover at
minimum:
- Why the hypervisor is abstracted behind an ABC
- Why the DB is the source of truth and rows are kept after termination
- Why Multipass was chosen, then retired (three daemon wedges from an
  upstream Qt bug; no console/ISO/image-import capability)
- Why QEMU + QMP over libvirt or the native platform APIs
- WHPX and VGA text mode: what the real limitation is, why std VGA
  remains the default, and why display is a per-instance choice. Include
  the correction — the earlier "WHPX renders nothing" conclusion was
  wrong and came from testing with no guest booted.
- Provisioning is asynchronous (202 + background job) and why
- Name uniqueness moved from a DB index to application logic
- Capacity: allocatable vs total, CPU oversubscription but not memory
- Launch options are framed by intent, with the accelerator derived

## 5. Consolidation
- Move the phase briefs into docs/history/ (they are valuable context, but
  they are not documentation). Add a one-line index noting which are
  superseded.
- Delete or clearly mark PHASE5_BRIEF.md (Multipass golden images) as
  never-implemented, and ROADMAP.md v1 as superseded.
- Fix the stale backend/README.md (either delete it in favour of the root
  README or reduce it to backend-specific dev notes).
- CONTRIBUTING.md: how to run the test suites, the conventions actually in
  use (typed Python, list-arg subprocess with timeouts, additive
  migrations via init_db, tests alongside behaviour changes), and the rule
  that live verification is expected for anything touching the hypervisor.
- Add a .env.example at backend/ with every IAAS_ setting, commented.

## Verification
- Follow your own README quickstart from a clean shell and confirm every
  command works verbatim. Fix anything that does not.
- Confirm the API doc matches the live OpenAPI schema (diff the route list
  programmatically rather than by eye).
- Note anything you found undocumented-and-surprising while writing —
  those are usually real design smells worth logging.

## Out of scope
Marketing copy, a landing page, logo/branding, a chosen license, and any
code changes beyond fixing genuinely broken docstrings you encounter.
