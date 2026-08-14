# PHASE 6 BRIEF — Console, ISO Boot & Image Import (QEMU-only)

Read ROADMAP_v2.md for strategic context. This phase is QEMU-only by
decision: every capability here is one Multipass cannot provide, and the
multipassd daemon on this host has wedged twice (upstream Qt event-loop
bug) — do not gate any work in this phase on Multipass being healthy.
MultipassEngine keeps working for what it already does; it gains nothing
here and must not regress.

## Context
Phases 1-5 DONE. `app/engines/` package with base.py (ABC), multipass.py,
qemu.py, qmp.py, seed.py, images.py, ports.py, process.py; EngineRegistry
dispatches per row. QEMU 10.0.94 on PATH, WHPX acceleration confirmed
(~23s boot). Every QEMU VM already launches with `-vnc 127.0.0.1:<display>`
and a persisted vnc_port — that plumbing exists and is unused. Instance has
ssh_port/vnc_port/qmp_port/pid. 130 tests passing.

This phase is large. Implement in the three parts below IN ORDER, testing
each before starting the next. Part A is the highest-value deliverable — if
time runs short, A shipped and verified beats all three half-done.

---

## PART A — Web Console (noVNC in the dashboard)

### Goal
A "Console" action on Running QEMU rows opens an in-browser screen showing
the VM's actual framebuffer — boot messages, login prompt, TTY — with
working keyboard and mouse.

### Backend
- QEMU's `-vnc` is a raw RFB TCP socket; browsers speak WebSocket. Bridge
  it in FastAPI rather than adding a websockify process:
  `WS /instances/{id}/console` — accept the WebSocket, open a TCP socket to
  127.0.0.1:<vnc_port>, and pump bytes in both directions with two asyncio
  tasks until either side closes. Binary frames, no framing/parsing of RFB.
- Guards: 404 unknown id; 409 if not Running or engine != qemu; close
  cleanly if the TCP connect fails (VM died) with a code the UI can show.
- Bind guarantee: VNC must stay bound to 127.0.0.1 (never 0.0.0.0) — the
  proxy is the only access path. Assert this in the launch args and test it.
- Backpressure: use bounded reads (e.g. 64KB) and let asyncio handle flow;
  don't buffer unboundedly if the client stalls.
- CORS/WS note: the Vite dev server proxies :5173 -> :8000; configure the
  WS path in vite.config so the browser origin stays same-origin in dev.

### Frontend
- Add `@novnc/novnc` (npm). New route/modal: full-screen-ish console panel
  with the VM name, a Ctrl+Alt+Del button, a fullscreen toggle, connection
  status, and a clear "Disconnected" state with Reconnect.
- Wire noVNC's RFB class to `ws(s)://<api-host>/instances/<id>/console`.
  scaleViewport true, clipViewport true; focus the canvas on connect so
  typing goes to the guest.
- The Console action appears only for Running qemu rows (disabled with a
  tooltip explaining why on multipass rows).

### Verify (live, mandatory)
Launch a QEMU VM, open Console, screenshot/describe what renders. Type at
the login prompt and confirm keystrokes reach the guest. Confirm the socket
closes cleanly when the VM is stopped and the UI shows Disconnected.

---

## PART B — ISO Boot (arbitrary guests)

### Goal
Launch a VM from an ISO with a blank disk, install an OS through the
console, and boot it afterwards from disk. This is the "arbitrary guest"
capability — the thing Multipass structurally cannot do.

### Backend
- New settings: iso_dir (default ~/.local-iaas/isos/).
- `GET /isos` lists .iso files present in iso_dir (name, size, mtime).
  Manual file placement is fine for this phase — no upload endpoint yet.
- InstanceCreate gains optional `iso: str | None` (filename within iso_dir;
  reject path traversal — resolve and assert the parent is iso_dir).
