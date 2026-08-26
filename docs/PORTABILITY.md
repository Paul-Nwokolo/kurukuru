# Portability audit

Everything in this project was written and validated on Windows 11. This is the
record of where that shows: every place the code assumed its host, what the
assumption would have done on Linux, and what was changed.

**Status: Parts A and B complete.** Part A was a static audit on the Windows
dev machine; Part B ran the whole thing on Ubuntu 24.04. Both are recorded
here — Part A's findings first, then [what the live run
found](#part-b--live-validation-on-linux), which includes three defects the
static audit could not have caught and which were never Linux-specific at all.

Severity is what the finding does on Linux:

| | |
|---|---|
| **breaks** | the feature does not work at all |
| **degrades** | it works, but wrongly, slowly, or while reporting something false |
| **cosmetic** | no functional effect; wrong, misleading, or a trap for the next change |

---

## Summary

Part A (static audit, findings 1–15) and Part B (live on Linux, findings
16–21). The Part B numbers are the ones worth reading first: the audit found
places where the code named a platform, but running it found places where the
code had never been *exercised* — and those turned out to be the more serious.

| # | Finding | Where | Severity | State |
|---|---|---|---|---|
| 1 | Accelerator probe hardcodes WHPX | `engines/qemu.py` | **breaks** | fixed |
| 2 | `-accel` argument maps everything non-WHPX to TCG | `engines/qemu.py` | **breaks** | fixed |
| 3 | `known_hosts` sent to `os.devnull` writes a real file | `cli/commands_instances.py` | **breaks** | fixed |
| 4 | Private key hardened only on generation | `ssh_keys.py` | **breaks** | fixed |
| 5 | `-cpu qemu64` (a WHPX workaround) applied to every accelerator | `engines/qemu.py` | degrades | fixed |
| 6 | Zombie children report as alive | `engines/process.py` | degrades | fixed |
| 7 | Force-kill starts at SIGKILL | `engines/process.py` | degrades | fixed |
| 8 | "Accelerated" defined as "is WHPX" | `qemu.py`, `main.py` | degrades | fixed |
| 9 | API rejects `accel: "kvm"` | `models.py` | degrades | fixed |
| 10 | Generated cloud-init is CRLF on Windows | `cloud_init.py` | cosmetic | fixed |
| 11 | State lives in a dotfile dir, not XDG | `config.py` | cosmetic | mechanism added, policy deferred |
| 12 | POSIX `ssh` exec could fall through to the Windows path | `cli/commands_instances.py` | cosmetic | fixed |
| 13 | Buffered output lost across `execvp` | `cli/commands_instances.py` | cosmetic | fixed |
| 14 | Windows-only fallback path in a test | `tests/test_images_api.py` | cosmetic | fixed |
| 15 | Platform-specific tests were invisible in a run | suite-wide | cosmetic | markers added |
| — | Ports, sockets, seed ISO, image store, console, encoding | various | — | audited, no change needed |
| 16 | **First launch fails on any fresh install** | `routers/instances.py` | **breaks** | fixed |
| 17 | **`Running` reported ~4 min before the guest is reachable** | `engines/qemu.py` | **breaks** | fixed |
| 18 | Console test hangs forever on POSIX | `tests/test_console.py` | **breaks** (suite) | fixed |
| 19 | Suite depends on the host's free disk and prior downloads | `tests/` | degrades (suite) | fixed |
| 20 | Accelerator probe tests hardcode WHPX | `tests/test_qemu_engine.py` | degrades (suite) | fixed |
| 21 | Shutdown grace is marginal under software emulation | `config.py` | degrades | documented |

---

## Paths and filesystem

### 1. `UserKnownHostsFile=os.devnull` writes a real file — **breaks** — *fixed*

`kurukuru ssh` points ssh's `known_hosts` at the null device, because every VM is
`127.0.0.1:<recycled port>` and real host-key tracking there produces a
guaranteed false alarm. It used `os.devnull`, which is the portable-looking
choice and is wrong.

Measured on this Windows host against a live guest, from a scratch directory:

| `UserKnownHostsFile=` | MSYS ssh (Git Bash) | Win32 OpenSSH |
|---|---|---|
| `nul` (= `os.devnull`) | **creates `./nul`** | discards |
| `NUL` | **creates `./NUL`** | discards |
| `/dev/null` | discards | discards |

Git Bash's ssh is a POSIX build. It has no concept of a reserved device name,
so it takes `nul` as an ordinary relative filename and drops a `known_hosts`
file wherever the user happened to be standing — which is how a stray `nul`
appeared in this repo's scratch directory during Phase 8. Win32 OpenSSH
understands `/dev/null`.

So the fix is the opposite of what it looks like: hardcode the **POSIX**
spelling on every platform. `app/cli/commands_instances.py` now defines
`NULL_DEVICE = "/dev/null"` with the table above beside it, and a test asserts
the CLI never emits the bare device name.

The general lesson, which is why the brief called this the canonical example:
`os.devnull` describes *this process's* platform, not the platform of the
program you are handing the string to.

### 2. Private key permissions asserted only at generation — **breaks** — *fixed*

`ssh_keys._harden_permissions` chmodded 0600 on POSIX, but was called only on
the branch that had just run `ssh-keygen` — which already creates 0600. The
only case it covered was the one that was never at risk.

The cases that break on Linux are the ones where the key arrives another way:
restored from a backup, unpacked from a tarball (tar restores 0644 by default
under a permissive umask), or copied between machines. OpenSSH *refuses* a
group- or world-readable private key, so every `ssh -i` fails with
`UNPROTECTED PRIVATE KEY FILE` and nothing else works.

Hardening now runs on the reuse path too, and covers the directory (0700) as
well as the key (0600). On Windows it remains a documented no-op — mode bits
are not access control on NTFS, and Win32 OpenSSH does its own ACL check.

### Audited, no change needed

- **Path construction.** Every path in the backend is built with `pathlib` and
  `/`; there is no string concatenation and no literal separator. `~` is
  expanded at use time via `expanduser()`, never assumed to be `C:\Users\...`.
- **Traversal defence is case-correct.** `isos.resolve_iso` compares *resolved*
  paths rather than strings, so `..`, absolute paths and symlinks all collapse
  to something that fails containment. On Linux the comparison becomes
  case-*sensitive*, which is stricter, not weaker. The symlink test already in
  the suite is the meaningful one and is platform-neutral.
- **Path length.** Nothing works around the Windows 260-character limit, so
  there is nothing to break elsewhere. (The README's note about deep checkouts
  breaking `python -m venv` is advice to the user, not code.)
- **Atomic writes.** The base-image download and the image import both stream to
  a `.part` file and `Path.replace()` it into position — atomic on both
  platforms.

---

## Process management

### 3. Zombie children report as alive — **degrades** — *fixed*

`os.kill(pid, 0)` succeeds for a **zombie**: a process that has exited but
whose status nobody has collected. Every VM here is a zombie in waiting — it is
spawned as a child of the backend, which throws the `Popen` away and never
calls `wait()`.

On Linux that would make a cleanly shut-down QEMU report alive for as long as
the backend runs. The visible symptom is in `stop_instance`, which polls
`pid_alive` alone: every stop would sit through its full 90-second grace period
and then announce that the guest "ignored ACPI powerdown", before "force
terminating" a process that exited a minute earlier.

Instance *status* is not affected — `_is_running` is two-factor (pid **and**
QMP), and QMP stops answering immediately — which is exactly why this would have
been so annoying to diagnose: the dashboard would look right while stop took 90
seconds.

`_posix_pid_alive` now reaps before probing: `waitpid(pid, WNOHANG)` collects
an exited child (making the pid genuinely disappear), returns 0 for a running
one, and raises `ChildProcessError` for a process we did not spawn — which is
the post-restart case, where init is the parent and a zombie cannot arise.

Windows is unaffected: `GetExitCodeProcess` reports a terminated process as
terminated whether or not anyone collected it.

### 4. Force-kill started at SIGKILL — **degrades** — *fixed*

`terminate_pid` sent `SIGKILL` directly on POSIX. QEMU handles `SIGTERM` by
shutting the machine down and flushing qcow2 metadata; `SIGKILL` cannot be
handled at all and leaves the image needing repair on next open. This is the
backstop path after an ACPI powerdown has already timed out, so it is rare —
but it is precisely the path where the disk is most likely to be mid-write.

Now `SIGTERM` → 5s grace → `SIGKILL`. The Windows path is unchanged and
documented for what it is: `TerminateProcess` is the only mechanism available
for a process with no console, and it is unconditional — a SIGKILL by any other
name.

### Audited, no change needed

- **Detached spawn.** `start_new_session=True` (setsid) with `stdin=DEVNULL` and
  stdout/stderr to a log file is the correct POSIX equivalent of
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, and it was already there. A
  POSIX-marked test asserts the child leads its own session.
- **No shelling out.** Nothing calls `tasklist`, `taskkill`, `ps` or `kill(1)`.
  The Windows-specific work is done through `ctypes` against `kernel32` and is
  reached only behind the platform flag.

---

## QEMU invocation

### 5. The accelerator probe hardcoded WHPX — **breaks** — *fixed*

`_probe_accel` launched QEMU with `-accel whpx,kernel-irqchip=off` on every
platform. On Linux QEMU exits immediately with an invalid-accelerator error, so
the probe would conclude "no acceleration available" and fall back to TCG —
**every VM on every Linux host running under software emulation**, roughly 30×
slower, with `/health` and `/diagnostics` correctly reporting no acceleration
and no indication that the tool never asked for the right one.

The candidate is now chosen by platform, because there is no choice to make
within one — WHPX exists only on Windows, KVM only on Linux, HVF only on macOS:

```python
HOST_ACCELS = {"win32": "whpx", "linux": "kvm", "darwin": "hvf"}
```

A platform not in the map gets TCG with a warning that says so.

Linux also gets a pre-check, because `/dev/kvm` is the overwhelmingly common
failure and QEMU's own message for it is easy to lose in a log. A missing
device and an unreadable one are distinguished, since only one of them has a
fix the user can apply:

- absent → no KVM support, virtualization disabled in firmware, or a VM without
  nested virtualization
- present but not readable/writable → *add the backend's account to the `kvm`
  group*

### 6. `_accel_arg` mapped everything except WHPX to TCG — **breaks** — *fixed*

```python
return "whpx,kernel-irqchip=off" if accel == "whpx" else "tcg"
```

A row that asked for `kvm` would have been launched with `-accel tcg`. This is
the sharp edge behind finding 5: even with the probe fixed, the argument
builder would have quietly discarded the answer. It now maps each accelerator
to its own argument, keeps `kernel-irqchip=off` for WHPX alone (it is required
there and accepted nowhere else), and still falls back to TCG for a name it
does not recognise rather than passing it through to QEMU.

### 7. `-cpu qemu64` applied to every accelerator — **degrades** — *fixed*

`qemu64` exists solely because WHPX dies on `-cpu max`/`host` with
"Unexpected VP exit code 4". Under KVM, `host` is both correct and materially
faster — the guest gets the physical CPU's AES-NI, AVX and the rest instead of
emulated substitutes. Carrying a Windows workaround onto Linux would have
hobbled every guest there for a bug that does not exist on the platform.

Now: WHPX → `qemu64`, KVM/HVF → `host`, TCG → `max`. `KURUKURU_QEMU_CPU_MODEL`
still overrides all of it.

### 8. "Accelerated" was defined as "is WHPX" — **degrades** — *fixed*

Three places asked `accel == "whpx"` to answer "is this hardware-accelerated?":
`QemuEngine.describe()`, `/host/capacity` in `main.py`, and
`resolve_accel`'s fallback logic. The two questions were indistinguishable
while Windows was the only host; on Linux they diverge, and a working KVM host
would have reported `accel_available: false` — the dashboard and `kurukuru doctor`
telling the user to go and fix a problem they do not have.

All three now test membership of `HARDWARE_ACCELS`. `resolve_accel` also
resolves a *foreign* request down to the host's own accelerator (asking for
WHPX on a KVM host gives KVM, with a warning) rather than dropping to TCG,
because the caller's evident intent was hardware acceleration.

