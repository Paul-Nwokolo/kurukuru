# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is `0.x`, breaking changes may land in a minor release; they
will always be listed under **Changed** with the migration path.

Design decisions behind these changes are recorded in
[docs/DECISIONS.md](docs/DECISIONS.md), and what is planned next is in
[docs/ROADMAP.md](docs/ROADMAP.md).

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

[0.1.0]: https://github.com/Paul-Nwokolo/kurukuru/releases/tag/v0.1.0
