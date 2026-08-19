# Windows guests — status

**Windows guests do not work yet. No Windows install has ever completed on this
project.** This is a status document, not a walkthrough: there is no successful
install to describe. It records what is established, what is eliminated, what is
merely suspected, and — most importantly — what any measurement here has to do to
be believed.

Last updated at the close of Phase 13.

---

## Where it stands in one paragraph

A Windows instance launches, gets virtual hardware Windows actually has drivers
for, boots its installer, and can be driven through Setup to the point where you
choose a disk. Setup sees the disk with no driver-loading step. Past that, the
install phase has never run to completion. The leading suspect is the VNC console
the engine attaches to every VM, which is measurably ruinous under this host's
accelerator — but the host is bimodal enough that several confident conclusions
have already been drawn and retracted, so read [Measuring anything
here](#measuring-anything-here) before you trust a number.

---

## What works, with evidence

**The device profile.** `guest_os: "windows"` gives the guest an AHCI/SATA root
disk (`ich9-ahci` + `ide-hd`), an Intel `e1000e` NIC and standard VGA — the
devices Windows carries inbox drivers for.

```
-device ich9-ahci,id=ahci
-drive file=…\disk.qcow2,if=none,id=hd0,format=qcow2
-device ide-hd,bus=ahci.0,drive=hd0
-drive file=…\Windows10.iso,if=none,id=cd0,media=cdrom,readonly=on
-device ide-cd,bus=ahci.1,drive=cd0
-device e1000e,netdev=n0
-vga std
```

Validated twice against a real installer, not inferred: Windows Setup's disk page
lists **"Drive 0 Unallocated Space, 40.0 GB"** with no "Load driver" step. That
is the whole point of choosing AHCI over virtio, and it is confirmed.

**Setup boots and can be driven.** From the language screen through Install now,
product key, edition selection, licence, installation type, and on to disk
selection — every screen, verified by a changing framebuffer signature at each
step rather than by assuming a keystroke landed.

**USB HID input.** With `-display none` and no VNC client attached, a QMP
`send-key` never reaches a PS/2 keyboard. Proved with a display-independent
signal: an entire Setup key sequence was sent blind and `disk.qcow2` never moved
off its initial 393,216 bytes, while the same sequence with USB HID attached drove
Setup screen by screen. Windows guests therefore get `qemu-xhci` + `usb-kbd` +
`usb-tablet`. The tablet is not a luxury — PS/2 is a *relative* pointer, so a
guest that never sees a mouse-move origin cannot be clicked accurately. Measured
to be speed-neutral.

**Runtime persistence.** `guest_os` is written to the instance's runtime file, so
a restart re-attaches identical hardware rather than handing a Windows install a
virtio disk it cannot boot from. Clones carry it too.

---

## What does not work

**The install phase never completes.** Setup reaches "Getting files ready for
installation" and then progresses extremely slowly or not at all. The furthest
observed was 2%. Everything downstream is therefore unvalidated: no first boot,
no in-guest networking, no volume attach/initialise/assign-letter, no restart, no
clone of a real Windows guest, and no graceful-ACPI-stop timing.

The only stop timing that exists is the *unresponsive* case — ACPI sent, guest
never answers, 90-second grace expires, process killed — measured at exactly 90 s
twice. That is the failure path, not the graceful path the phase wanted.

**Windows 11 is out of reach and always will be on a Windows host.** It requires
TPM 2.0, and QEMU gates TPM emulation on `host_os != 'windows'` in `meson.build`.
This is a build-time exclusion, not a missing feature, so no version upgrade
changes it. Windows Server and Windows 10 are unaffected.

---

## Eliminated, with evidence

Each of these was a live hypothesis and each is now closed.

| Hypothesis | How it died |
|---|---|
| Wrong virtual hardware | Setup lists the AHCI disk with no driver step; Alpine on identical hardware boots, sees disk and NIC, writes 33.9 MB |
| The accelerator | The same failure reproduces under TCG software emulation, and on a Linux host with QEMU 8.2.2 |
| Missing UEFI | OVMF through a `pflash` pair boots under WHPX on QEMU ≥ 11.1.0, and its NVRAM persists across a power cycle (varstore-file controlled) |
| Hyper-V enlightenments | Hiding the CPUID leaf with `-cpu Westmere,-hypervisor` changes nothing; supplying `hv-relaxed,hv-vapic,hv-spinlocks,hv-time,hv-synic,hv-stimer,…` is strictly worse |
| Host free memory | Retracted. During a 900 s failing run the guest's full 2 GB was **resident** and `\Memory\Pages/sec` was **zero** — nothing pages |
| `hostfwd` / reconciler QMP polling | Both single-variable variants passed on the first try after the base config had failed three times consecutively |

**Media is a genuine but separate finding.** Old Windows 10 media (`bootmgr.efi`
10.0.19041.2965) never reaches Setup; fresh media (3636) does. On the legacy BIOS
path the only differing component is `sources/boot.wim`. Windows Server 2022's
Evaluation Center media is March-2022 vintage with RTM boot binaries and has never
reached Setup; Server 2025 (build 26100.32230, January 2026) reaches the boot logo
and no further.

---

## The leading hypothesis: `-vnc` under WHPX

**This is the one result in the phase that meets the evidence standard below.**
A-B-A at 2 GB, identical command lines, only `-vnc` differing:

| run | config | time to Setup language screen |
|---|---|---|
| A | no `-vnc` | **21 s** |
| B | `-vnc` | never, capped at 240 s |
| A | no `-vnc` | **21 s** |

The failing run is bracketed by successes of the control, so it is not page cache
and not drift. A separate 900-second run with `-vnc` never reached Setup while
holding both vCPUs at ~197%.

**Suspected mechanism, not confirmed.** A VNC display enables dirty page logging
so the server knows which framebuffer regions changed. This project already has
independent evidence that WHPX's dirty-memory tracking is deficient: live
snapshots fail on it with *"State blocked due to non-migratable CPUID feature
support, dirty memory tracking support, and XSAVE/XRSTOR support"*. The cost here
is paid with **zero** VNC clients connected, which matches upstream discussion
that dirty logging could be skipped when no client is attached.

Related upstream, none resolved and none an exact match:

- [qemu#1820 — whpx is slower than tcg](https://gitlab.com/qemu-project/qemu/-/issues/1820)
- [qemu#2063 — poor performance with `-accel whpx`, missing CPUID hypervisor ident data](https://gitlab.com/qemu-project/qemu/-/issues/2063)
- [qemu#3256 — Windows guest idle while host CPU usage is high](https://gitlab.com/qemu-project/qemu/-/issues/3256)

**What is NOT established:** that `-vnc` explains the *install-phase* crawl. The
one install that was watched closely ran **without** `-vnc` and still crawled, so
on current evidence `-vnc` is sufficient to break the boot but is not the known
cause of the install stalling. That pairing was attempted and abandoned — see
below for why.

---

## Measuring anything here

**This host is bimodal.** The same QEMU command line, unchanged, either reaches
the Setup language screen in about **21 seconds** or pegs both vCPUs and reaches
nothing within the cap. Across roughly twenty runs in one session there was no
intermediate outcome.

That property manufactures false confidence in both directions, and Phase 13 lost
three hypotheses to it. One configuration failed **three consecutive times** —
exactly the shape of a deterministic defect — and then two single-variable
variants of it both passed on the first attempt. The 3/3 was a streak.

A contributing factor is the page cache: the install ISO is 4.9 GB, so the first
run after any cache disturbance (recreating a disk, a test sweep, killing a large
VM) is much slower. That explains some of the variance and not all of it.

**The standard for a believable result here:** at least three runs per arm, run
**alternating**, with the failing arm bracketed by successes of the arm it is
compared against. Anything less cannot distinguish a difference from a streak.

`tools/ab_measure.py` exists to enforce exactly that (see `tools/README.md`;
`tools/verify_media.py` next to it is the media checker referenced above). It alternates arms, records
host free memory per run, reports a per-run table and a per-arm distribution, and
**refuses to offer a comparison** from fewer than three runs per arm. It does not
conclude; a human reads the distribution.

```
python tools/ab_measure.py --runs 3 --cap 240 \
    --iso ~/.local-iaas/isos/Windows10.iso \
    --arm "novnc:" --arm "vnc:-vnc,127.0.0.1:30"
```

---

## If `-vnc` is the blocker: the console options

Windows guests have no SSH and no cloud-init, so the console is the only way in.
If the VNC console is what makes them unusable, the feature and its access path
are in direct conflict, and that is a design decision rather than a bug fix.
**None of these has been built, and none should be chosen on evidence this host
can currently produce.**

**1. Screendump-based console.** Drop the VNC display entirely; build the console
from periodic QMP `screendump` frames plus QMP input events. *For:* costs nothing
when idle, needs no VNC display attached, and is effectively prototyped already —
it is how Setup was driven for this document. *Against:* polling rather than RFB,
so a lower frame rate and more backend work; no off-the-shelf client; depends on
the USB HID input that is now in place.

**2. Lazy VNC.** Attach the VNC display only while a console session is open.
*For:* keeps RFB and its clients, pays the cost only when someone is watching.
*Against:* QEMU cannot generally add a VNC display to a running VM, so in practice
this means restarting the guest to open a console — a bad experience during an
install, which is exactly when the console matters most.

**3. A different display device.** `-vga std` is required for Windows Setup, but
other VGA implementations (`cirrus`, plain `-device VGA`) may have different
dirty-tracking costs. *For:* potentially a one-line change. *Against:* entirely
untested — one alternating run would tell you, and it may simply not help.

Rejected already: **TCG** for Windows guests. It was tried, and withdrawn once
longer runs showed it stalling too, further along and still writing nothing to
disk, at roughly 30× the cost.

---

## What would move this forward

The remaining unknowns need a host that is not bimodal. That same missing piece
already blocks the `-cpu host` question and the KVM display question, so three
open questions converge on one piece of hardware — which makes acquiring it a
scoping decision rather than a debugging one.

On such a host, in order:

1. Re-run the `-vnc` A-B-A with `tools/ab_measure.py` to see whether the effect is
   host-specific or general.
2. Pair the **install phase** with and without `-vnc`, which is the measurement
   Phase 13 could not complete.
3. Only then choose a console option, and only then finish the validation
   scorecard: in-guest networking, volume attach/initialise/assign-letter and
   survive-restart, restart, clone, and the graceful ACPI stop timing.
