# PHASE 9 BRIEF — Linux Support

Everything so far is Windows-validated only. This phase makes the project
run on Linux and, more importantly, finds where it has quietly assumed
Windows. Read docs/ARCHITECTURE.md and docs/DECISIONS.md first — several
decisions were made for Windows-specific reasons and need revisiting, not
preserving.

Two parts. PART A is a static audit that runs on the Windows dev machine
and needs no Linux host. PART B is live validation and requires a Linux
host with /dev/kvm. Do A fully and report before starting B; A's findings
will tell us what B is likely to hit.

Guiding rule: prefer genuinely portable code over branching. Add a
platform branch only when the platforms really differ (process spawning,
socket types), never to paper over a hardcoded assumption.

---

## PART A — Portability audit (no Linux host needed)

Produce `docs/PORTABILITY.md` recording every finding, its location, its
severity (breaks / degrades / cosmetic), and the fix — then implement the
fixes that don't need a Linux host to verify.

Audit at minimum:

### Paths and filesystem
- Every path construction: any string concatenation or literal separator
  that should be pathlib. `~` expansion, absolute-path assumptions, and
  case-sensitivity assumptions (Linux is case-sensitive; a filename that
  works on Windows may not resolve).
- The `NUL` incident is the canonical example: `-o UserKnownHostsFile=NUL`
  created a literal file on Windows. `/dev/null` vs `NUL` needs a
  platform-correct constant (`os.devnull`), and the same class of bug may
  exist elsewhere.
- File permissions: ssh_keys.py notes a Windows ACL limitation in a
  comment. On Linux, 0600 on the private key is REQUIRED — OpenSSH
  refuses keys with loose permissions. Verify the POSIX path actually
  sets it, and that the instance store and cloud-init dirs are sane.
- Path length: the 260-char limit is Windows-only. Ensure nothing works
  around it in a way that breaks elsewhere.

### Process management
- The detached-spawn path uses Windows creationflags (DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP). Linux needs the equivalent: start_new_session
  (setsid) plus redirected std handles, so QEMU survives the parent.
- Process liveness checks: any use of tasklist/taskkill or Windows APIs
  needs a POSIX equivalent. psutil covers most of this portably — prefer
  it over shelling out.
- Signal handling: SIGTERM before SIGKILL on POSIX; the Windows fallback
  is TerminateProcess. Make the escalation explicit and platform-correct.

### QEMU invocation
- Accelerator: whpx is Windows-only. Linux uses kvm; macOS uses hvf.
  The probe must select by platform and available device (/dev/kvm
  present and readable), and fall back to tcg with a warning. Report
  clearly which was chosen — this is already surfaced in /health and
  /diagnostics; make it correct per platform.
- CPU model: `-cpu qemu64` was chosen because `max` crashes under WHPX.
  That workaround is Windows-specific — under KVM, `host` is the right
  choice and is significantly faster. Make the CPU model
  accelerator-aware (it partly already is) and verify the mapping.
- The VGA/display decision (std default, virtio option) was driven by the
  WHPX text-mode limitation. Under KVM this limitation does not apply.
  Do NOT change the default yet — record the question for Part B to
  answer with evidence, the way the WHPX investigation did.
- Binary names: qemu-system-x86_64 is right on both, but distros vary in
  packaging; keep the settings override working and document the package
  names (Debian/Ubuntu: qemu-system-x86, qemu-utils).

### Sockets and networking
- QMP over localhost TCP was chosen because Windows lacks Unix sockets.
  On Linux, Unix sockets are available and preferable (no port
  allocation, no chance of collision). Assess: is it worth supporting
  both, or is TCP-everywhere simpler and good enough? Recommend, don't
  implement yet.
- Port allocation and bind-probing: verify the logic has no
  Windows-specific behaviour (SO_REUSEADDR semantics differ between
  platforms — on Linux it permits rebinding in TIME_WAIT, which can mask
  a collision).
- Confirm VNC and QMP still bind strictly to 127.0.0.1 on Linux.

### Cloud-init and images
- The NoCloud seed ISO is built with pycdlib (chosen to avoid
  genisoimage on Windows) — that stays correct and portable; confirm no
  path assumptions leaked in.
- Base image download path and cache location: `~/.local-iaas/` should
  probably follow the XDG spec on Linux (XDG_DATA_HOME /
  XDG_CONFIG_HOME) rather than a dotfile in $HOME. Recommend a layout;
  implement behind a setting with the current default preserved so
  nothing breaks for existing installs.

### Tooling and CLI
- Encoding: the cp1252 fix forced UTF-8 on stdout/stderr. Confirm it's a
  no-op on Linux rather than a double-wrap.
- `iaas ssh` execs on POSIX and subprocesses on Windows — verify the
  POSIX branch actually exists and is correct (os.execvp replaces the
  process, which is what you want for a passthrough).
- Shell completion: the powershell target is Windows-only; confirm
  bash/zsh/fish generation works.
- Line endings: ensure no CRLF assumptions in generated files
  (cloud-init YAML especially — CRLF in user-data can break parsing).

### Tests
- Any test that hardcodes a Windows path, drive letter, or executable
  extension. Mark platform-specific tests with pytest markers
  (@pytest.mark.windows / @pytest.mark.posix) so the suite is honest
  about what it did and didn't cover on a given host.

Deliverable for Part A: docs/PORTABILITY.md, the fixes applied that can
be verified on Windows, the full suite still green on Windows, and a
clear list of what only Part B can settle.

---

## PART B — Live validation (requires a Linux host with /dev/kvm)

Do not start until a host is available. Ubuntu 24.04 LTS is the reference
target.

1. **Install from the README, verbatim, on a clean machine.** Every
   command must work as documented. Record every deviation — a fix to
   the docs is as valuable as a fix to the code here. Note the distro
   package names actually needed.
2. **Full lifecycle**: launch a cloud image, reach Running, SSH in via
   the Copy SSH / `iaas ssh` command, stop, start, terminate. Report boot
   time under KVM and compare with the Windows/WHPX figure (~13-23s).
3. **Console**: open the noVNC console. Answer the question Part A
   deferred — does the Ubuntu cloud image render under KVM with the
   default std VGA, or does it stay in VGA text mode as it does under
   WHPX? Test the same way the WHPX investigation did (boot a guest,
   inject a keystroke, diff the framebuffer). If std works under KVM, the
   default may be platform-dependent — recommend, with evidence.
4. **ISO boot**: Alpine from ISO to an interactive console.
5. **Image import**: import a qcow2, launch from it, verify the backing
   file.
6. **Capacity**: confirm psutil reports sane figures and that the
   allocatable arithmetic holds on Linux (cgroup limits, if running in a
   container, will differ from host totals — note whether that matters).
7. **CLI**: the full command set, and the one-liner
   `iaas launch web-01 --wait && iaas ssh web-01 whoami`.
8. **The suite**: pytest on Linux. Report which platform-marked tests
   skipped and why.

## Report back
- docs/PORTABILITY.md as the durable artifact.
- Part A: findings by severity, what was fixed, what's deferred.
- Part B: the install transcript, the boot-time comparison, the console
  evidence, and anything that behaved differently from Windows.
- A plain statement of Linux support status for the README: what works,
  what's untested, what's known broken.

## Out of scope
macOS/HVF (next), packaging and installers, systemd service units
(installer phase), distro packages (.deb/.rpm), containerised deployment.
