# PHASE 19 BRIEF — Narrowing the Linux/KVM questions on nested KVM

Draft. Not started. Read docs/PORTABILITY.md (Phase 18 section, the eight open
questions) and DECISIONS #40, #55, #65–#73 first.

## Why now, and the one rule

Both venues we have expose `/dev/kvm`: WSL2 on the dev machine (`root:kvm`,
the user not yet in the group) and GitHub's Ubuntu runners. Both are **nested**
— KVM running inside another hypervisor (Hyper-V; the cloud provider's). That
is not bare metal: CPUID, timing, interrupt delivery and device emulation all
pass through a second layer.

**Every result this phase produces is labelled "observed on nested KVM
(<venue>)", written next to the open question it narrows, and never recorded
as a bare-metal answer.** No default changes on a nested result — not the
display default, not the reboot watchdog's platform gate, not `-cpu host`.
Those wait for bare metal, the way the WHPX display conclusion should have
waited and did not (Phase 6 concluded the console was blank under WHPX; it was
only VGA text mode, which took a framebuffer diff to establish).

## The eight questions, and what nested KVM can do with each

| # | Question | Nested KVM can… | Why |
|---|---|---|---|
| 1 | Do VMs launch, boot and run under KVM from a pipx install? | **Narrow** — the functional path (KVM selected, the command line accepted, the cloud image boots, SSH answers) | Boot *times* and stability under load are properties of the outer hypervisor too; they say nothing about bare metal |
| 2 | Does `-cpu host` work? | **Narrow** | "host" in a nested guest is the virtual CPU the outer hypervisor presents, not a physical one. Accepted-and-boots is evidence; a bare-metal CPU can still differ |
| 3 | Should the display default differ under KVM? | **Narrow, strongly** | The WHPX failure was a missing dirty-logging API. KVM's dirty-log interface is the same interface in a nested L1, so a rendered framebuffer on `std` is good evidence about the mechanism. One bare-metal confirmation still closes it |
| 4 | **Does qemu#4410 (the `system_reset` hang) reproduce under KVM?** | **Narrow — asymmetrically** (see below) | A reproduction is far more informative than a non-reproduction |
| 5 | Do VMs survive a service restart, and a logout without lingering? | **Settle the restart half; neither for logout** | Restart is systemd + process behaviour (`KillMode=process`, re-adoption), not hypervisor behaviour, and either venue can run it with real QEMU. Logout cannot be tested: WSL stops the whole distribution at the last window (Phase 18), and a CI runner has no login sessions |
| 6 | Console and port forwards | **Settle, for user-mode networking** | The console bridge and `hostfwd` forwards are QEMU's userspace SLIRP and our loopback WebSocket — independent of the accelerator. Bridged networking stays out of scope |
| 7 | Do Linux guests need the forced resolvers? | **Settle on the CI runner; not on WSL2** | SLIRP's DNS is QEMU userspace, but it forwards to the host's resolver, and WSL2's resolver is itself a Windows-side NAT shim — unrepresentative. The runner's is an ordinary Linux resolver |
| 8 | psutil under cgroups | **Neither — not a KVM question** | Settle separately in a container with a memory limit; it does not belong in this phase |

## Question 4, in detail — the one that decides the watchdog

Today the reboot watchdog runs on every host, Linux included (Phase 18, WSL2
journal). Whether it should is the question.

- **If the hang reproduces under nested KVM**, that is strong evidence it is
  not WHPX-specific — a QEMU/guest interaction the watchdog is right to cover on
  Linux too. Record it; still do not change the gate until bare metal agrees,
  but the burden of proof shifts.
- **If it does not reproduce**, that is weak evidence it is WHPX-specific: an
  absent hang under one more layer of virtualization can be timing. It narrows
  the question; it does not license turning the watchdog off on Linux.
- **Protocol:** `tools/reset_measure.py`, alternating runs, at least three each
  way (decision 40 — this host's results are bimodal and fewer runs have misled
  before). Same guest, same media and same build as the WHPX measurements in
  decisions 54–55, so the only variable is the accelerator.
- **What the measurement needs.** Not a Windows install: `reset_measure.py`
  boots the Windows *installer ISO* against a blank disk, issues
  `system_reset`, and checks whether the guest comes back — a 2048 MB guest by
  default, the Windows 10 ISO already on the dev machine, no licence beyond
  the one that ISO came with.

**Where #4 runs, decided before the phase starts:**

1. **First choice: WSL2.** The ISO is already on this machine (copied into
   `~` on ext4 — reading it from `/mnt/c` to copy is the only `/mnt/c` access).
   WSL has 3.8 GiB under the cap; a 2048 MB guest leaves ~1.7 GiB for the WSL
   kernel, systemd and QEMU's own overhead. That is plausible and unmeasured.
   **Step one of the phase is to measure it**: boot the ISO once with `free -h`
   sampled before and during, and abort #4 on WSL if the guest is
   OOM-killed or swaps heavily — not lower the guest's memory, which changes
   the experiment, and not raise the cap, which is a finding to report.
2. **Second choice: a GitHub runner** (16 GB RAM, nested KVM). Blocked on
   media: the runner has no Windows ISO, and fetching Microsoft's evaluation
   ISO from CI on every run is a licensing and stability question that must be
   answered in writing before any workflow downloads it. Not to be attempted
   until it is.
3. **The control: the Windows host under WHPX**, same ISO, same build, same
   tool. Decisions 54–55 measured the hang there, but this host is bimodal
   (decision 40), so the control is **re-run in the same session** as the KVM
   runs, alternating where possible — a stale baseline is not a control.

**If neither nested surface can run it:** #4 is recorded as "not measurable on
the nested venues available — needs bare metal", with the reason (memory on
WSL, media on CI). The phase continues and completes on #1–#3 and #5–#7; it
is not a failed phase. The reboot watchdog stays on for every host, exactly as
today, and #4 heads the list for the bare-metal session.

## Venues and setup

- **WSL2:** add the user to `kvm` (re-login — the trap doctor already
  describes), `sudo apt install qemu-system-x86 qemu-utils`, the pipx install
  from Phase 18. Nested virtualization must be on in WSL; confirm it rather than
  assume it. Everything under `~` on ext4, never `/mnt/c`.
- **GitHub runner:** a separate workflow, `workflow_dispatch` only — never the
  per-push `verify` run. A udev rule or group change for `/dev/kvm`, the
  distribution's QEMU, the wheel via pipx.

## Deliverables

- PORTABILITY's open-questions table: each row gains a "nested result" column
  — what was observed, where, how many runs — and keeps its "what settles it"
  column unchanged.
- No code changes beyond what a measurement needs (e.g. a workflow file).
- A short DECISIONS entry only if a result changes what a bare-metal session
  should prioritise.

## Out of scope

Bare-metal claims of any kind; changing any default; bridged networking;
macOS; anything that needs the `.wslconfig` cap raised (report it instead).
