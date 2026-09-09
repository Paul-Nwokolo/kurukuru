# PHASE 13 BRIEF — Windows Guest Support

Read docs/ARCHITECTURE.md, docs/DECISIONS.md and the #14 audit findings
first. The audit established that Windows guests do not work today: the
root disk is `if=virtio` and the NIC is `virtio-net-pci`, and Windows
Setup has no inbox driver for either — the installer boots to "no drives
found". `GuestOS.WINDOWS` exists in the enum and is refused at the create
route with an explanation. This phase makes it real.

Scope for this phase: **bootable + drivers + docs.** A user supplies a
Windows ISO, launches it, installs through the console, and ends with a
working VM they can reach. Unattended install and automated credential
injection (the Windows equivalent of cloud-init) are explicitly a LATER
phase — do not attempt them here, but do not design anything that would
block them.

Targets: Windows Server and Windows desktop (10/11). Both must be
validated; where they differ, say so rather than generalising.

## Constraints that shape everything

- **Microsoft ISOs cannot be bundled or auto-downloaded.** The user
  supplies their own. The UI must say where to get one and what to do
  with it, in the same plain register as the existing copy.
- **virtio-win CAN be redistributed** (Red Hat, BSD-licensed drivers with
  a signed ISO). Decide and justify: bundle it, auto-download on demand
  like the Ubuntu base image, or require the user to fetch it. My prior
  is auto-download on demand from the official Fedora people mirror,
  reusing `ensure_base_image`'s httpx machinery — but verify the licence
  terms yourself and report what you find rather than taking my word.
- **Two viable driver strategies.** Assess both, recommend one:
  (a) *Compatible devices*: SATA/AHCI disk + e1000e NIC + std VGA.
      Windows installs with inbox drivers, no extra media, works
      immediately. Slower I/O; virtio can be installed afterwards.
  (b) *virtio + driver ISO*: attach virtio-win as a second CD-ROM, user
      loads the storage driver during setup ("Load driver" → browse).
      Better performance, more steps, more ways to fail.
  A third option is (a) for install with a documented path to (b) later.
  Recommend with reasoning; do not build both without saying why.

## Backend

### Guest OS becomes load-bearing
`guest_os` currently exists and is refused. It must now drive device
selection end to end:
- Disk bus: `virtio` (Linux) vs the Windows-appropriate choice from your
  recommendation above. Persisted per instance in the runtime file so a
  restart re-attaches identically — the same rule volumes already follow.
- NIC model: `virtio-net-pci` vs `e1000e`.
- Display: the audit measured std=720x400/1-colour vs virtio=1280x800
  under WHPX for Linux cloud images. Windows Setup has no virtio-gpu
  driver, so std is the only option that shows a picture during install.
  Verify this by measurement (QMP screendump + distinct-colour count),
  the way the earlier display work was settled — do not assume.
- Machine type, RTC and any other flags Windows needs: `-rtc base=localtime`
  is conventional for Windows guests; check whether it matters here and
  say what you found.
- TPM/Secure Boot: Windows 11 nominally requires TPM 2.0 and UEFI. Assess
  honestly — does this QEMU build have swtpm available on Windows? Is
  OVMF/UEFI firmware present? If Windows 11 cannot be installed without
  work beyond this phase, say so plainly and support Server + Windows 10
  rather than shipping a Windows 11 option that fails at the ISO's
  hardware check. Report what you measured.

### No cloud-init for Windows
`cloud_init.build_config()` emits `shell: /bin/bash`, `sudo: ALL=(ALL)
NOPASSWD:ALL` and apt packages. Windows consumes none of it. A Windows
instance must get NO NoCloud seed — the same path ISO instances already
take. `ssh_enabled` is false; the SSH command and key-pair selection must
be hidden or disabled for Windows instances with a one-line explanation,
not silently absent.

### Access story
Windows has no OpenSSH by default (Server 2019+ can add it as a feature;
desktop has it as an optional component). For this phase the access path
is **the console**, exactly as for Linux ISO installs. Do not build an
RDP client. DO consider whether a port forward preset for RDP (3389) is
worth offering once the user has enabled it inside the guest — that is
cheap given the forwards feature already exists, and it is the natural
follow-on. Recommend, do not assume.

### Sizing
Windows needs more than the Linux presets: 2 vCPU / 4 GB / 40 GB is a
realistic floor for desktop, Server somewhat less. Either add Windows
presets or adjust minimums when `guest_os` is windows, and refuse
obviously-doomed sizes with the explanatory-422 style already in use.
Capacity checking applies unchanged.

## Frontend
- Guest OS becomes a first-class choice in the launch modal, and it must
  be chosen BEFORE the options that depend on it, since it inverts the
  correct answers for graphics and disk bus.
- ISO mode for Windows: name where to get the ISO (Microsoft's official
  download pages), what to do with it (place it in the ISO directory —
  reuse the existing path presentation), and what to expect (install
  takes considerably longer than a Linux cloud image; use the console).
- The Graphics control: Windows Setup needs std. If Modern graphics is
  never correct for a Windows guest, do not offer it — the audit's rule
  was to avoid presenting invalid combinations.
- Instance detail and the table must show guest OS clearly. A Windows
  instance with no SSH command should say why, not show a blank.
- Volumes: the cross-platform text already landed. Verify the Windows
  half is correct against a real Windows guest — Disk Management /
  `Get-Disk`, initialise, format, assign a letter.

## Documentation
- README: Windows guests as a supported workload, with the honest
  requirements (user-supplied ISO, longer install, console-based, no
  automated provisioning yet).
- A `docs/WINDOWS.md` walkthrough: obtaining an ISO, launching, the
  install flow including any driver step, enabling RDP inside the guest,
  adding the port forward, and the known limitations.
- DECISIONS entries for the driver strategy, the virtio-win distribution
  choice, and anything the TPM/Secure Boot assessment settles.

## Validation (this phase lives or dies here)
1. **Install Windows Server end to end** through the browser console:
   boot the ISO, installer sees the disk, completes, reboots into the
   OS, has network. Report elapsed time and the exact QEMU command line.
2. **Install Windows desktop (10 or 11 per your TPM findings)** the same
   way. If 11 is out of reach this phase, say so with the measurement.
3. **Network works inside the guest** — resolve a name and fetch a page.
4. **A volume attaches and is usable** — initialise, format, assign a
   letter, write a file, restart the instance, confirm it survives.
5. **Restart and clone** behave correctly for a Windows instance.
6. **Lifecycle**: stop must be graceful (ACPI shutdown reaches Windows),
   not a 90-second grace-period expiry followed by a kill. Measure it —
   the audit's stop path is the one that matters here, since Windows
   dislikes hard power-off.
7. Linux guests must be unchanged. Run the full existing suite and a
   live Linux launch to prove no regression.
8. Both themes, both suites green, exit codes read directly.

## Practical notes
- Windows ISOs are ~5-6 GB and an install wants 30+ GB free. Check
  capacity before starting and tell me if the host cannot accommodate
  it — do not half-run the validation.
- These installs are slow. Budget for it; do not shorten the validation
  to save time. If you run out of context mid-phase, stop at a clean
  point and report rather than rushing item 7.

## Report
The driver-strategy recommendation and its evidence; the TPM/Secure Boot
finding; the two install transcripts with timings; what works, what
does not, and what the automation phase would still need.

## Out of scope
Unattended install (unattend.xml / Cloudbase-Init), automated credential
injection, an RDP client, Windows guest agent integration, Windows-on-ARM.
