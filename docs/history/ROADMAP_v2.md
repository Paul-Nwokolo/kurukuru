# ROADMAP v2 — Local IaaS Orchestrator (Path C pivot)

Strategic decision (recorded): evolve from local dev tool toward a
standalone virtualization product. Engine strategy = Path C: direct QEMU
control via QMP, one cross-platform engine, hardware-accelerated per host
(WHPX on Windows, HVF on macOS, KVM on Linux).

**Multipass is retired (Phase 7).** It was kept as a secondary engine during
the transition; that transition is complete. Reasons, in order of weight:

1. **No capability overlap with where the product is going.** Phase 6 shipped
   the web console, ISO boot and image import. Multipass can offer none of
   them: it exposes no framebuffer, cannot boot arbitrary media, and manages
   its own image catalog. Every feature after Phase 5 would have been
   "QEMU-only" with a disabled affordance and an explanatory tooltip.
2. **Operational unreliability on this host.** `multipassd` wedged three
   times during Phases 5-6, each with the same signature — `Start-VM`
   succeeds, "Waiting for SSH to be up", then a Qt event-loop warning
   (`QObject::startTimer: Timers cannot be started from another thread`) and
   permanent silence. Recovery needs an elevated service restart. An engine
   that needs manual intervention to unstick is not a foundation.
3. **Cost of carrying it.** Two engines meant per-engine dispatch in every
   route, doubled fakes in the tests, and UI branches for capabilities only
   one side had.

**The abstraction is retained.** `ComputeEngine` (the ABC), `LaunchOptions`,
`EngineRegistry` and the per-row `engine` dispatch all stay. Removing
Multipass touched exactly two things — its driver module and one factory
entry — which is the evidence the seam works. A Hyper-V, HVF or KVM driver
slots in the same way, without changes to routers, models or the reconciler.

Historical `engine="multipass"` rows are **not rewritten**: they still render
with their badge, are skipped by the reconciler (no driver to consult, so
their last known state stands), and can be terminated but not controlled.

Product identity: a LOCAL CLOUD PLATFORM (API-first, images, flavors,
cloud-init, reconciliation, EC2-style dashboard) that gains Type-2
hypervisor depth (arbitrary guests, console, networking) — not a
feature-for-feature VirtualBox clone.

Status: Phases 1-6 DONE. Phase 7 in progress — Multipass retired; QEMU is
the only engine. The ComputeEngine ABC was the pivot point and it held:
everything above it survived both the addition and the removal of an engine.

## Phase 5 — QemuEngine Proof of Concept  [DONE]
Boot an official Ubuntu cloud image (qcow2) through the EXISTING
POST /instances flow using a new QemuEngine side-by-side with Multipass.
- qcow2 overlay disks over a shared base image (copy-on-write)
- NoCloud cloud-init seed ISO (reuses the Phase 4 generator)
- QMP control socket per VM; user-mode networking with SSH port-forward
- Instance.engine column; reconciler dispatches per engine
- VNC server flag plumbed (port recorded, no UI yet)
Achieved: SSH into a QEMU-provisioned VM via the dashboard's Copy SSH.

## Phase 6 — Console & Real Guests  [DONE]
The visible "this rivals a desktop hypervisor" phase.
- noVNC console in the dashboard, bridged in FastAPI (no websockify process)
- Boot arbitrary ISOs (blank disk, boot order dc, console-only access)
- Image import: user-supplied qcow2/raw/vmdk/vdi registered in the catalog
- Deferred: graceful reboot via QMP + guest agent detection
- Constraint found: QEMU renders no display under WHPX on this host, so
  console-critical VMs auto-select TCG. See the console notes in the README.

## Phase 7 — Single Engine & Image Service  <- CURRENT
- [DONE] Multipass retired; QEMU is the only engine (see the rationale above)
- "Create image from instance" = qemu-img snapshot/commit or re-basing an
  overlay into a new backing image (near-instant, copy-on-write, no disk
  duplication)
- Snapshot trees per instance (qemu-img snapshot)

## Phase 8 — Networking, Packaging, White-label
- Networking modes: user (default), bridged, host-only; per-instance choice
- Resource metrics via QMP/guest agent in the dashboard
- Packaging: backend as a service + built frontend, single installer;
  bundle QEMU (process-boundary licensing posture, as UTM/Podman do)
- Branding pass; auth; the deferred hardening list from ROADMAP v1

## Parked / exploratory
- USB passthrough (platform-dependent, revisit after Phase 6)
- macOS/Linux host support pass (engine flags per accelerator)
- Windows guests (works in QEMU; needs virtio driver ISO story)

## Superseded
ROADMAP.md (v1) and PHASE5_BRIEF.md (Multipass golden images) are
superseded by this file and PHASE5_QEMU_BRIEF.md. Do not implement the old
Phase 5. Anything in the earlier briefs that assumes a Multipass engine is
historical — that engine no longer exists.