### 9. The API rejected `accel: "kvm"` — **degrades** — *fixed*

`KNOWN_ACCELS` was `("auto", "whpx", "tcg")`, so a Linux user naming their own
accelerator got a 422. Now `("auto", "whpx", "kvm", "hvf", "tcg")`, with all
names accepted on all platforms and the driver resolving what the host cannot
provide — a request that was valid yesterday should not start failing because
the backend moved to another machine. `auto` remains the right thing for a
portable client to send, and is the default.

### Deferred to Part B: the display default

`std` VGA is the default and `virtio` the option, and that choice was driven
entirely by WHPX: QEMU's VGA emulation depends on memory dirty-tracking WHPX
does not provide, so a guest that stays in VGA text mode (notably the Ubuntu
cloud image) renders a black console. KVM provides dirty-tracking, so the
limitation very likely does not apply — but "very likely" is how the original
WHPX conclusion was wrong for two phases, and it was only settled by booting a
guest and diffing the framebuffer.

**No change has been made.** The mechanism is already accelerator-keyed, so it
does the right thing either way once the fact is known:
`ACCELS_WITHOUT_DIRTY_TRACKING = frozenset({"whpx"})` means no caveat is
emitted under KVM already. Part B should test it the way the WHPX
investigation did — boot the cloud image on `std` under KVM, inject a
keystroke, diff the framebuffer — and only then decide whether the *default*
should be platform-dependent.

