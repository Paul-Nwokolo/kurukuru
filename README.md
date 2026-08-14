# Local IaaS

A local cloud platform for a single machine. It gives you an HTTP API and a web
dashboard for the whole VM lifecycle — provision, start, stop, terminate — on
top of QEMU, with a disk image catalog, cloud-init provisioning, a browser
console, and sizing bounded by what the host can actually spare. It is aimed at
people who want EC2-shaped workflows (launch by API, pick a size, get an SSH
command back) without a cloud account or a hypervisor GUI.

![The instances list in the dashboard's dark theme](docs/images/dashboard.png)

## What works today

### Platform support

| Platform | Status |
|---|---|
| **Windows 11** + Windows Hypervisor Platform | Validated. The reference platform |
| **Ubuntu 24.04** (software emulation) | Validated: install, full VM lifecycle, SSH, console, ISO boot, image import, the CLI, and the test suite all pass |
| **Ubuntu 24.04** + KVM | **Untested.** The code selects KVM and `-cpu host`, and neither has ever run |
| **macOS / HVF** | **Untested.** Code paths exist; nobody has run them |

The Linux validation ran on a host with no hardware virtualization available
(a cloud guest without nested virt), so everything there ran under TCG software
emulation — boots take minutes rather than seconds. That is a property of the
test host, not of Linux. What it means in practice is that the KVM-specific
choices are still unexercised: **acceleration under `/dev/kvm`, `-cpu host`,
and whether the console renders on the default `std` display under KVM.**

What that run did settle, and what it found — including three defects that were
never platform-specific and had survived nine phases on a developer machine —
is in [docs/PORTABILITY.md](docs/PORTABILITY.md).

- **VM lifecycle over HTTP** — `POST /instances` returns 202 immediately and
  provisions in the background; start, stop and terminate follow.
- **Custom sizing, capacity-aware** — CPUs, memory and disk are per-instance
  numbers. Presets (small/medium/large) fill them in. Requests are checked
  against *allocatable* capacity and refused with the arithmetic if they exceed
  it.
- **Cloud-init provisioning** — the orchestrator's SSH key is injected into
  cloud images through a NoCloud seed ISO, so a new instance is SSH-ready.
- **Browser console** — noVNC over a WebSocket bridge in the backend. No
  websockify process.
- **ISO boot** — install an arbitrary OS from an ISO onto a blank disk, through
  the console.
- **Image catalog** — register local qcow2/raw/vmdk/vdi files and launch from
  them. Instance disks are copy-on-write overlays, so a new VM costs megabytes,
  not gigabytes.
- **Reconciliation** — the database holds desired state; a background pass folds
  actual hypervisor state back in, so out-of-band changes show up.
- **A command-line client** — `iaas launch`, `ls`, `ssh`, `rm`, `doctor`, with
  `--json` on every read command and a documented exit-code table. See below.
- **SSH key pairs** — import your own or generate one, and choose which go on an
  instance. The orchestrator's key remains the default.
- **Custom user-data** — supply your own cloud-config; it is merged with the
  generated one rather than replacing it, so you keep your login.
- **Snapshots** — capture a stopped instance's disk and restore it later.
- **Port forwards** — expose any guest port on a host port, added and removed
  live on a running instance.
- **Volumes** — extra disks that outlive the instances they attach to.
  Terminating a VM detaches its volumes and keeps the data.
- **Instance detail view** — one page per instance: access, key pairs,
  snapshots, the user-data it was built with, and what has happened to it.
- **Projects** — group instances, images and key pairs so the dashboard is not
  one flat list. Organisational only: there is no auth, so a project filters a
  view and is never an access boundary.
- **Event log** — an append-only history per instance and across the install.
  Records what the rows cannot: every stop and start rather than the last one,
  every error rather than the most recent, restores (which change no row at
  all), and corrections the reconciler made on its own.

Not built: authentication, multi-host, golden-image capture from a running VM,
bridged and host-only networking (both need elevation or a component this
project does not ship — see DECISIONS #24), live resize, volume snapshots. Snapshots require the instance to be stopped —
see [DECISIONS #15](docs/DECISIONS.md) for the measurements behind that. See
[Project status](#project-status).

## Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.11+ | Tested on 3.13.7 (Windows) and 3.12.3 (Ubuntu 24.04) |
| Node.js | 18+ | Tested on 24.15.0. Only needed for the dashboard |
| QEMU | 8.0+ | Tested on 10.0.94 (Windows) and 8.2.2 (Ubuntu) |
| Accelerator | WHPX on Windows · KVM on Linux · HVF on macOS | Optional but strongly wanted; without it VMs fall back to software emulation and boot roughly 30× slower |

`qemu-system-x86_64` and `qemu-img` must be on `PATH`, or pointed at explicitly
(see [Configuration](#configuration)).

**Linux prerequisites.** Ubuntu ships none of these by default, and the venv
step fails confusingly without the first:

```bash
sudo apt install python3.12-venv qemu-system-x86 qemu-utils
```

On Fedora/RHEL that is `qemu-kvm` and `qemu-img`; on Arch, `qemu-full`. To use
KVM, the account running the backend must be able to read `/dev/kvm` — add it
to the `kvm` group and log back in. `iaas doctor` reports which accelerator was
selected and why.

**Disk:** the Ubuntu base image is ~600 MB, downloaded once on first QEMU
launch. Each instance adds a sparse overlay — a few hundred MB in practice, but
it can grow to the disk size you asked for. Budget a few GB.

**Memory:** 2 GB is held back for the host by default; whatever remains is what
instances can be given.

### Enabling Windows Hypervisor Platform

```powershell
# Elevated PowerShell. Requires a reboot.
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
```

## Command line

If you live in a terminal, the CLI is the primary interface — the dashboard is
the optional part. Installing the backend as an editable package puts `iaas` on
`PATH`:

```bash
pip install -e backend
iaas doctor          # checks QEMU, the accelerator, disk, keys — with remedies
iaas serve           # run the backend in the foreground
```

```bash
iaas launch web-01 --preset small --wait
iaas ls
iaas ssh web-01 -- uname -a
iaas rm web-01 --yes
```

```
NAME    STATUS   ADDRESS         SIZE      SOURCE                    AGE
web-01  Running  127.0.0.1:2200  1c/1G/5G  Ubuntu 24.04 LTS (cloud)  41s
web-02  Running  127.0.0.1:2201  2c/2G/8G  Ubuntu 24.04 LTS (cloud)  18s
```

It is an API client — the same HTTP API the dashboard uses, no privileged path
of its own. `--json` on every read command emits parseable JSON and nothing
else (progress goes to stderr), exit codes distinguish *not found* from
*conflict* from *timed out*, and nothing prompts when stdout is not a terminal.

Full reference: [docs/CLI.md](docs/CLI.md).

## Quickstart

### Backend

```powershell
# PowerShell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
# bash / zsh
cd backend
python3 -m venv .venv           # Ubuntu ships no bare `python`; see prerequisites
source .venv/bin/activate       # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The API is then on <http://localhost:8000>, with interactive docs at
<http://localhost:8000/docs>.

### Frontend

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

The dashboard is on <http://localhost:5173>.

### Configuration

Every setting is overridable by environment variable with an `IAAS_` prefix, or
by a `.env` file in `backend/`. Copy the annotated template:

```powershell
cp backend/.env.example backend/.env
```

Three things worth knowing about how `.env` is read:

- The prefix is `IAAS_`, uppercased — `qemu_dir` becomes `IAAS_QEMU_DIR`.
- The file is decoded as `utf-8-sig`, so a UTF-8 BOM (which Windows editors like
  to add) is tolerated.
- Unknown keys are **ignored**, not rejected. Leaving a stale setting behind is
  harmless, but a typo will also be silently ignored — if an override seems to
  have no effect, check the spelling first.

## First launch

1. Open <http://localhost:5173>. The header shows whether the backend is
   reachable.
2. Click **Launch Instance**. Pick **Quick launch**, give it a name
   (lowercase letters, digits and hyphens; must start with a letter), and pick a
   size. The **Size** row shows live host capacity.
3. Click **Launch**. The row appears as `Pending`, then `Provisioning`, then
   `Running` — typically about 30 seconds with hardware acceleration. The first
   launch on a fresh install is slower because it downloads the base image.
4. **To SSH in:** click the copy icon next to the instance's address. That copies
   a complete command including `-i` and the orchestrator's private key:

   ```
   ssh -i "C:\Users\you\.local-iaas\keys\id_ed25519" -p 2200 iaas@127.0.0.1
   ```

   Paste it into any terminal. QEMU guests are reached through a loopback port
   forward, which is why the port is part of the address.

5. **To use the console:** click **Console** on a running instance. This is the
   only way into ISO instances and images without cloud-init, which get no SSH
   key.

To install an OS from an ISO: drop a `.iso` into `~/.local-iaas/isos/`, then
choose **Install from ISO** in the launch dialog and pick it from the list.

## Troubleshooting

Each of these is a failure that actually happened during development.

### `HypervisorUnavailableError` / "QEMU binary not found"

`qemu-system-x86_64` is not on `PATH`. Either add it, or point at it directly:

```powershell
$env:IAAS_QEMU_SYSTEM_BINARY = "C:\Program Files\qemu\qemu-system-x86_64.exe"
$env:IAAS_QEMU_IMG_BINARY    = "C:\Program Files\qemu\qemu-img.exe"
```

`GET /health` reports the engine's own view, including the detected version —
check there first.

### `python -m venv .venv` fails with an `ensurepip` error

Same message, two completely different causes — check which platform you are on
before chasing the wrong one.

**On Linux**, the `python3-venv` package is missing and the error says so:
`apt install python3.12-venv`. (Also note `python` does not exist on Ubuntu —
use `python3`.)

**On Windows**, the checkout is too deep. The 260-character path limit bites
during venv creation, and the error names `ensurepip` rather than the real
cause. Clone somewhere shorter (`C:\src\local-iaas`), or enable long paths:

```powershell
# Elevated PowerShell.
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
  -Name LongPathsEnabled -Value 1
```

### VMs are extremely slow to boot

Hardware acceleration is not active, so QEMU is emulating in software. Run
`iaas doctor`, or check `GET /health`: `accel` will read `tcg` rather than
`whpx`/`kvm`/`hvf`.

- **Windows:** enable Windows Hypervisor Platform (above) and reboot.
- **Linux:** confirm `/dev/kvm` exists and that you can read it — if it is
  missing, the host has no virtualization support, it is disabled in
  firmware, or the machine is itself a VM without nested virt. If it exists but
  is unreadable, add your account to the `kvm` group and log back in.

For scale: a cloud image boots in ~23 s accelerated, and 4–5 minutes emulated on
a 2-core host. If you are stuck on software emulation, raise
`IAAS_QEMU_SHUTDOWN_TIMEOUT_SECONDS` too — a graceful stop was measured at 89 s
against the 90 s default, and exceeding it force-kills a guest mid-shutdown.

### The console is black on a cloud image

This is expected on standard graphics with hardware acceleration, and it is not
a bug in the console. QEMU's VGA emulation depends on memory dirty-tracking that
WHPX does not provide, so a guest that stays in **VGA text mode** — notably the
Ubuntu cloud image, which never binds a display driver — renders nothing.

Guests that switch to a framebuffer (any ISO installer worth the name) are
unaffected and render fine.

The fix is to launch with **Modern graphics** (`display: "virtio"`), under
*Advanced* in the launch dialog. The instance row's Console tooltip says this
too. Software emulation also works, but costs far more.

### A launch is refused with a 422 about capacity

The request exceeded what the host can currently allocate. The message contains
the arithmetic, for example:

```
Requested 60000 MB but only 13780 MB allocatable
(2048 MB host reserve, 0 MB committed to running instances).
```

Stop or terminate an instance to free the commitment, ask for less, or lower
`IAAS_HOST_RESERVE_MEMORY_BYTES` if you genuinely want to cut into the host's
share. `GET /host/capacity` shows the same numbers.

### An instance is Running but shows no address, marked Degraded

The VM booted but never published an IP after 90 seconds. Usually cloud-init
failed or the guest's network did not come up. Open the console to look, or read
the guest's serial log at:

```
~/.local-iaas/qemu/instances/<name>/qemu.log
```

### Where logs live

- **Backend:** stdout of the `uvicorn` process.
- **Guest serial console:** `~/.local-iaas/qemu/instances/<name>/qemu.log`
- **QEMU's own stderr:** `~/.local-iaas/qemu/instances/<name>/qemu-process.log` —
  this is where a VM that dies instantly explains itself.
- **Instance runtime state:** `~/.local-iaas/qemu/instances/<name>/runtime.json`
  (pinned ports, pid, accelerator, display).

## Project status

Early, and honest about it. It runs real VMs and has been used to do real work,
but:

- **Single host.** No clustering, no remote hypervisors.
- **No authentication.** None. Every endpoint is unauthenticated, and anyone who
  can reach port 8000 can create, destroy and open a console into your VMs.
  **Do not expose this on a shared or untrusted network.** It is bound to
  localhost by default; keep it that way until authentication exists.
- **Windows-validated only.** Other platforms are unexercised.
- **SQLite, single process.** Fine for one machine; not a multi-writer design.

### Known gaps, deliberately deferred

Raised in review, understood, and not done yet. Listed here rather than left in
a comment thread, because a known gap that is not written down is
indistinguishable from one nobody noticed.

- **No backup before a migration.** `init_db()` alters the schema in place on
  every startup, and the changes so far are additive and idempotent — but
  "additive" is a property of the migrations written so far, not a guarantee
  about the next one. A copy of the database taken before the first altering
  statement of a run would make any of them reversible. Related: the one-time
  relocation in `app/database.py` already refuses to write over a database
  that has rows in it, so that path is covered; ordinary migrations are not.
- **Reconciling is hard to reach and hard to read.** Reconciliation is the
  mechanism that makes the dashboard agree with the hypervisor, and it is
  currently a single unlabelled icon in the header that reconciles
  *everything*. There is no way to ask for one instance, and the result — what
  it checked, what it changed — is a sentence that appears for four seconds.
  For a control plane whose whole claim is "this matches reality", that is too
  little affordance and too little evidence.

Guest-facing surfaces are bound to `127.0.0.1` by design — VNC, QMP and the SSH
port forward — so the console proxy is the only path to a VM's screen. That is a
deliberate invariant with a test guarding it, not an accident of configuration.

### Non-goals

Not a VirtualBox or VMware replacement, not a container runtime, not a
multi-tenant cloud. The target is a single-machine platform with cloud-shaped
ergonomics.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — layering, state machine, reconciler,
  QEMU specifics, and how to add an engine.
- [API reference](docs/API.md) — every endpoint, generated against the live
  OpenAPI schema.
- [CLI reference](docs/CLI.md) — install, configuration, every command, the
  exit-code table and the scripting contract.
- [Portability](docs/PORTABILITY.md) — where the code assumed Windows, what was
  fixed, and what a Linux host still has to confirm.
- [Decisions](docs/DECISIONS.md) — why things are the way they are.
- [Contributing](CONTRIBUTING.md) — running the tests and the conventions in use.
- [History](docs/history/) — the phase briefs this was built from.

## License

**TODO — not yet chosen.** Until a license is added, no permission to use, copy
or redistribute is granted.

QEMU is invoked as a separate process via its command line; no QEMU code is
linked into this project. That keeps QEMU's GPL obligations at the process
boundary, the same posture UTM and Podman take. This note is a description of
the technical arrangement, not legal advice.
