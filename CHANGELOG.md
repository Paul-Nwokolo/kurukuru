# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is `0.x`, breaking changes may land in a minor release; they
will always be listed under **Changed** with the migration path.

Design decisions behind these changes are recorded in
[docs/DECISIONS.md](docs/DECISIONS.md), and what is planned next is in
[docs/ROADMAP.md](docs/ROADMAP.md).

## [0.1.2] — 2026-09-22

Everything here came from two external installs. Nothing in it was found on
the development machine, and most of it could not have been: the failures are
properties of a *fresh* Windows 11 machine, of an install shape nobody tested,
and of an offline moment nobody staged.

### Fixed

- **Smart App Control blocks the bundled QEMU, and nothing said so.** On a
  clean Windows 11 install, Smart App Control is on by default and refuses any
  program it does not recognise. Kurukuru is not code-signed, so it refuses
  Kurukuru — and all the user saw was an engine reporting unavailable and

  ```
  qemu-system-x86_64.exe --version failed (exit 3236495362)
  ```

  That number is a signed NTSTATUS in decimal, so it does not read as a status
  code at all. It is `0xC0E90002`, `STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION`
  — Windows' own message for it is "An Application Control policy has blocked
  this file". The user took the decimal to an AI assistant, which converted it
  wrongly and told them to add QEMU to `PATH`: a fix for a problem they did not
  have, on an install whose path resolution was already correct.

  Windows status codes are now reported in hex, with their name and a plain
  sentence about what to do. `kurukuru doctor` reads Smart App Control's state
  and says plainly when it is what is blocking Kurukuru — and, when it is off
  but something still blocked QEMU, says that too, because `0xC0E90002` is also
  what an enterprise policy returns and that one is the administrator's to lift.

- **A launch that failed before reaching the hypervisor had its cause
  overwritten.** Offline, a launch correctly reported that the base image could
  not be downloaded. A minute later the reconciler replaced that with "VM no
  longer exists on the hypervisor" — not merely less useful but false, since
  that VM never existed. A row already in `Error` now keeps the cause it
  recorded; absence is only news for a row that was previously healthy.

- **The offline download failure now leads in plain language.** "Couldn't
  download the Ubuntu base image — check your internet connection", followed by
  the way out (Images → Add image, for a machine with no internet), followed by
  the raw error, which is kept because a bug report needs it. An HTTP error is
  no longer described as a connection problem.

- **The built-in image stayed on "Importing" forever.** Its file is downloaded
  by the *engine*, during a launch, so nothing in the images router ever learned
  it had arrived: the Images page went on showing "Importing" with no virtual
  size while the file sat on disk at full size with an instance running off it.
  The row is now brought up to date after a launch.

- **Uninstall and upgrade hit blocked files.** The backend runs as a logon
  task, so on any machine where Kurukuru has been used it is *running* when its
  own installer starts. The installer and the uninstaller now stop the task and
  end any Kurukuru and QEMU processes before touching a file, and a file that
  still cannot be replaced produces a clear message and no changes at all,
  rather than a partial install that will neither start nor uninstall.

- **Uninstall left a dead `PATH` entry.** Inno appends to `PATH` and has no
  matching removal, so every uninstall since 0.1.0 left an entry pointing at a
  directory that no longer existed — while the docs claimed it was removed. It
  is now removed. Found while verifying the fix above, by noticing that two
  test installs had accumulated two dead entries.

- **The dashboard's "task is running?" hint named a task that has never
  existed** (`KurukuruBackend`; it is `Kurukuru`).

- **`kurukuru auth init` crashed with `NameError: name 'engine' is not
  defined`** — the first command a new user runs, broken in 0.1.1 and 0.1.0.
  `kurukuru.database` builds its engine lazily through PEP 562's module
  `__getattr__`, which serves `kurukuru.database.engine` to importers but
  *not* a bare `engine` written inside a function in that same file: that is a
  global name lookup, and global lookup never consults `__getattr__`. It
  worked only because some importer had already fetched the attribute, which
  caches it into the module's globals. The backend always has such an importer
  (the routers); the CLI's `auth init` path does not. Invisible to the whole
  suite, because every test imports a router or patches the engine directly —
  so the new test for it runs in a subprocess that has imported nothing else.
  Found by running the packaged build rather than the checkout.