### Binary names and packaging

`qemu-system-x86_64` and `qemu-img` are correct on both platforms; only the
packaging differs. Documented in `.env.example`, along with the reminder that
`KURUKURU_QEMU_SYSTEM_BINARY` / `KURUKURU_QEMU_IMG_BINARY` exist for installs that are
not on `PATH`:

| Distro | Packages |
|---|---|
| Debian / Ubuntu | `qemu-system-x86`, `qemu-utils` |
| Fedora / RHEL | `qemu-kvm`, `qemu-img` |
| Arch | `qemu-full` (or `qemu-base` + `qemu-img`) |

---

## Sockets and networking

### Port allocation is platform-correct by construction — *no change*

`is_port_free` binds the candidate and reports whether the bind succeeded, with
**no `SO_REUSEADDR`** — verified by inspection and by test. That is the
important half: with `SO_REUSEADDR` on Linux a port in `TIME_WAIT` binds
successfully, which is exactly the collision the probe exists to catch.

The platforms do differ here. Measured on this host: with a socket held on
`0.0.0.0:P`, binding `127.0.0.1:P` **succeeds** on Windows, so `is_port_free`
answers "free". On Linux the same bind fails, so it would answer "taken".

That difference is harmless, and the reason is worth writing down: the probe
binds *exactly what QEMU will bind* — same address, same port, same options —
so whatever a platform's rule is, the probe predicts QEMU's outcome on that
platform. This is why the design survives the difference without a branch.

