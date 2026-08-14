# PHASE 5 BRIEF (QEMU) — QemuEngine Proof of Concept

SUPERSEDES the old PHASE5_BRIEF.md (Multipass golden images). Do not
implement that file. Read ROADMAP_v2.md for strategic context.

## Context
Phases 1-4 DONE. FastAPI on :8000, React on :5173, MultipassEngine works,
cloud-init generator (`cloud_init.py`, dict + yaml.safe_dump) and SSH key
manager (`ssh_keys.py`) exist. QEMU for Windows is installed at
C:\Program Files\qemu (on System PATH). Windows Hypervisor Platform (WHPX)
is enabled by the user — verify before relying on it (see Acceleration).

## Goal
A second ComputeEngine implementation, `QemuEngine`, running SIDE-BY-SIDE
with MultipassEngine. Success criterion: POST /instances with
{"name": "qemu-test", "flavor": "small", "engine": "qemu"} produces a VM
the user can SSH into from the dashboard's Copy SSH button, and stop/start/
terminate all work — through the SAME routes and UI as Multipass instances.

## Architecture rules (unchanged + extended)
- All QEMU specifics live in `backend/app/engines/qemu_engine.py`. Suggest
  restructuring: `app/engines/` package with `base.py` (the ABC, moved),
  `multipass.py`, `qemu.py`, and a `get_engine(name) -> ComputeEngine`
  factory. Keep import paths working (re-export from compute_engine.py or
  update imports project-wide — your call, keep tests green).
- DB stays desired state; the reconciler must now dispatch per-engine.
- No blocking routes: provisioning stays a background job.

## Data model changes
- Instance.engine: str = "multipass" | "qemu" (default "multipass"),
  included in InstanceRead.
- Instance gains nullable runtime fields used by qemu: ssh_port (int),
  vnc_port (int), qmp_port (int), pid (int). Multipass rows leave them null.
- InstanceCreate gains optional engine field (validated against known
  engines; 422 otherwise).

## QemuEngine design

### Storage layout (new settings, same pydantic pattern)
- qemu_dir: base working dir, default ~/.local-iaas/qemu/
  - base-images/  downloaded cloud images (see Base image)
  - instances/<name>/  disk.qcow2 (overlay), seed.iso, qemu.log
- qemu_system_binary ("qemu-system-x86_64"), qemu_img_binary ("qemu-img")

### Base image (one-time bootstrap)
- Ubuntu 24.04 cloud image (noble-server-cloudimg-amd64.img, qcow2 format)
  from cloud-images.ubuntu.com. Implement a helper that downloads it into
  base-images/ if absent (httpx, stream to disk, log progress). Do the
  download ONCE during your work and note its size in the summary.

### Disk per instance
- qemu-img create -f qcow2 -F qcow2 -b <base> instances/<name>/disk.qcow2 <size>
  (copy-on-write overlay; <size> from flavor, must be >= base virtual size)

### Cloud-init (NoCloud)
- Reuse build payload from cloud_init.py (same user/key/packages). For
  NoCloud you need TWO files in a seed volume labeled "cidata":
  user-data (the #cloud-config) and meta-data (instance-id + local-hostname).
- Build the seed ISO in pure Python with pycdlib (add to requirements.txt)
  — no external genisoimage dependency on Windows. Joliet, volume label
  CIDATA (NoCloud requires the label; commonly uppercase works — verify
  during the live test and adjust).

### Launch (subprocess, list args, never shell=True)
qemu-system-x86_64
  -machine q35 -accel whpx,kernel-irqchip=off -cpu max
  -smp <cpus> -m <memory>
  -drive file=<overlay>,if=virtio,format=qcow2
  -drive file=<seed.iso>,media=cdrom
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:<ssh_port>-:22
  -device virtio-net-pci,netdev=n0
  -qmp tcp:127.0.0.1:<qmp_port>,server,nowait
  -vnc 127.0.0.1:<vnc_display>
  -display none
  -serial file:instances/<name>/qemu.log
- Allocate ssh_port/qmp_port/vnc ports from an ephemeral range (bind-probe
  for free ports; persist chosen ports on the row).
- Windows process handling: spawn DETACHED (creationflags
  DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, closed std handles) so VMs
  survive backend --reload restarts. Record pid. QEMU's -daemonize is
  POSIX-only — do not use it.

### Acceleration
- Probe once at engine init: attempt whpx; if unavailable, fall back to tcg
  with a logged warning and an engine-level flag surfaced in /health or
  engine info (tcg is slow but must not hard-fail the PoC).

### Control (QMP)
- Minimal QMP client (JSON over TCP socket): negotiate capabilities, then
  support: query-status, system_powerdown (graceful stop), quit (destroy).
  Either implement the ~80-line client directly or use the qemu.qmp PyPI
  package — your call; if the package fights Windows event loops, hand-roll.
- stop_instance: system_powerdown, wait for process exit (timeout from
  settings, then SIGKILL-equivalent TerminateProcess fallback).
- start_instance (from Stopped): re-spawn with the SAME persisted ports.
- destroy_instance: quit via QMP if alive (else kill pid), then delete the
  instance dir (overlay + seed). Row -> Terminated, keep audit.
- get_instance_info / reconciler: alive = pid running AND QMP responds;
  map to Running. pid dead -> Stopped (dir exists) or missing -> Terminated.
  ip_address for qemu rows: "127.0.0.1" and use ssh_port.

### SSH surface
- InstanceRead already has ssh_user. For qemu rows the dashboard's Copy SSH
  must produce: ssh -p <ssh_port> iaas@127.0.0.1
  Frontend: use ssh_port when present, else the current format. Show the
  port in the IP column for qemu rows ("127.0.0.1:2231").

## Frontend (minimal for this phase)
- Launch modal: "Engine" selector (Multipass | QEMU (experimental)),
  default Multipass. Instances table: small engine badge per row.
- Copy SSH port-aware as above. Nothing else.

## Testing (mandatory)
1. Unit: overlay command construction, port allocation, NoCloud seed
   contents (mount/parse the ISO back with pycdlib and assert user-data ==
   generated YAML), QMP client against a fake socket server, reconciler
   dispatch per engine. All existing tests stay green.
2. Live end-to-end:
   a. POST engine=qemu -> 202; watch Pending -> Provisioning -> Running.
   b. SSH via the persisted port: whoami == iaas (proves NoCloud worked).
   c. Stop (graceful powerdown), start again, SSH again.
   d. Simultaneously launch a Multipass instance — both listed, both
      correct, reconciler confuses nothing.
   e. Terminate both; instance dir removed; multipass list empty; no
      orphaned qemu-system processes (check tasklist).
3. Report: acceleration mode achieved (whpx or tcg fallback), boot time,
   the exact qemu command line used, and the live transcript.

## Out of scope (parked for Phase 6+)
noVNC UI, ISO boot, image import/catalog, snapshots, bridged networking,
guest agent, multi-base-image support.
