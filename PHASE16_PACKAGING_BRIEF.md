# PHASE 16 BRIEF — Rebrand & Packaging (Windows first)

Read docs/ARCHITECTURE.md, docs/DECISIONS.md and CONTRIBUTING.md first.

This phase turns the project into something a stranger can download and
run. Two halves: the rename, and the installer. Windows first; structure
everything so a Linux target slots in without rework, the way the engine
abstraction did.

**The rename touches live user data — including yours.** Treat Part A as
a data-migration phase that happens to involve strings, not a
find-and-replace. Nothing may orphan an existing install.

---

## Naming (decided)

- Product: **Kurukuru**
- CLI command: **kurukuru** (`kk` is taken on PyPI; do not use `kuru` —
  it collides with a disease name. Document an optional
  `alias kk=kurukuru` rather than shipping a short binary name.)
- PyPI distribution: `kurukuru` (verified available)
- Etymology, which belongs in the README and matters for a reason beyond
  charm: *kurukuru* is Yoruba for fog/cloud. There is a live Nintendo
  registration for "KURUKURU KURURIN" in Class 009 scoped to video game
  programs. We are not in that category and must never look like we are:
  no pixel-art or retro-game styling, no spinning-character imagery, and
  the mark should generally appear with its descriptor
  ("Kurukuru - local cloud infrastructure") rather than bare.

---

## BLOCKER - decide before building the installer

**The license.** The repo has none, which means all rights reserved. An
installer ships a licence. Recommend one with reasoning if the owner has
not chosen - Apache-2.0 is the standing suggestion (permissive, patent
grant, corporate-friendly, compatible with an open-core tier later).
Add `LICENSE`, headers where conventional, and the SPDX identifier in
package metadata. Do not proceed to Part C without this settled.

---

## PART A - Rename, safely

### The renames
| Thing | From | To |
|---|---|---|
| Display name | Local IaaS Orchestrator | Kurukuru |
| CLI | `iaas` | `kurukuru` |
| State dir | `~/.local-iaas` | `~/.kurukuru` |
| Env prefix | `IAAS_` | `KURUKURU_` |
| Python package | `app` | keep or rename - recommend |
| DB filename | `iaas.db` | recommend: keep or rename |

`CLI_NAME` already centralises the command name and there is a
`check-product-name` guard - use both. Extend the guard to cover every
new literal introduced here.

### The migration is the hard part
An existing install has instances, VM disks, keys, cloud-init files,
ISOs, backups and a database under `~/.local-iaas`, and runtime files
that record absolute paths. Requirements:

- On startup, detect the old state dir and migrate: move the directory,
  rewrite any persisted absolute paths (instance runtime files reference
  disk, seed and nvram paths), and leave a marker so it runs once.
- Take a pre-migration database backup using Phase 14 Part A's
  machinery - this is exactly what it was built for.
- **A running VM holds its disk open.** Phase 13 measured that Windows
  refuses to rename a directory containing an open file. Detect running
  instances and refuse to migrate with a clear message telling the user
  to stop them first, rather than half-moving the tree.
- Old `IAAS_*` env vars: honour them with a deprecation warning for one
  release, or refuse with a clear message. Recommend which; silently
  ignoring them would send a user's configured paths to a default.
- Test the migration the way `test_migrations.py` does: build a real
  old-layout tree, migrate it, assert every file arrives and every
  persisted path is rewritten, and assert idempotency on a second run.

Verify against a copy of the real install before touching the real one.

---

## PART B - One process, one origin

Today: `uvicorn` on 8000 plus Vite on 5173. Shipped, that becomes one
service serving both.

- The backend serves the built frontend as static files, with SPA
  fallback so deep links work (`/instances/{id}` must resolve).
- API and dashboard become same-origin. This changes the CORS surface:
  narrow `allow_origins` to nothing needed in production, keep the dev
  configuration separate and explicit.
- **Do not weaken CSRF.** Decision 45 established that SameSite does not
  protect across localhost ports because port is not part of a site, so
  same-origin does not make the CSRF token redundant. Confirm the
  measurement still holds in the packaged shape and say so.
- Default port: 8000 is heavily contended. Recommend a less common
  default, and handle "port already in use" with a clear message rather
  than a stack trace.
- Bind to loopback by default. Binding elsewhere must be a deliberate,
  documented choice with a warning.

---

## PART C - The Windows installer

### What ships
- The backend and its Python runtime. The user must not need Python
  installed. Assess and recommend: PyInstaller (onedir), an embedded
  CPython distribution, or Nuitka. Weigh startup time, antivirus false
  positives (a real problem for PyInstaller onefile), size, and how
  `subprocess` calls to QEMU behave from a frozen app. Prototype enough
  to have evidence, not just documentation.
- The built frontend.
- **QEMU, pinned and bundled.** Ship a specific tested version - never
  "latest", never an in-app upgrade path. Record the pinned version in
  the version-awareness range built in Phase 13. Note the current dev
  build (`v11.1.0-12130-g...`) is a snapshot, not a tagged release:
  recommend what to pin. QEMU is GPLv2 and invoked as a separate
  process; state the licensing posture and include its licence text.
- The bundled QEMU must be found via config, not PATH - Phase 13's
  `qemu-img` shadowing incident is exactly this failure.

### Installer behaviour
- Inno Setup or WiX (recommend, with reasoning). Per-user install by
  default so no elevation is needed; elevation only if genuinely
  required, and say what for.
- Install path must avoid Windows' 260-character limit trap documented
  in `docs/PORTABILITY.md`.
- Start Menu entry, optional desktop shortcut, `kurukuru` on PATH.
- Run the backend as a background service or a startup task -
  recommend which. A user should not need a terminal open to use the
  dashboard.
- First run after install: the user has no account. The host-local
  `auth init` flow must be reachable without a terminal, or the installer
  must run it. Recommend a design; a default credential is not an option.
- **Uninstall must not delete user data by default.** VM disks, images,
  volumes and the database live in the state dir; removing them silently
  would destroy work. Offer it as an explicit, clearly-labelled choice.
- Upgrade over an existing install must preserve state and re-run
  migrations.

### Signing
Ship unsigned for now. Document the SmartScreen warning honestly in the
README with what the user will see and why. Structure the build so a
signing step can be added later without rework.

---

## PART D - Release mechanics
- Version numbering and a single source of truth for it, surfaced in
  `/health`, the CLI, and the installer.
- A repeatable build script producing the installer from a clean
  checkout. It must be runnable by someone who is not you.
- `docs/INSTALL.md`: requirements, what the installer does, where state
  lives, how to upgrade, how to uninstall, and the SmartScreen note.
- README rewritten for a stranger: what Kurukuru is, the etymology,
  screenshots, install, first launch, and honest limitations (Windows
  guests in progress, no TLS, no roles, single host).

---

## Cross-cutting
- Linux is out of scope to *ship*, but nothing may be structured so that
  adding it later means starting over. Where you make a Windows-specific
  choice, note what the Linux equivalent would be.
- All suites green; exit codes read directly.
- Commit per part on a branch; push.
- Live verification: install from the built installer on this machine as
  if a new user, reach the dashboard, create an account, launch a VM,
  then upgrade over it and confirm state survives.

## Report
Per part: what shipped, live evidence, deviations. Bring recommendations
BEFORE implementing for: the licence, the Python packaging approach, the
installer toolchain, the service/startup model, the first-run design, the
default port, and what QEMU version to pin.

## Out of scope
macOS and Linux installers, code signing, an auto-update mechanism, a
website, telemetry.