Neither platform's ephemeral port range overlaps the pools in use (2200–2299,
4400–4499, 5900–5999): Linux defaults to 32768–60999, Windows to 49152–65535.
A host with a hand-lowered `net.ipv4.ip_local_port_range` could collide, and
would be caught by the bind probe rather than by a failed launch.

### Loopback binding carries over unchanged — *no change*

VNC, QMP and the SSH forward are `127.0.0.1` literals in the QEMU command line,
and the invariant test asserts no argument anywhere in that line contains
`0.0.0.0`. The command line is identical on both platforms, so the guarantee
holds on Linux for free. Confirm it live in Part B anyway — this is the one
where being wrong publishes every VM's screen to the LAN.

### Recommendation: keep QMP on TCP — *not implemented, by design*

QMP runs over a loopback TCP socket because Windows has no Unix domain sockets.
Linux does, and they would be a genuine improvement in one respect: no port to
allocate, so a third of the port-collision surface disappears along with a
third of `runtime.json`'s pinned state.

**Recommendation: do not.** Supporting both means two code paths through the
QMP client, the port allocator, the runtime file and its migration, in exchange
for removing a collision risk the bind probe already handles and that has never
been observed. Unix sockets also bring their own portability question — path
length limits on `sun_path` (108 bytes) against an instance directory under
`$HOME` — trading a solved problem for an unsolved one.

TCP-everywhere is the simpler system and is good enough. If Part B finds port
exhaustion or collisions in practice, revisit with evidence.

---

## Cloud-init and images

### 10. Generated cloud-init used the host's line endings — **cosmetic** — *fixed*

`build_cloud_init` wrote the `#cloud-config` document in text mode, so on
Windows it was CRLF and on Linux LF — two different documents from one input.
Measured: 11 CRs in the file on this host.

It reaches the guest intact today, but only by accident: the engine reads it
back with `read_text()`, whose universal-newline translation strips the CRs
before they enter the seed ISO. The defect is cancelled, not absent — and the
obvious optimisation (`read_bytes`, skip the re-decode) would uncancel it and
hand cloud-init a CRLF document, which is a class of failure that shows up as a
guest that boots with no user and no key.

Now written with `newline="\n"`, with a test asserting the bytes contain no CR.

### Audited, no change needed

- **The seed ISO is byte-clean.** `seed.py` takes strings, encodes UTF-8 and
  writes through `BytesIO`; no path assumptions and no text mode. pycdlib
  remains the right call on both platforms — it was chosen to avoid
  `genisoimage` on Windows, and avoiding a subprocess is not worse on Linux.
- **The image store** builds every path with `pathlib`, resolves user-supplied
  paths with `expanduser().resolve()`, and reports an unreadable source in terms
  of the backend process's access rather than assuming ownership.

### 11. State lives in a dotfile directory, not XDG — *mechanism added, policy deferred*

