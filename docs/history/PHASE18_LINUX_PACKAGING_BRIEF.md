# PHASE 18 BRIEF — Linux packaging (the half that needs no KVM)

Read docs/PORTABILITY.md and docs/DECISIONS.md first. Phase 9 did a
portability audit and a live run on a TCG-only cloud host; this phase
builds the packaging on top of it.

## The constraint, stated up front

This phase runs in WSL2 on the development machine. WSL2 is a real Linux
userland and it is **not** a faithful hypervisor host: nested
virtualization under Hyper-V is unreliable, its networking is NAT'd
behind a virtual adapter, and it has no display path for a VM console.

So anything measured about VM behaviour here is untrustworthy, and must
not be recorded as settled. The division is:

**In scope — WSL2 can answer these honestly**
- The Python package installing cleanly from a wheel on Linux
- State directory layout, XDG compliance, file permissions
- The systemd user service unit: writing, installing, enabling, start,
  stop, restart, logs
- The full test suite on a non-Windows OS
- Anything still Windows-shaped that the Phase 9 audit missed
- CLI behaviour, `doctor` output, error messages that name Windows
  things on a Linux host
- Documentation: install, upgrade, uninstall, where state lives

**Out of scope — needs a bare-metal Linux host with /dev/kvm**
- Whether VMs actually launch, boot and perform
- `-cpu host`, chosen on reasoning in Phase 9 and never executed
- The display default, scoped to WHPX and never tested under KVM
- Whether the `system_reset` defect (qemu#4410) exists under KVM at all,
  which decides whether the reboot watchdog is Windows-host-only
- Console behaviour, port forwards, bridged networking feasibility

Record every out-of-scope item as an open question with what would
settle it. Do not let a WSL2 result stand in for any of them.

## Design decisions to make and justify

**QEMU is not bundled on Linux.** Distros ship it, keep it patched, and
KVM is properly fast. That removes the signing problem and the stale-file
class of bug in one move. So the installer's job is to detect QEMU,
report clearly when it is missing, and name the package to install for
the common distros. Do not download or build QEMU.

**State directory.** Phase 9 recommended XDG (`XDG_DATA_HOME`,
`XDG_CONFIG_HOME`) over a dotfile in `$HOME`, with the mechanism added
and the policy deferred. Decide it now, and handle the case of an
existing `~/.kurukuru` from a source checkout — migrate it the way the
Windows rename migration did, or leave it and document. Recommend with
reasoning; do not silently relocate someone's data.

**Install shape.** The realistic first form is `pipx install kurukuru`
plus a small script that installs a systemd **user** service — not a
system service, for the same reasons the Windows build uses a logon task
rather than a Windows service: it needs no root, it runs as the user who
owns the state directory, and the file-permission model stays intact.
`.deb` and `.rpm` can come later if there is demand. Recommend and say
what you are not building.

**The kvm group.** A user who is not in it gets a permission error on
`/dev/kvm` that looks like a product fault. `doctor` must detect it,
say so plainly, and give the exact command — including that a re-login
is required afterwards.

## Requirements

- The systemd unit must survive logout if lingering is enabled, and say
  so in the docs if it is not. State the default behaviour honestly.
- `doctor` on Linux must not print Windows-shaped advice. Sweep every
  user-facing string for Windows assumptions — the Phase 9 audit found
  these in code; this is the same sweep for copy.
- Uninstall must not delete user data, matching the Windows behaviour
  established in 0.1.3. There is no Recycle Bin on Linux: recommend an
  equivalent (a `trash-cli` fallback, or a prompt that moves the tree
  aside) rather than a silent `rm -rf`.
- Add a Linux job to CI running the backend and tools suites. It will
  have no KVM, so mark anything requiring it as skipped rather than
  passing vacuously, and report what skipped.
- `docs/INSTALL-LINUX.md`: requirements, the QEMU package per distro,
  install, first run, the kvm group, upgrade, uninstall, and an explicit
  statement of what has and has not been verified on real hardware.

## Report
Per area: what shipped, what WSL2 could and could not establish, and the
open-questions list for the bare-metal session. Be explicit that Linux
support is not claimed as working until a KVM host has run the full
scorecard — the README must say the same.

## Out of scope
Bundling QEMU, .deb/.rpm packaging, macOS, bridged networking,
signing, and every item in the out-of-scope list above.