- **The test suite failed on a machine that had never run Kurukuru.** The test
  that proves the isolation guard works has to create `~/.kurukuru/keys` so it
  has somewhere to plant a deliberate leak. `mkdir(parents=True)` also creates
  `~/.kurukuru`, and the cleanup removed only the leaf — so the state root
  appeared once and stayed, drifting the fingerprint that same guard compares
  and blaming whichever unrelated test happened to straddle it. Scattered
  teardown errors in the CLI and event suites, reproducible only in a full run
  and only where no real install exists, which is every fresh clone and CI.
  Found while verifying this release, by deleting the directory.

### Changed

- **The default VM user is now `kurukuru`, not `iaas`** — the pre-rename name.
  Done in the order that makes it invisible: the login is now recorded on the
  instance row, every existing row is backfilled with `iaas` (correct for all of
  them, since no release ever shipped another default), cloud-init is fed the
  row's user so the guest and the row agree by construction, and only then does
  the default move. **An instance created before 0.1.2 reports `iaas` and its
  "Copy SSH" command is byte-for-byte what it was.** A clone inherits its
  source's login, because its disk carries the source's accounts.

- **The installer is per-user only.** The "install for me / for all users"
  choice is gone. The state directory, the token's file permissions and the
  logon task all assume one signed-in user, so all-users was an untested shape
  — one a real user picked and got stranded on. An existing all-users install
  is detected and the installer asks for it to be removed first.

- **Settings now says where configuration lives on the build you are running.**
  It used to say "put it in `backend/.env`" and "restart the backend", neither
  of which names anything that exists on an installed machine. An installed
  build reads `%USERPROFILE%\.kurukuru\kurukuru.env`, and the screen says so.

- **`kurukuru doctor` warns when `KURUKURU_QEMU_SYSTEM_BINARY` or
  `KURUKURU_QEMU_IMG_BINARY` is set**, because an override replaces the
  bundled-QEMU resolution. The 0.1.0 release note told people to set exactly
  these; that workaround is for **0.1.0 only** and on a later build replaces a
  correct answer with a hardcoded path — on an all-users install, with one that
  did not exist. The docs now say so where the workaround appears.

### Added

- **`kurukuru restart`**, and a **Restart Kurukuru** Start Menu entry. Settings
  are read once at startup; an installed user had no terminal, no visible
  process, and no answer but signing out and back in. This is the second user
  who needed one.

- **A configuration file for installed builds**, at
  `%USERPROFILE%\.kurukuru\kurukuru.env`. Environment variables still win over
  it, which is how `KURUKURU_STATE_DIR` has to be set — it decides where the
  file is read from, so it cannot be set inside it.

### Performance

Two probes were being paid for far more often than necessary. Measured on the
reference Windows host, against the bundled QEMU 11.1.0:

| | before | after |
|---|---|---|
| `is_available()`, repeated | ~110 ms and 2 processes **per call** | 2 processes per 5 s window; 200 calls in 0.02 ms |
| acceleration probe, per backend start | 6.13 s | 0.10 s once measured |

The engine availability check was reached by five endpoints, several of them
polled, and a user's log showed `qemu-system-x86_64 --version` starting several
times a second with the dashboard open. It is now cached for five seconds —
short enough that a user who has just fixed the cause sees the engine recover
without restarting anything.

The acceleration probe was 6.13 s of an 8.4 s cold start, and the reason is its
shape: QEMU started with `-S` never exits, so the *timeout* is the success
signal. A working accelerator is the slow answer and a broken one is instant,
so **only a success is cached** — keyed on the binary's identity and version,
cleared when a launch fails, and expiring on its own after two weeks. A host
that gains an accelerator still speeds up by itself, with no cache to find.

## [0.1.1] — 2026-09-10

### Fixed