`~/.kurukuru/` is conventional on Windows and macOS and *a* convention on
Linux, where the XDG Base Directory spec would put state under
`$XDG_DATA_HOME` (`~/.local/share/kurukuru`).

Changing the default would move every existing install's VMs and keys out from
under it, which presents as "all my instances disappeared". So the default is
untouched and `KURUKURU_STATE_DIR` is the supported way to relocate everything at
once:

```bash
KURUKURU_STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kurukuru"
```

The four directories that follow it (`qemu_dir`, `ssh_key_dir`,
`cloud_init_dir`, `iso_dir`) are re-rooted only when they are still at their own
defaults — an explicitly set directory is a decision and always wins, so
"state on the system volume, ISOs on the big disk" stays expressible. Tests
cover both the re-rooting and, more importantly, that the defaults are
byte-for-byte what they were.

Whether Linux should *default* to XDG is a policy question for Part B, when
there is a Linux user to have an opinion. The CLI's own config file
(`~/.kurukuru/cli.toml`) is deliberately not covered: the CLI does not read
backend settings, by design.

---

## CLI and tooling

### 12–13. The POSIX `ssh` path — **cosmetic** — *fixed*

The POSIX branch does use `os.execvp`, which is correct: ssh takes over the
terminal, so job control, Ctrl-C and the exit status all behave as if it had
been typed. Two flaws around it, both found by writing the test:

- **No terminal statement.** `execvp` does not return, so nothing followed it —
  but nothing *stopped* control reaching the Windows `subprocess.run` below
  either. An explicit raise now makes the branch terminal.
- **Buffers were not flushed.** `execvp` replaces the process image without
  flushing Python's buffers, so anything pending is lost — including the
  `ssh -i ...` line the CLI prints, which is most wanted in exactly the
  redirected run where it would have been dropped.

### Audited, no change needed

- **UTF-8 stream reconfigure is a no-op on Linux.** Verified: `reconfigure()`
  mutates the existing `TextIOWrapper` in place (same object id — no second
  wrapper), and on Linux the encoding is already `utf-8`, so the only change is
  `errors="strict"` → `"replace"`, which is wanted everywhere. The fix was for
  Windows, where a redirected stream defaults to the locale encoding (`cp1252`
  here).
- **Shell completion generates for every target.** Confirmed on this host:
  bash (263 B), zsh (165 B), fish (291 B), powershell/pwsh (804 B), each exiting
  0. PowerShell targets are inert on Linux but harmless, and the command
  validates the shell name rather than emitting a broken script.

---

## Tests

### 14–15. The suite could not say what it had skipped — *fixed*

Two problems, one theme.

`test_images_api.py` fell back to a hardcoded
`C:\Program Files\qemu\qemu-img.exe` when `qemu-img` was not on `PATH` — on
Linux, a path that cannot exist, turning "qemu-img is installed somewhere
unusual" into a silent skip of the entire image-probing suite. It now consults
`KURUKURU_QEMU_IMG_BINARY` (the same override the backend honours) and says so in
the skip reason.

Platform markers are now registered and applied, with `--strict-markers` so a
typo disables nothing quietly:

| Marker | Meaning |
|---|---|
| `windows` | needs a Windows host (Win32 APIs, path separators, ACLs) |
| `posix` | needs a POSIX host (signals, sessions, file modes) |
| `posix_logic` | POSIX *decision logic*, exercised anywhere by substituting syscalls |

`posix_logic` is the interesting one, and its limits should be stated plainly:
it proves the code routes correctly given each syscall outcome — reap before
probing, SIGTERM before SIGKILL, exec rather than spawn. It does **not** prove
the kernel produces those outcomes. That a freshly exited QEMU really presents
as a zombie, and that `waitpid` really clears it, is a Part B observation.

On this Windows host the run is **351 passed, 2 skipped** — the two skips being
the POSIX session test and the Windows-only test that does not apply where it
was run.

---

## Part B — live validation on Linux

Ubuntu 24.04.3 LTS, kernel 6.8, 2 cores / 3.9 GB RAM / 9.8 GB disk, run as a
non-root user (`claude`, uid 1002). Every VM was terminated before the next was
started, to fit the host.

### The test environment had no hardware virtualization

**This is a limitation of the test host, not a finding about the product.** The
box is itself a cloud guest whose provider does not expose nested
virtualization:

```
$ grep -oE 'vmx|svm' /proc/cpuinfo | sort -u    # (nothing)
$ ls -l /dev/kvm                                # No such file or directory
$ systemd-detect-virt                           # kvm
```