- When iso is set (qemu engine only, else 422):
  - create a BLANK qcow2 of the flavor's disk size (no backing file)
  - NO cloud-init seed (a generic ISO won't consume NoCloud) — skip seed
    generation entirely; record on the row that this is an ISO instance
  - args: `-drive file=<iso>,media=cdrom` plus
    `-boot order=dc,menu=on` so CD is tried first, then disk
  - such instances have no orchestrator SSH key: ip_address stays null,
    Copy SSH must be hidden/disabled for them, and Console becomes the
    primary access path (this is the intended UX, not a gap)
- Add `boot_source` to the Instance model ("image" | "iso") + in
  InstanceRead so the UI can branch cleanly.
- Reconciliation for ISO instances is process-based only (pid + QMP), which
  already works — no IP expectations.

### Frontend
- Launch modal: a "Boot from" choice — Cloud image (default) | ISO. Picking
  ISO shows the ISO list from GET /isos and a note: "No SSH key injection;
  use the Console to install and log in."
- Instances table: ISO rows show a small "ISO" tag and a dash for IP.

### Verify (live, mandatory)
Use a small, fast-installing ISO — Alpine Linux standard x86_64 (~60MB) is
ideal; download it into iso_dir. Launch from it, open the Console, and show
the Alpine boot/login prompt rendering. Log in as root in the console.
(A full install-to-disk is NOT required for sign-off — reaching an
interactive prompt from an ISO through the browser console is the proof.
If time permits, `setup-alpine` to disk and reboot into it is the gold
standard; report which you achieved.)

---

## PART C — Image Import (bring your own image)

### Goal
Register user-supplied disk images so they can be launched like the
built-in Ubuntu cloud image.

### Backend
- New `Image` table: id, name (unique among non-deleted), filename,
  format ("qcow2" | "raw" | "vmdk" | "vdi"), source ("builtin" | "imported"),
  virtual_size_bytes, actual_size_bytes, has_cloud_init (bool),
  status (Available | Importing | Error), error_message, created_at.
- Seed one builtin row for the existing Ubuntu 24.04 base image.
- `POST /images/import` body {name, path, has_cloud_init} — registers a
  local file already on disk (copy into base-images/ in a BACKGROUND job:
  status Importing -> Available/Error). Probe with
  `qemu-img info --output=json` to fill format and sizes; reject anything
  qemu-img can't read, with its stderr in error_message.
  (File upload through the browser is out of scope — a path field plus a
  note is correct for a local-first tool and avoids multi-GB uploads.)
- `GET /images`, `GET /images/{id}`, `DELETE /images/{id}` (refuse deleting
  builtin; refuse if any non-Terminated instance references it -> 409).
- InstanceCreate gains optional `image_id`; default = the builtin Ubuntu.
  Overlay creation uses the selected image as backing file. Cloud-init seed
  is generated only when the image's has_cloud_init is true; otherwise skip
  it and treat access like ISO instances (console-first, no SSH string).
- Instance.image_id recorded and shown.

### Frontend
- Sidebar "Images" placeholder becomes real: table (Name, Format, Virtual
  size, Source badge, Status, Actions: Delete with confirm), poll 3s,
  amber pulsing for Importing (reuse StatusBadge).
- An "Import image" modal (name, path, has-cloud-init checkbox) with a hint
  that the path must be readable by the backend process.
- Launch modal: image picker (builtin + imported Available images), shown
  when Boot from = Cloud image.

### Verify (live, mandatory)
Import a second image — simplest honest test: `qemu-img create -f qcow2`
a small blank one, OR re-register a copy of the Ubuntu base under a new
name — confirm it appears Available with correct probed size, launch an
instance from it, and confirm the overlay's backing file is the imported
file (`qemu-img info` on the overlay). Delete it and confirm the 409 guard
fires while an instance still references it.

---

## Cross-cutting requirements
- Migrations: additive columns + new tables via the existing init_db
  mechanism (the _ADDED_COLUMNS / _REDEFINED_INDEXES pattern). The real
  iaas.db must converge without data loss — test it the way
  test_migrations.py already does.
- Every new subprocess call: list args, no shell=True, timeout from
  settings, stderr captured into error_message on failure.
- No Multipass changes. Multipass rows must render and behave exactly as
  before; Console/ISO/import affordances are disabled for them with an
  explanatory tooltip.
- All existing suites stay green; frontend typechecks, lints, builds.

## Report back
For each Part: what shipped, the live verification result (Part A's console
render and Part B's ISO prompt are the headline evidence), any deviations
with reasoning, and the final route list. Note anything you deliberately
left for later.

## Out of scope (Phase 7+)
Golden-image creation from a running instance (qcow2 rebase/commit),
snapshot trees, bridged/host-only networking, guest agent metrics, USB
passthrough, browser file upload for images, SPICE.
