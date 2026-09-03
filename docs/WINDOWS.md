# Windows guests — status

**No Windows install has completed *through this project's engine* yet, but
both defects standing between here and a finished install are now root-caused,
and both have a fix.** The copy-phase stall that killed every attempt is fixed
(`hpet=off` + `-rtc base=localtime` on an i440fx-family machine). The
remaining blocker — a hang on the guest's first internal reboot — is fully
characterised (QEMU/WHPX's `system_reset` deterministically, 36/36 trials
across every configuration and two separate QEMU builds, fails to bring the
guest back up, while a fresh process against the same disk state always works)
and confirmed as an unreported upstream defect, now filed
(https://gitlab.com/qemu-project/qemu/-/issues/4410). Since no released version fixes it, kurukuru carries
`kurukuru/reboot_watchdog.py`: a Windows-only heuristic that detects a guest
stuck at a static framebuffer and restarts the QEMU process, documented
end-to-end (module docstring, settings, this file) as a workaround for a named
upstream defect, not a general feature — see its own docstring for the full
design and named misfire conditions. Neither defect is a host property; see
below. This is still a status document rather than a walkthrough, because
nobody has yet run a full install end to end with the workaround in place to
confirm it actually reaches a desktop. It records what is established, what is
eliminated, what is merely suspected, and — most importantly — what any
measurement here has to do to be believed.

Last updated after a second host's first Windows-guest session: a correction
that overturned this document's standing assumption (the failure was never the
host, WHPX, or the media — see [It is not the host: VirtualBox completes this
exact install on this exact
machine](#it-is-not-the-host-virtualbox-completes-this-exact-install-on-this-exact-machine)),
the resulting copy-phase fix, and the root cause of the reboot hang (see
[Confirmed: the reboot hang is `system_reset` itself, deterministic, and
independent of everything tested so
far](#confirmed-the-reboot-hang-is-system_reset-itself-deterministic-and-independent-of-everything-tested-so-far)).
Phase 13's original measurements are left as recorded — they are real data —
but any passage that reads as "this host/WHPX has a pathology" is superseded and
should not be taken as this document's conclusion.

---

## Where it stands in one paragraph

A Windows instance launches, gets virtual hardware Windows actually has drivers
for, boots its installer, and can be driven through Setup to the point where you
choose a disk, with no driver-loading step. Past that, this project's original
device profile — `q35`, no RTC/HPET adjustment, no Hyper-V enlightenments —
stalled every attempt somewhere in the copy phase, never past 30%. **That was
never evidence of a broken host.** VirtualBox installs the identical media
successfully on the same machine, through the same Windows Hypervisor Platform
API `-accel whpx` binds to (Hyper-V already owns VT-x there, so VirtualBox falls
back to its own WHP binding, NEM, and completes installs through it routinely).
The fault was this project's QEMU configuration: switching to an i440fx-family
machine and adding `hpet=off` plus `-rtc base=localtime` — nothing else,
`-cpu Westmere` and every device unchanged — takes a real Windows 10 install
through the entire copy phase to "Windows needs to restart to continue" in ~9
minutes, reproducibly, fixed. **What comes after that restart is a second,
separate, fully root-caused defect, and it is also not a host property.** An
in-process QMP `system_reset` — the mechanism a guest-triggered reboot actually
uses — hangs the guest at SeaBIOS's boot prompt in 36 out of 36 trials, across
every chipset/RTC/HPET/Hyper-V-enlightenment/CD-ejection combination tried and
two separate QEMU builds; a fresh QEMU process against the identical disk state
works every time instead. Filed upstream (https://gitlab.com/qemu-project/qemu/-/issues/4410) rather than
carried as a mystery — nothing there is fixed, so kurukuru now detects the
symptom itself and restarts the process rather than letting QEMU reset in
place (`kurukuru/reboot_watchdog.py`), a documented heuristic rather than
another configuration flag. See [the full writeup](#confirmed-the-reboot-hang-is-system_reset-itself-deterministic-and-independent-of-everything-tested-so-far)
for the measurements and the workaround's design. There is also a smaller isolated-failure rate visible
across `ab_measure` boot-phase runs (a 60-100% success band, never streaked)
that remains unexplained; it is *not* established as a host or platform
property either — three separate VirtualBox VMs completed full installs on this
machine with no visible failure across many boot transitions — but it is a
separate, lower-priority question from the two defects above. Read [Measuring
anything here](#measuring-anything-here) before trusting any single number from
that part.

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
    --iso ~/.kurukuru/isos/Windows10.iso \
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

## Second host: qualification, dead media, and how far install actually gets

A second machine (16 GB RAM — same as the first, so not a new variable; C: nearly
full at ~11-15 GB free, D: 543 GB free; QEMU 11.1.0 installed but not on PATH;
Windows 10, Server 2022 and Server 2025 media already on D:). First time this
project has run with `KURUKURU_STATE_DIR`/`KURUKURU_ISO_DIR` off the default
drive, and first time a second host's bimodality has been characterised at all.

**Environment gaps found, both fixed for this session, both worth carrying
forward.** Neither is specific to Windows guests.

- `KURUKURU_STATE_DIR` and `KURUKURU_ISO_DIR` **work correctly** when pointed at a
  different drive — verified against the running backend, not just `Settings()`
  in isolation: database, keys, and WAL files all landed under
  `D:\kurukuru-state`, nothing was created under the default `~/.kurukuru`, and
  `list_isos()` correctly enumerated a `D:\ISOs` directory holding pre-existing
  media. The CLI's independently-duplicated token-path resolver
  (`kurukuru.cli.auth_store.token_path`) agreed. No bug here.
- **QEMU not on PATH silently drops the host to TCG.** `QemuEngine._probe_accel()`
  shells out to `self._system_binary` (default `"qemu-system-x86_64"`, a bare
  name) to test WHPX; when that name isn't resolvable, the probe raises
  `FileNotFoundError`, which is caught and treated the same as "WHPX genuinely
  unavailable" — logged as a warning, never surfaced anywhere a user is likely to
  look (`capacity` just prints `accelerator tcg`, no red flag). On a fresh install
  where QEMU is unzipped somewhere but not added to PATH, every Windows launch
  would silently get the ~30×-slower path that this project has already rejected
  for Windows guests, with no error to explain why. Setting
  `KURUKURU_QEMU_SYSTEM_BINARY`/`KURUKURU_QEMU_IMG_BINARY` to full paths fixes it
  immediately (`capacity` then reports `accelerator whpx`) — the fix already
  exists and is documented in `capabilities.py`'s own consequence text, it just
  isn't reachable from anywhere a user would think to look first.
- **The CLI's `launch` command has no way to request a Windows guest.** The
  dashboard's launch modal sends `guest_os` in its request body; `_launch_payload`
  in `kurukuru/cli/commands_instances.py` never does — there is no `--guest-os`
  flag at all. Every Windows launch in this session went through the HTTP API
  directly (`ApiClient.create_instance` with `guest_os: "windows"` in the
  payload), bypassing the CLI entirely. This blocks the CLI-only workflow the
  rest of the product otherwise supports.

**Host qualification (Decision 40's protocol).** `tools/ab_measure.py`, ≥5
alternating runs per arm, machine idle, `Windows10.iso`:

| comparison | arm | n | reached | notes |
|---|---|---|---|---|
| `-vnc` on/off | novnc | 5 | 4/5 | isolated failure, bracketed by successes |
| | vnc | 5 | 4/5 | isolated failure, bracketed by successes |
| USB HID (`qemu-xhci`+`usb-kbd`+`usb-tablet`) on top of `-vnc` | vnc | 5 | 4/5 | |
| | vnc+usb | 5 | 3/5 | scattered, not streaked |
| memory (2048 vs 4096 MB) | 2048 MB (either arm above) | 10 | 8/10 | |
| | 4096 MB | 10 | 8/10 | one arm had 2 failures in its own 5, other had 0 |

Every arm sits in a 60-80% success band with **isolated** failures (never more
than one in a row for a given arm, always bracketed by successes), a materially
milder and qualitatively different signature than the first host's — which showed
whole runs of only-success or only-failure with no middle ground, and `-vnc`
correlating with total failure. On this host `-vnc` does not correlate with
failure at all, and neither does adding USB HID devices or doubling memory. This
looks like the same underlying property (Decision 40's bimodality) at a much
lower, noisier rate, not a distinct host-specific defect. The raw per-run tables from this session live in `D:\kk-bench-out\*_check*.log`
on the host that produced them — host-local scratch, not committed to the repo.

**Tooling bug found and fixed while building the USB-device arm.**
`tools/ab_measure.py`'s arm spec split on every comma
(`--arm "usb:-device,qemu-xhci,id=xhci"` → three tokens: `-device`, `qemu-xhci`,
`id=xhci`), but QEMU's own `-device` syntax uses commas *inside* a single argv
token. The malformed command line made QEMU exit immediately, which read as
"NOT REACHED" data rather than the parsing bug it was — and there was a checked-in
test asserting the broken split as intentional. Fixed by switching the arm
delimiter to `;` (`tools/ab_measure.py`, `tools/test_ab_measure.py`,
`tools/README.md`); all existing tests updated and passing. Anyone citing an
`ab_measure` result involving a multi-property `-device`/`-drive`/`-netdev` arm
from before this fix should treat it as invalid.

**Server 2025 media reconfirmed dead, on a second host.** The only Server 2025
ISO available (build 26100.32230) was already documented above as reaching "the
boot logo and no further." Four real `guest_os: windows` launches through the
actual product, and 13 further `ab_measure` runs targeting this ISO specifically,
**all** stalled at the identical 117-colour boot-logo frame with the QEMU process
pegged at ~200% CPU — 0/17 reached Setup. This is not new host bimodality; it is
the same dead-media finding Phase 13 already recorded, reproduced exactly. No
amount of retrying fixes it; it needs fresher Server 2025 media.

**Windows 10 (the known-good media) reaches Setup and can be driven all the way to
disk selection, repeatedly, through the real product.** Three separate real
launches (`guest_os: windows`, `iso: Windows10.iso`, the `windows` preset — 2
vCPU/4096 MB/40 GB, `whpx`, `std` display) all reached the Setup language screen,
and all three were driven — via QMP `screendump` plus `input-send-event` for the
`usb-tablet`'s absolute pointer, clicking through the actual Setup UI rather than
guessing at focus order — through language, "Install now", "I don't have a
product key", edition selection (Windows 10 Pro), licence acceptance, "Custom:
Install Windows only", and disk selection, landing on the same **"Drive 0
Unallocated Space, 40.0 GB"** screen Phase 13 documented, with no driver-loading
step. This confirms that finding on a second host.

**New: the install phase can now progress far past Phase 13's 2% ceiling — and
still doesn't finish.** All three of the real Windows 10 launches above were
carried past disk selection into the actual install. Every one reached
"Getting files ready for installation" and climbed at a normal rate (roughly
6-12%/minute) before permanently stalling, CPU pegged at ~190-200% (the same
signature as the boot-phase failures), never recovering after 5+ minutes of
waiting:

| attempt | stalled at | time to stall |
|---|---|---|
| 1 | 30% | ~4 min |
| 2 | 17% | ~2 min |
| 3 | 21% | ~2 min |

Three different stall points rules out a fixed byte offset or a specific file in
`install.wim` as the cause. **This was written up, in the moment, as "the same
host/WHPX pathology, capable of striking at any point in a Windows guest's
lifetime" — that conclusion was wrong, and is corrected in the next section.**
VirtualBox completes an install of this exact media, on this exact machine,
through the same underlying hypervisor API `-accel whpx` uses. So the boot-phase
and install-phase stalls are real and reproducible, but they are not a property
of the host or of WHPX-the-platform; they are a property of *this project's own
QEMU command line* on that platform. "Just retry" not being reliable is still
true — but the fix is a configuration difference to find, not a piece of
hardware to accept as broken. No install has completed **through this project's
engine**. The rest of the Phase 13 scorecard — in-guest networking, volume
attach/initialise/assign-letter/survive-restart, restart, clone, and the graceful
ACPI stop timing — remains unreached as a result, not because the host can't do
it (VirtualBox's guests get that far routinely) but because this project's own
launch configuration hasn't yet been brought in line with one that works.

---

## It is not the host: VirtualBox completes this exact install on this exact machine

Prompted by a direct challenge to the "host pathology" framing above, which was
wrong. **VirtualBox 7.1.4, already installed on the second host, has three
powered-off VMs — `WinSvr2019`, `WinSvr2022`, `WinSvr2025` — each with a
multi-gigabyte disk (WinSvr2025's is 7.5 GB) and a multi-hour runtime in its own
log, i.e. each one completed a real Windows install and ran afterward.** One of
them (`WinSvr2025`) installs from `D:\WinSvr2025.iso`, a different Server 2025
image than the dead `26100.32230` media this project has been testing against —
but that only matters for the media question; it does not explain the boot-phase
and install-phase *hangs*, which happened on Windows 10 media this project has
independently verified is good (fresh, reaches Setup, `bootmgr.efi` 3636 vintage).

**The platform layer is the same one WHPX uses.** `VBoxManage showvminfo` reports
`Effective Paravirt. Prov.: HyperV`, and each VM's `VBox.log` says why:

```
HM: HMR3Init: Attempting fall back to NEM: VT-x is not available
NEM: WHvCapabilityCodeHypervisorPresent is TRUE, so this might work...
```

VirtualBox tried its own native hardware-virtualization engine (HM, direct
VT-x/AMD-V), found VT-x already claimed — by Hyper-V/the Windows Hypervisor
Platform, the same thing that has to be present for `-accel whpx` to work at all
— and fell back to **NEM**, VirtualBox's binding to the identical
`WinHvPlatform.dll`/`WHv*` API that QEMU's WHPX accelerator binds to. This is not
"a similar hypervisor" or "the same class of technology" — on a host where Hyper-V
owns VT-x, it is literally the same platform surface. VirtualBox completed a full
install through it. **That closes the question in [What would move this
forward](#what-would-move-this-forward) about whether VirtualBox running under
Hyper-V would narrow the fault: it does, and it does not survive as a hardware or
platform explanation.**

### Configuration comparison

Everything below is `VBoxManage showvminfo WinSvr2025` (confirmed identical
across all three VBox VMs — this is VirtualBox's stock "Windows" guest-type
template, not something hand-tuned per VM) against
`QemuEngine.build_launch_command` for a `guest_os: windows` instance.

| Aspect | VirtualBox (completes installs) | This project (stalls) | Same? |
|---|---|---|---|
| Platform/hypervisor | NEM (`WinHvPlatform`/`WHv*`), via Hyper-V fallback | `-accel whpx,kernel-irqchip=off` | **Same underlying API** |
| Firmware | BIOS (legacy) | BIOS (legacy) | Same |
| Storage controller | SATA, `IntelAhci` | `ich9-ahci` (AHCI) | Same family, inbox driver either way |
| Chipset | **piix3** (i440fx-family: legacy PIC/PIT) | **q35** (ICH9-family: IOAPIC/PCIe), always, regardless of guest OS | **Different** |
| HPET | **disabled** | not set → q35 default is **on** | **Different** |
| RTC | **local time** (`rtcuseutc=off`) | not set → QEMU default is **UTC** | **Different** |
| Paravirt provider | Default → effective **HyperV** (Hyper-V enlightenment CPUID/MSRs exposed to guest) | none — no `hv-*` CPU properties set anywhere in the engine | **Different** |
| CPU model | `host` (WHP's own CPUID virtualization) | `Westmere` (deliberately old/minimal — `host`/`max` crash WHPX outright, measured) | Different, but independently justified |
| vCPUs | 4 | 2 (`windows` preset) | Different, **untested as a variable** |
| Memory | 2048-4096 MB (varies by VM) | 4096 MB (`windows` preset) | Overlapping; already tested (2 vs 4 GB) with no effect |
| Graphics | VBoxSVGA (VirtualBox's own device, VBE-compatible fallback) | `std` VGA (Bochs/Cirrus-family VBE) | Different, but `std` VGA already renders correctly in this project's own screendumps, so unlikely to be *the* cause |
| NIC | `virtio` + a virtio-win driver ISO attached for injection | `e1000e` (inbox driver, no injection needed) | Different, but the stalls happen mid-file-copy, before networking is exercised |
| Input | USB Tablet + PS/2 keyboard | USB Tablet + USB keyboard (`qemu-xhci`) | Different, unlikely to matter (`ab_measure`'s USB-device test found no effect) |
| Install method | `VBoxManage unattended` (autounattend.xml via a floppy) | Manual click-through Setup, driven here via QMP `screendump`/`input-send-event` | Different, but the stalls happen well after any interactive step |

`grep -rn "rtc\|hpet\|hv-" kurukuru/engines/qemu.py` returns nothing except the
constant name `WINDOWS_WHPX_CPU` — this project's engine has never set an RTC
base, never touched HPET, and never set a single Hyper-V enlightenment CPU
property, for any guest. A quick, non-destructive check (paused boot, `-S`, no
disk, no ISO) confirmed this QEMU build (11.1.0) accepts `hv-relaxed`,
`hv-vapic`, `hv-spinlocks`, `hv-time`, `hv-frequencies` under `-accel whpx`
without erroring — so the flags are at least syntactically live on this build;
whether they *do* anything useful under WHPX, and whether the same flags Phase 13
found "strictly worse" (Decision 39) behave differently on 11.1.0 than on
whatever QEMU build that test ran against (the surrounding decisions reference
10.0.94), is exactly what's untested.

### Leading hypothesis

The four "Different" rows that cluster around **timekeeping and hypervisor
identity** — chipset (which determines the legacy interrupt/timer hardware
model), HPET, RTC, and Hyper-V paravirt enlightenment — are the most mechanically
plausible explanation for a symptom that looks like a true hang but reads as
**pegged CPU with no forward progress**, which is also the signature of a guest
stuck in an inefficient busy-wait rather than a genuinely crashed one. Windows'
HAL chooses between a halt/wait-based idle loop and a hypervisor-assisted one
based on what it detects about its environment; a guest that can't get a
coherent read on its virtual timer hardware or its hypervisor identity is a
believable way to end up spinning a vCPU at ~100% while making no progress,
which is exactly what every stall in this document looks like (CPU pegged,
guest not visibly crashed, screendump frozen).

This is a hypothesis, not a finding — unlike the `-vnc` claim in Decision 39,
nothing above had been isolated by an alternating A-B run yet at the time it was
written. It has since been tested; see below.

### Confirmed: `hpet=off` + `-rtc base=localtime` fixes the copy-phase stall

Tested one variable at a time, boot phase first via `ab_measure`, per Decision
40's protocol, on the machine documented in [Second host: qualification, dead
media, and how far install actually
gets](#second-host-qualification-dead-media-and-how-far-install-actually-gets):

1. **Chipset alone** (`q35` → `pc`/i440fx, everything else unchanged): no boot-
   phase signal — i440fx 5/5, q35 4/5, both inside the established noise band.
   Likely a ceiling effect (boot phase was already 60-100% reliable before any
   change); this does not rule chipset out for the install phase, it just
   couldn't be measured at boot.
2. **RTC + HPET, layered on i440fx** (`-machine pc,hpet=off` + `-rtc
   base=localtime`, chipset held constant): **13/13 (100%) across two sessions**,
   every run landing at an identical, reproducible 36.3-36.4s — versus the
   control's 11/13 (85%) at the usual noisy 20.9s. Thirteen consecutive
   successes with zero variance in timing is the cleanest result this host has
   produced for anything in this document.
3. **The install phase itself, real product device profile, Windows 10 media**
   (i440fx + `hpet=off` + `rtc=localtime` + `-cpu Westmere`, everything else
   identical to `QemuEngine.build_launch_command`'s Windows output — AHCI,
   e1000e, std VGA, `qemu-xhci`/`usb-kbd`/`usb-tablet`, `-vnc` attached): **the
   entire "Getting files ready for installation" phase completed** — 0% → 25%
   (2 min) → 72% (5 min) → 90% (7 min) → "Windows needs to restart to continue"
   at ~9 minutes, CPU never above normal, zero stalling. No prior attempt on
   either host has gotten within 3× of this. Hyper-V enlightenments (the
   originally-planned step 3) were **not** added — this result stands on
   chipset + RTC + HPET alone, and CPU model stayed at `Westmere` throughout
   per the standing instruction not to touch it.

**This is not yet a completed install, and a new, separate stall was found past
it.** After Setup's internal restart, the guest lands at SeaBIOS's `Press ESC
for boot menu.` prompt and, in this run, sat there for 5+ minutes at ~90-97% CPU
on a single core — a different signature from the original pathology (~190-200%
across both cores). Reusing the same mid-install disk (it already had Windows'
files copied, so this needed no repeat of step 3's 9-minute run) and rebooting
it under **plain, unmodified `q35`** — no RTC/HPET change at all — produced the
**same class of hang** at the same screen, one restart cycle later: `q35` passed
its first post-Setup restart cleanly (reached "Getting ready", genuine OOBE
territory, spinner visibly animating, CPU ~36%) and then hung at the identical
SeaBIOS prompt on Windows' *second* automatic restart (after "Getting ready"
completes). So this stall is **not** a side effect of the RTC/HPET fix — both
the fixed and unmodified configurations hit it, at different specific restarts,
each after previously passing at least one restart cleanly.

Two hypotheses were checked and ruled out with single data points before proper
measurement replaced them (both wrong, in the way single trials on this project
are always wrong): that `-boot order=dc` re-probing the CD-ROM on every restart
was the cause (ejecting the CD via QMP before boot-device probing seemed to fix
one instance, then failed to reproduce on the very next attempt), and that
Hyper-V enlightenments would matter here the way they mattered nowhere else so
far. Neither survived contact with an actual alternating-run test.

### Confirmed: the reboot hang is `system_reset` itself, deterministic, and independent of everything tested so far

**The methodological error, caught before it became another wrong conclusion.**
Every "restart" trial up to this point was actually a *fresh QEMU process*
launched against a saved copy of the disk — killing the stuck process and
starting a new one, or a test harness that always spawns a new process per
trial. That is not what happens in reality: kurukuru's engine, and a real
Windows install, never kill the QEMU process across a guest-triggered reboot —
QEMU handles it internally via a hardware reset while the same process keeps
running. A harness measuring cold-process boots against this disk showed 10/10
clean escapes from the SeaBIOS prompt, which looked like progress but wasn't
measuring the failing scenario at all.

**Corrected test: one long-lived QEMU process, boots once, then receives an
in-process QMP `system_reset` — the actual mechanism a guest reboot uses.**
Reusing a template disk frozen at "Windows needs to restart to continue" (a
qcow2 overlay per trial, so the ~9-minute copy phase is paid once, not per
trial), each trial's first boot is expected to succeed (it always does), then
`system_reset` is sent to the same still-running process:

| profile | eject? | n | 1st boot | **survived `system_reset`** |
|---|---|---|---|---|
| i440fx + `hpet=off` + `rtc=localtime` + Hyper-V enlightenments | no | 5 | 5/5 | **0/5** |
| i440fx + `hpet=off` + `rtc=localtime` + Hyper-V enlightenments | yes | 5 | 5/5 | **0/5** |
| plain `q35`, `-cpu Westmere`, nothing else | no | 5 | 5/5 | **0/5** |
| plain `q35`, `-cpu Westmere`, nothing else | yes | 5 | 5/5 | **0/5** |

**20/20. Every single trial, across every configuration tried in this entire
document, hangs at the identical SeaBIOS text-mode screen (2 colours) after an
in-process `system_reset`, and never recovers.** This is not flakiness, not a
host property, not something `-vnc`, USB devices, memory size, chipset,
`hpet`/`rtc`, CD-ROM ejection, or Hyper-V enlightenments have any bearing on —
every one of those was varied across these and earlier trials with zero effect
on this specific outcome. It is a deterministic defect in how QEMU/WHPX's
`system_reset` path reinitialises whatever SeaBIOS needs to proceed past its
boot-device-probe prompt, full stop.

**This also finally explains why a fresh process reliably works where an
in-place reset doesn't**: a new process builds its WHPX partition and every
device from scratch every time, so whatever state `system_reset` fails to tear
down and rebuild correctly simply doesn't exist yet in a fresh process.

### Confirmed upstream: known bug class, no exact match, reproduces on a tagged release

Three related, unresolved QEMU GitLab issues describe WHPX failing on a
guest-triggered reboot — [#2042](https://gitlab.com/qemu-project/qemu/-/issues/2042)
("Not able to reboot Linux guest on Windows host", QEMU 8.1, crashes with
`WHPX: Unexpected VP exit code 4`, reporter says `-smp 1` and
`kernel-irqchip=off` both avoid it) and
[#2402](https://gitlab.com/qemu-project/qemu/-/issues/2402) (WHPX + edk2 UEFI,
Windows 11, same crash on reboot from Setup, cross-references #2042), plus a
smaller related cluster (#1915, #2287, #858). None is fixed; none has a
maintainer response with a root cause.

**Neither reported workaround explains this project's hang, and it reproduces
on an official release, not just this project's dev snapshot.** `kernel-irqchip=off`
is already this project's standing default and does not help. `-smp 1`,
tested directly (6/6 trials, alternating, both eject arms), hangs identically
to `-smp 2` — **26/26 total** at that point. The failure here is also silent
(no `Unexpected VP exit code` message), unlike both linked reports. Tested
further against **QEMU 11.1.1**, an officially tagged release (MSYS2's
`mingw-w64-x86_64-qemu` build, not this project's `v11.1.0-12130-ge470268ff4`
dev snapshot) — **10/10, identical hang** — bringing the total to **36/36**
across two separate QEMU builds. This is not a snapshot-specific regression:
there is no released version to pin to instead of carrying a workaround.

This looked related-but-distinct enough, and cleaner to reproduce than either
linked report (deterministic, no special flags, plain `q35`, either vCPU
count), to be worth its own report rather than a comment on either — **filed
upstream: https://gitlab.com/qemu-project/qemu/-/issues/4410.**

**Given no upstream fix exists at any tested version, kurukuru needed an
engine-side workaround — not a QEMU-config change, and not a version pin.**
Built as `kurukuru/reboot_watchdog.py`: a Windows-only framebuffer-staleness
watchdog that reuses the existing `restart_instance()` respawn path. Its full
design — the threshold's justification, the once-per-window restart guard, the
`AUTO_RESTARTED` event kind, and console-reconnect support — plus its **named
misfire conditions** live in that module's own docstring, which is the
authoritative source; see [What would move this
forward](#what-would-move-this-forward) for the summary and the one-commit
removal checklist.

No engine code was changed until the workaround above — every measurement
before it was a manual QEMU command line built to exactly match
`QemuEngine.build_launch_command`'s Windows output with the tested
substitutions (plus two small purpose-built harnesses, `restart_measure.py`
and `reset_measure.py`, kept beside `qmp_driver.py` in the host-local scratch
directory rather than committed — they're diagnostic one-offs, not general
project tooling like `ab_measure.py`), run outside the product so each result
could be trusted before committing to a code change.

---

## What would move this forward

Phase 13's framing — "the remaining unknowns need a host that is not bimodal" —
is retired, not revised. A host was never the missing piece: VirtualBox
completes this exact install, on this exact machine, through the same platform
layer WHPX uses. Two concrete, deterministic defects have been found and
isolated, not a mysterious host property:

1. **The copy-phase stall — fixed.** `hpet=off` + `-rtc base=localtime` on an
   i440fx-family machine takes a real Windows 10 install through the entire
   copy phase reliably, reproduced across multiple full runs. This is ready to
   carry into `QemuEngine.build_launch_command` for Windows guests once the
   reboot defect below no longer blocks reaching a finished install to validate
   against.
2. **The guest-reboot hang — root-caused, confirmed upstream and unfixed at
   any tested version, workaround built.** QEMU/WHPX's `system_reset`
   deterministically (36/36 across every chipset/RTC/HPET/enlightenment/eject
   combination tried, and across two separate QEMU builds — this project's dev
   snapshot and the officially tagged 11.1.1 release) leaves the guest stuck at
   SeaBIOS's boot prompt; a fresh process against the same disk state always
   works. A related, unresolved bug class exists upstream (#2042, #2402) but
   nothing matched closely enough to be the same report, so this was filed
   separately: **https://gitlab.com/qemu-project/qemu/-/issues/4410.** No release fixes it — there was no
   version to pin to instead of a workaround.

   **Built:** `kurukuru/reboot_watchdog.py` — a Windows-only (`guest_os ==
   "windows"` only; see its docstring on why a static Linux console is a
   normal steady state, not a symptom) framebuffer-staleness watchdog. Samples
   a running Windows guest's screendump every
   `windows_reboot_watchdog_interval_seconds`; a static, ≤8-colour (SeaBIOS
   text-mode) frame held for `windows_reboot_watchdog_stuck_seconds` (300s
   default — roughly 8x the slowest legitimate boot-to-graphical transition
   measured anywhere in this document) triggers `restart_instance()`, the same
   respawn path the manual `/restart` endpoint already uses. At most one
   automatic restart per `windows_reboot_watchdog_cooldown_seconds` (1 hour
   default) — stuck again inside that window marks the instance `Error`
   instead of retrying, so this cannot loop. The restart is its own event kind,
   `AUTO_RESTARTED`, distinct from a user-requested `RESTARTED` and from a
   routine `RECONCILED` correction — a user must be able to tell "the backend
   did this to your VM without being asked" apart from both. The console
   auto-reconnects (`ConsoleModal.tsx`) when a watched instance drops
   mid-session and the backend still reports it Running, up to three attempts,
   rather than leaving a dead viewer with no way back.

   **This is a heuristic, not a fix, and is documented as one everywhere it
   lives** — `reboot_watchdog.py`'s module docstring is the authoritative
   statement of its named misfire conditions (a guest legitimately slow rather
   than stuck; the in-memory tracking state not surviving a backend restart)
   and carries a one-commit removal checklist naming every file this
   workaround touches, for the day the upstream issue is fixed and this
   project's minimum QEMU version moves past it.
3. **Now that the reboot hang has a workaround**, run a full install end to
   end to confirm it actually reaches a desktop, then decide whether the
   copy-phase fix is Windows-specific or worth reconsidering more broadly —
   Linux guests have shown no evidence of either defect and should stay on
   their current profile unless a reason appears.
4. **Fresh Server 2025 media**, since the only copy on hand is confirmed dead
   independent of everything above (VirtualBox's own `WinSvr2025` VM installs
   from a *different* Server 2025 ISO than the dead one this project has) and
   blocks validating this project's most likely real-world target guest.
5. Finish the validation scorecard (network, volumes, restart, clone, ACPI
   stop) — "restart" is no longer a hypothetical scorecard item but the exact
   defect above, so it will be validated as a side effect of confirming the
   workaround holds under a real install.