So everything below ran under **TCG software emulation**. The backend detected
this correctly and said so in the terms the user needs, which is itself a
Part A fix working:

```
WARNING kurukuru.qemu: Acceleration probe: /dev/kvm does not exist — this host has
no KVM support, or virtualization is disabled in its BIOS/UEFI (or it is itself
a VM without nested virtualization) — falling back to TCG
```

Two brief items therefore **cannot be answered here and are deferred**, not
guessed:

- **Boot time under KVM, compared with Windows/WHPX (~13–23 s).** What was
  measured is TCG on 2 cores: **254 s** first boot, **200 s** restart, **258 s**
  and **309 s** on later launches. That is a number for software emulation on a
  small host and says nothing about KVM.
- **Whether the Ubuntu cloud image renders on `std` VGA under KVM.** The
  Part A question — whether the WHPX text-mode limitation applies to KVM, and
  so whether the display default should be platform-dependent — is untouched.
  It needs a host with `/dev/kvm`, a framebuffer diff, and a keystroke, exactly
  as the WHPX investigation did. What *was* shown is that `std` renders fine
  under TCG (see item 4 below), which was expected and is not evidence about
  KVM either way.

Everything else in Part B was answerable and was answered.

### 16. First launch fails on any fresh install — **breaks** — *fixed*

The most serious finding of the phase, and not a portability defect at all —
Linux is simply where a genuinely fresh install existed for the first time.

```
$ kurukuru launch first-boot --disk 4
first-boot  a66bef5c-…  Pending
$ kurukuru show first-boot
status: Error | Image 'Ubuntu 24.04 LTS (cloud)' is Importing, not Available
```