- **The bundled QEMU was unreachable, so a fresh install did nothing.** The
  Windows installer copies a trimmed, hash-verified QEMU into `{app}\qemu`, and
  the README promises nothing else is needed first — "not Python, not QEMU".
  Nothing pointed at it. The installer puts `{app}` on PATH so the `kurukuru`
  command works, but not `{app}\qemu`, and the settings defaulted to the bare
  names `qemu-system-x86_64` and `qemu-img`, which resolve through PATH.

  On a machine that already had QEMU installed this worked, because PATH found
  *that* copy — which is why it survived every test: the bundled binaries were
  never the ones being run. On a fresh machine the engine reported unavailable
  with 25 MB of working QEMU sitting unused beside the executable, and because
  the base image is only fetched while provisioning an instance, it also never
  downloaded. **Anyone installing 0.1.0 without QEMU already present got a
  non-working install.** Found by the first real external install.

  Settings now prefer a QEMU bundled beside the executable and fall back to
  PATH, so a checkout is unaffected and an explicit `KURUKURU_QEMU_*_BINARY`
  still wins over both.

### Changed

- Settings now says "base image downloads on first launch" rather than "base
  image not downloaded yet". The image is fetched the first time an instance
  launches, so on a fresh install the old wording described normal state in the
  language of a fault — to exactly the person hunting for a reason nothing works.

## [0.1.0] — 2026-09-09

First public release. Kurukuru manages QEMU virtual machines on a single
machine, through a web dashboard and a CLI, on Windows and Linux.

### Added

- **Instances.** Create, start, stop, restart and delete VMs from cloud images
  or ISO media. Flavors describe CPU, memory and disk as a named size.
- **Console.** In-browser VNC access to any running guest, authenticated with
  single-use tickets rather than the session cookie.
- **Networking.** User-mode (SLIRP) networking with port forwards you can add
  and remove while an instance runs.
- **Cloud-init.** User-data and SSH key injection for cloud images, with
  generated or imported keypairs.
- **Projects.** A way to group instances, networks and keys for organisation.
- **Storage.** ISO library management and qcow2 disk handling through
  `qemu-img`, with the toolchain verified at startup rather than assumed.
- **Authentication.** Password login, API tokens, CSRF protection, and secrets
  written with an OS-level ACL restricting them to your account.
- **Windows installer.** A signed-by-nobody but self-contained installer that
  bundles the backend, the dashboard and a service wrapper.
- **A warning when hardware acceleration is missing**, printed by `launch`
  itself. The backend already detected this and logged it, but the log is a
  different process from the terminal the user is watching, so the whole
  symptom was a boot that took minutes instead of seconds with nothing said
  and no error string to search for.
- **`kurukuru doctor`.** Reports the toolchain actually resolved — QEMU, the
  accelerator, the state directory and its filesystem — because most problems
  on this kind of tool are environmental.
- **Continuous integration.** Every gate runs on Windows with real QEMU, plus
  the frontend suite and an icon-artwork check, on a machine that is not the
  author's.

### Fixed

Between the first tag and this release, three defects were found by moving the
checks off the development machine. They are listed because they were live:

- **The console failed on every upgraded install.** `POST /console/ticket`
  answered 500 wherever the database predated the `credential_version` column,
  which the additive migrations never covered. Every test passed throughout,
  because tests build their schema with `create_all` and so always had the
  current one.
- **`harden_file` reported a protection it had not applied.** It stripped
  inherited ACEs and assumed that left the file bare. On an administrator
  account — the ordinary case on a personal Windows machine — a new file also
  carries explicit entries from the token's default DACL, so SYSTEM,
  Administrators and OWNER RIGHTS survived while the function returned success.
  The DACL is now emptied explicitly and read back before any claim is made.
- **A route-coverage test asserted a property of the checkout.** It required the
  dashboard's SPA fallback to be registered, which only happens when a built
  bundle is present — so it failed on any tree that had not run `npm run build`.

### Known limitations

Stated here because they are design boundaries, not oversights. The full threat
model is in [docs/SECURITY.md](docs/SECURITY.md).

- **No authorization.** Every authenticated account can do everything. Projects
  organise resources without isolating them.
- **Loopback by default.** Binding beyond `127.0.0.1` exposes the whole API to
  your network. Supported, documented, and your decision.
- **Single machine, single user.** There is no clustering, no remote host
  support, and no multi-tenancy.
- **The installer is unsigned.** Windows SmartScreen will warn on first run.
  See the README for what the warning says and how to proceed.

[0.1.1]: https://github.com/Paul-Nwokolo/kurukuru/releases/tag/v0.1.1
[0.1.0]: https://github.com/Paul-Nwokolo/kurukuru/releases/tag/v0.1.0