The built-in Ubuntu image is downloaded **on demand** by the engine
(`ensure_base_image`), so its catalog row sits at `Importing` — "not downloaded
yet" — until something asks for it. `_build_launch_options` required
`Available`, so the first launch was refused **for the absence of the very file
that the launch was supposed to fetch**. The README documents the opposite
("The first launch on a fresh install is slower because it downloads the base
image"), and the engine has the download path; the gate simply never let it
run.

It survived nine phases because every developer machine had already downloaded
the image during an earlier phase, and because the *second* attempt works — the
failed first one leaves the file behind. It would have hit a fresh Windows
install identically.

`_image_is_launchable` now allows `Importing` for the built-in image only.
`Error` still refuses everywhere, including for the built-in: that means the
file is present and unreadable, which downloading does not fix. An imported
image that is still copying is refused as before — nothing fetches those.

Verified on the host that had never downloaded it:

```
08:00:04  launch accepted
08:00:23  Base image download: 544.0 MiB (91%)
08:00:25  Base image ready: …/noble-server-cloudimg-amd64.img (595.3 MiB)
08:04:39  Instance 'first-boot' up in 254.3s (accel=tcg, source=image)
```

Three regression tests pin it, including the case a dev machine can never
reproduce naturally (`test_first_launch_works_before_the_base_image_is_downloaded`).

### 17. `Running` reported four minutes before the guest is reachable — **breaks** — *fixed*

Also not Linux-specific in cause. A slow host is just what makes it visible.

Measured on the first boot: the row said `Running` at **~30 s**, while the
guest did not answer SSH until **254 s**. For 224 seconds the API, the
dashboard and `kurukuru ls` all reported a healthy, addressed instance that refused
every connection.

The cause is in the reconciler's guard. A background pass may promote a row out
of `Provisioning` only if the engine reports `Running` **with an address** —
the comment calls the address "evidence of readiness". For QEMU it is not
evidence of anything: `get_instance_info` synthesised the address from
*liveness* (`pid alive` + `QMP answers`), both true about a second after spawn.
So the guard never fired, and any reconcile pass landing mid-boot promoted the
row. Under WHPX the boot is ~20 s against a 30 s reconcile interval, so the
race is usually missed; under TCG it is won every time.

Two further consequences, both invisible until now:

- `kurukuru launch --wait` returned on an unusable instance. The Phase 8 SSH
  banner pre-flight in `kurukuru ssh` was the only thing keeping the headline
  one-liner working.
- `degraded` — "Running, expected an address, still has none" — was **dead code
  for the only shipping engine**, because QEMU always supplied an address.

The fix makes the address mean what every consumer already assumed: it is
reported only when the guest's SSH banner actually answers. Verified on a real
slow boot:

```
   4s   status=Provisioning   ssh_banner=none
 264s   status=Running        ssh_banner=SSH-     ← now coincident
```

**A follow-on, caught by the same live test.** The first version of this fix
used a 1 s probe budget, which looked generous. On this host the banner takes
**1.01–1.24 s** — the emulated CPU has to run sshd's accept path — so a healthy
guest intermittently reported no address, and `launch --wait && ssh` failed on
a VM that was serving perfectly. The budget is now the same 5 s the engine's
own boot wait already used; inventing a tighter number for the same question
was the mistake. Cost is bounded: a stopped VM refuses instantly, so only a
Running-and-still-booting instance can spend it.

### 18. The console test hangs forever on POSIX — **breaks the suite** — *fixed*

The first full suite run on Linux never finished. `py-spy` on the stuck process
named it precisely: `test_vm_closing_its_socket_ends_the_session`, blocked in
`receive_bytes`, holding an established loopback socket.

The fake QEMU closed its socket to simulate a VM going away. Measured, with the
same script on both platforms:

| | `close()` alone | `shutdown()` + `close()` |
|---|---|---|
| **Linux** | **no EOF — peer hangs forever** | EOF seen |
| **Windows** | `ECONNRESET` (abortive) | — |

Closing a socket that another thread is blocked in `recv()` on does not wake
that thread on POSIX: the descriptor is dropped, the socket survives behind the
blocked call, and **no FIN is sent**. Windows resets the connection instead,
which is why this stood up for nine phases and hung on the first Linux run.

**This is a test-harness defect, not a product defect** — a distinction worth
being precise about. `console.py` handles EOF correctly, and real QEMU needs
none of this: a process exiting closes its descriptors and the kernel sends
FIN. Only a *simulated* departure has to be explicit. `FakeVncServer.close()`
now calls `shutdown(SHUT_RDWR)` first.

### 19. The suite depended on the host's free disk and prior downloads — degrades — *fixed*

With that hang cleared, the first complete Linux run was **67 failed, 284
passed**. Almost all of it was two ambient-state dependencies, not 67 bugs:

- **Free disk.** The host had 4.8 GB free; the `small` preset asks for 5 GB, so
  the capacity check correctly refused, and ~60 tests failed several frames
  away from the cause with `KeyError: 'id'`. The codebase already had the right
  idea — the `small_host` fixture exists so "capacity assertions must not
  depend on whatever the machine running the tests happens to have free" — it
  just was not applied by default. It now lives in `tests/conftest.py` and
  every client fixture depends on it, because *any* test that launches an
  instance is a capacity assertion whether it means to be or not.
- **A previously downloaded base image.** The remaining 18 were finding 16
  above: the tests assumed the built-in image was `Available`, which is only
  true on a machine that has already run one. Fixing the product fixed the
  tests.

### 20. Accelerator probe tests hardcoded WHPX — degrades — *fixed*

Two tests asserted `engine.accel() == "whpx"`, which is a property of the
machine the test was written on. They are now parameterised over
win32/linux/darwin and assert the mechanism — a probe that survives its timeout
means the accelerator works — rather than the answer one host gives. A third
now covers the `/dev/kvm` short-circuit, which is this host's real situation.

### 21. Shutdown grace is marginal under software emulation — degrades — *documented*

Two graceful stops were measured: **89 s** and **46 s**, against a 90 s
`KURUKURU_QEMU_SHUTDOWN_TIMEOUT_SECONDS`. The first came within one second of being
force-killed — and a force-kill of a guest that is mid-shutdown is how a qcow2
ends up needing repair.

The elapsed time is the guest, not the plumbing (see the next section). But the
default was chosen on an accelerated host, and on any TCG host a normal
shutdown can exceed it. Recorded rather than changed: the right number depends
on the guest, and raising it lengthens every genuinely-wedged stop. Users on
software emulation should raise it.

### The two checks Part A flagged for verification

**Does the zombie reap land?** Yes — proven directly rather than inferred. A
real child, spawned through `spawn_detached`, allowed to exit:

```
child pid          : 40225
/proc state BEFORE : Z      (zombie, awaiting reap)
pid_alive() says   : False
/proc state AFTER  : gone
```

And during a real VM stop, polling QEMU's process state every second:

```
0s→46s   R/S alternating   QEMU actively emulating the guest's shutdown
46s      Z                 exited, awaiting reap
47s      gone              reaped
```

Zombie for at most one second. The 46 s and 89 s stops are the guest's own
shutdown under emulation, not the reap — which is finding 21, not a defect in
the Part A fix.

**Does the key-permission repair land?** Yes, and the hard break it prevents
was demonstrated rather than asserted. With the key deliberately at 0644 and
the orchestrator key temporarily authorised so the check is actually reached:

```
@@@@ WARNING: UNPROTECTED PRIVATE KEY FILE! @@@@
Permissions 0644 for '…/id_ed25519' are too open.
This private key will be ignored.
   ↓ the reuse path (get_private_key_path → ensure_keypair) runs
mode now: -rw-------
LOGIN_OK
```

The directory is repaired to 0700 alongside it.

### Everything else, as run

| Brief item | Result |
|---|---|
| 1. Install from the README verbatim | Works, with 3 doc deviations — see below |
| 2. Full lifecycle | launch → SSH → stop → start → terminate, all pass. Boot-time comparison **deferred** (no KVM) |
| 3. Console | RFB 003.008 handshake over the WebSocket bridge, bytes both ways, `console_caveat: None` (correctly not warning under TCG). KVM display question **deferred** |
| 4. ISO boot | Alpine 3.21 to an interactive `boot:` prompt. Framebuffer via QMP `screendump`: 720×400, **5 distinct colours** — renders, not blank |
| 5. Image import | 128 MB qcow2 imported → `Available`; instance overlay's backing file is the **copy in the store**, `backing file format: qcow2` |
| 6. Capacity | psutil figures correct (2 vCPU / 3.8 G / 9 G); arithmetic holds; oversubscription note renders. **cgroups: not applicable** — this is a VM, not a container, so `virtual_memory()` reports the real limit. In a container it would report host totals and overcommit; still open for containerised deployment |
| 7. CLI | Every command exercised; `--json` parses; `ssh` execs with `/dev/null` and leaves no stray file. One-liner `kurukuru launch web-01 --wait && kurukuru ssh web-01 whoami` → `kurukuru`, **exit 0**, 309 s (TCG) |
| 8. Suite | **359 passed, 2 skipped** on Linux; identical on Windows |

Loopback binding confirmed with a live VM, which is the invariant worth
checking on a new platform:

```
LISTEN 127.0.0.1:2200  qemu-system-x86    (ssh)
LISTEN 127.0.0.1:4400  qemu-system-x86    (qmp)
LISTEN 127.0.0.1:5900  qemu-system-x86    (vnc)
```

Nothing on `0.0.0.0`.

### Install deviations (brief item 1)

The README's install steps do not work verbatim on Ubuntu 24.04. All three are
documentation defects:

1. **`python -m venv .venv` fails** — Ubuntu ships no `python`, only `python3`.
2. **`python3 -m venv .venv` also fails** — `ensurepip is not available`,
   needing `apt install python3.12-venv`. Worth noting that the README's
   troubleshooting section already covers an `ensurepip` error and attributes
   it to the Windows path limit; on Linux the same message has a completely
   different cause and fix.
3. **QEMU is not mentioned as a Linux prerequisite.** `qemu-img` was present
   (pulled in by something else) but `qemu-system-x86_64` was not, and the
   Requirements table names only Windows Hypervisor Platform.

What was actually needed:

```bash
sudo apt install python3.12-venv qemu-system-x86 qemu-utils
```

QEMU 8.2.2 from Ubuntu's archive — a different major version from the Windows
box's 10.0.94, which independently confirms the "8.0+ expected" claim.

### Still open after Part B

1. **Boot time under KVM**, and the WHPX comparison. Needs `/dev/kvm`.
2. **The `std` vs `virtio` display default under KVM** — the Part A question,
   unchanged. Boot the cloud image on `std` under KVM, inject a keystroke, diff
   the framebuffer.
3. **`-cpu host` under KVM has never been executed.** Part A made it the
   KVM/HVF choice on the reasoning that passing the physical CPU through is
   correct and faster. That reasoning is sound and entirely untested — this
   host ran `-cpu max` under TCG, as designed.
4. **psutil under cgroups**, for containerised deployment.
5. **Whether Linux guests need the forced resolvers.** `qemu_guest_nameservers`
   works around a Windows SLIRP DNS defect; cloud-init's package install
   succeeded here with the default in place, so it was never tested empty.
6. **macOS/HVF** remains entirely unexercised.

---
## Guiding rule, and where it was applied

The brief's rule was to prefer genuinely portable code over branching, and to
branch only where the platforms really differ.

Branches added: the accelerator candidate (`HOST_ACCELS`), which is a real
platform difference — each OS has exactly one hypervisor interface; and POSIX
liveness and termination, which are real differences in process semantics.

Branches *not* added: the null device, which looked like the most obvious
platform switch in the codebase and turned out to need one hardcoded POSIX
string; and the state directory, where a setting with a preserved default beat
a per-platform default.
