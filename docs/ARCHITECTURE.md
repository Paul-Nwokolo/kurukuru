# Architecture

## Layering

```
React dashboard (Vite, TypeScript)      kurukuru CLI (Typer)   kurukuru/cli/
        │  HTTP + one WebSocket                 │  HTTP
        └──────────────────┬────────────────────┘
                           ▼
FastAPI control plane          kurukuru/main.py, kurukuru/routers/
        │
        ├── SQLite (desired state)      kurukuru/models.py, kurukuru/database.py
        │
        ▼
ComputeEngine (ABC)            kurukuru/engines/base.py
        │
        ▼
QemuEngine                     kurukuru/engines/qemu.py
        │  subprocess + QMP over localhost TCP
        ▼
qemu-system-x86_64 processes
```

Everything above `ComputeEngine` is hypervisor-agnostic. The routers, the
models, the reconciler and the frontend do not know what a qcow2 file is.

The CLI ships from the backend package (that is how `pip install -e backend`
puts `kurukuru` on `PATH`) but is not part of the control plane: it imports no
router, model, session or engine, and reaches the system only over HTTP. See
[decision 11](DECISIONS.md) and [CLI.md](CLI.md).

### Why the engine seam exists

It is not speculative generality — it has already been exercised in both
directions. A Multipass engine was added alongside QEMU in Phase 5 and retired
in Phase 7. Retiring it meant deleting one module (`kurukuru/engines/multipass.py`)
and one entry in a factory dict. No router, model or reconciler code changed.

That is the practical test of the abstraction, and it is why the registry and
the per-row `engine` column survive even though only one driver ships today.

## Module map

| Path | Responsibility |
|---|---|
| `kurukuru/main.py` | App wiring, lifespan, system endpoints (`/health`, `/diagnostics`, `/engines`, `/host/capacity`, `/flavors`, `/isos`, `/ssh-key`), background reconcile loop |
| `kurukuru/routers/instances.py` | Instance lifecycle, sizing resolution and validation, the reconciler, the console WebSocket route |
| `kurukuru/routers/images.py` | Image catalog and async import |
| `kurukuru/models.py` | SQLModel tables and API schemas — one hierarchy serves both, so the DB and the API contract cannot drift |
| `kurukuru/database.py` | Engine, WAL pragma, additive migrations, pre-migration backups |
| `kurukuru/product.py` | The names — product, command, env prefix, state dir, database leaf — plus what each was called before Phase 16 and the `IAAS_*` env shim. No dependencies, so both the backend and the CLI import it downward |
| `kurukuru/config.py` | `pydantic-settings`; every knob, `KURUKURU_`-prefixed |
| `kurukuru/state_migration.py` | Moves a pre-Phase-16 `~/.local-iaas` to `~/.kurukuru` once: refuses while VMs run, backs the database up first, renames it, and repoints every persisted absolute path — runtime files, database columns, and qcow2 backing headers |
| `kurukuru/host_capacity.py` | psutil probes plus the allocatable arithmetic |
| `kurukuru/console.py` | VNC↔WebSocket byte pump |
| `kurukuru/cloud_init.py` | Renders the `#cloud-config` document |
| `kurukuru/ssh_keys.py` | The orchestrator's single ed25519 keypair |
| `kurukuru/image_store.py` | `qemu-img` probing, import copy, image paths |
| `kurukuru/isos.py` | Boot-media listing and traversal-safe path resolution |
| `kurukuru/engines/` | `base.py` (ABC), `qemu.py` (driver), `qmp.py`, `seed.py`, `images.py`, `ports.py`, `process.py` |
| `kurukuru/cli/` | The `kurukuru` command. `main.py` (root app, error boundary), `client.py` (the only route to the system), `naming.py` (re-exports the names from `kurukuru/product.py`), `config.py`, `output.py`, `formats.py`, `support.py`, `commands_*.py` |

## Instance state machine

Six states, defined in `InstanceStatus`:

```
                POST /instances
                      │
                      ▼
                  ┌────────┐
                  │Pending │
                  └───┬────┘
                      │ provisioning job starts
                      ▼
              ┌──────────────┐
              │ Provisioning │───────────┐ launch fails
              └──────┬───────┘           │
                     │ launch returns    │
                     ▼                   ▼
                ┌─────────┐  stop   ┌─────────┐
                │ Running │◄───────►│ Stopped │
                └────┬────┘  start  └────┬────┘
                     │                   │
                     │  VM vanished      │
                     ▼                   │
                 ┌───────┐               │
                 │ Error │               │
                 └───┬───┘               │
                     │                   │
                     └────────┬──────────┘
                              │ DELETE (from any state)
                              ▼
                       ┌────────────┐
                       │ Terminated │  (terminal)
                       └────────────┘
```

Who performs each transition:

| Transition | Performed by |
|---|---|
| → `Pending` | `POST /instances`, synchronously, before returning 202 |
| `Pending` → `Provisioning` | The background provisioning job |
| `Provisioning` → `Running` | The provisioning job, after the launch call returns *and* the instance reports ready |
| `Provisioning` → `Error` | The provisioning job, on cloud-init failure, bad launch options, or a hypervisor error |
| `Running` ↔ `Stopped` | `POST /{id}/start` and `/{id}/stop`, synchronously |
| any → `Error` | The reconciler, when the hypervisor no longer knows the VM |
| any → `Terminated` | `DELETE /{id}` |

`Terminated` is the only terminal state. `Error` is deliberately not terminal —
a row can recover if the hypervisor comes back, and terminate still works from
it.

**Readiness differs by guest.** A cloud image is ready when its SSH port answers
a banner. An ISO guest has no injected key and no promised network service, so
it is ready as soon as QEMU is running and QMP responds. Waiting for SSH on the
latter would time out on a VM that is working perfectly.

**Degraded** is not a stored status. It is derived in `InstanceRead`: `Running`,
with `ssh_enabled`, without an address, for longer than
`KURUKURU_DEGRADED_AFTER_SECONDS`. Console-only instances are excluded, because
having no address is how they are supposed to work.

## The reconciler

The database is the *desired* state. The hypervisor is the truth. The reconciler
folds the second into the first.

It runs on a timer (`KURUKURU_RECONCILE_INTERVAL_SECONDS`, default 30s) and on
demand via `POST /instances/refresh`. Without a recurring pass, anything missed
in the single post-launch sample stuck permanently, and changes made outside the
API never appeared at all.

Each pass is three phases, and the split is the point:

1. **Decide** which engines to ask, from a cheap read of the live rows.
2. **Ask** — list every instance from each engine. This is the slow part:
   seconds normally, up to a full timeout when a hypervisor is wedged. **No
   transaction is held open here.**
3. **Read, decide, write** in one boundary. Rows are re-read *after* the I/O,
   then updated and committed once.

Reading rows after the slow phase is what makes the write safe. A terminate that
lands mid-pass is already visible by the time phase 3 selects, so a stale
snapshot cannot overwrite it. An earlier version held rows across the listing
and patched the race with a per-row re-read; that was replaced by moving the
boundary.

Two more rules the pass obeys:

- A row whose engine has no driver (a legacy `multipass` row) is **skipped**, not
  errored. Treating "no driver" as "VM missing" would rewrite history to claim
  those instances failed.
- A launch in flight belongs to the provisioning job. A background pass will not
  promote a `Pending`/`Provisioning` row unless the hypervisor reports it Running
  *with* an address. Only the job — which knows the launch returned — can finish
  the transition for address-less guests.

### Liveness has three states, and QMP serves one client

The reconciler asks two questions about a QEMU instance: is the process alive,
and does its QMP monitor answer. Those are not one question, and collapsing them
caused a real failure.

**QEMU's QMP chardev accepts a single client at a time.** While anything else
holds that socket, a handshake from the backend cannot complete. The original
two-factor check read that as "not running" and the reconciler rewrote a healthy
instance to `Stopped`, clearing its pid — observed as a 40-minute
Running/Stopped flap that ended within one cycle of the competing client
disconnecting.

So `QemuEngine._liveness` returns three states:

| pid alive | QMP answers | QMP port bindable | verdict |
|---|---|---|---|
| no | — | — | `stopped` |
| yes | yes | — | `running` |
| yes | no | no | `unreachable` — something else holds the monitor |
| yes | no | yes | `stopped` — QEMU is gone; this pid is someone else's |

The port is the tie-breaker. QEMU holds its QMP port for as long as it lives, so
a busy monitor still has something bound to it while a dead QEMU has released
it. That distinguishes a contended socket from a *recycled pid*, which was the
reason the QMP check existed in the first place — both concerns are served
rather than traded off.

`unreachable` counts as up: the process exists, and saying otherwise is simply
false. It is carried out of the engine on `InstanceInfo.monitor_reachable`,
persisted by the reconciler, and surfaced as **Degraded** with its own
explanation — not as healthy, and not as stopped.

**This is an operational constraint, not only an internal detail.** Any second
consumer of a VM's QMP socket will produce it: a debugging session with `nc`, a
screendump or console tool, a second backend process pointed at the same state
directory. If instances start reporting a degraded monitor, look for the other
client before looking at the VM.

## Invariants worth stating

- **Name uniqueness is application-enforced**, in `POST /instances`, and scoped
  to non-`Terminated` rows. There is no unique index; there used to be, and it
  made every name unusable forever once its instance was destroyed.
- **Hypervisor calls are only ever reached from a row fetched by primary key.**
  Nothing looks a VM up by name from user input. This is what makes name reuse
  safe: terminating a stale audit row cannot destroy the live VM that reused its
  name (the DELETE route returns early on already-terminated rows).
- **Capacity counts `Pending` and `Provisioning` instances as committed.** Their
  memory is not touched yet, but it is spoken for — otherwise two concurrent
  launches could each pass the check and jointly overcommit. `Stopped` instances
  are *not* counted; they have handed their RAM back.
- **VNC, QMP and the SSH forward bind to `127.0.0.1` only.** A test asserts no
  argument in the launch command line contains `0.0.0.0`. If VNC ever bound a
  wildcard, every VM's screen would be on the LAN with no authentication.
- **Sizing lives on the row, not in a preset reference.** Presets fill numbers in
  at creation; editing a preset later cannot retroactively change what an
  existing instance claims to be.

## QEMU specifics

### Per-instance directory

Everything under `KURUKURU_QEMU_DIR` (default `~/.kurukuru/qemu/`):

```
base-images/
    noble-server-cloudimg-amd64.img     shared backing image, downloaded once
    imported-<uuid>.qcow2               imported images
instances/<name>/
    disk.qcow2          copy-on-write overlay (or a blank disk for ISO boot)
    seed.iso            NoCloud cloud-init volume (cloud images only)
    runtime.json        pinned ports, pid, accelerator, display, ssh_enabled
    qemu.log            guest serial console
    qemu-process.log    QEMU's own stdout/stderr
```

That directory *is* the engine's state. A VM can be found, probed, stopped and
destroyed from the filesystem alone, which is what lets VMs survive a backend
restart.

### Disks

Instance disks are qcow2 overlays over a shared backing image, created with
`qemu-img create -f qcow2 -F qcow2 -b <base>`. A new instance costs the delta,
not a copy. `-F` is passed explicitly because modern `qemu-img` refuses to guess
a backing format.

ISO instances get a **blank** disk with no backing file — an installer needs
empty space, not somebody else's filesystem underneath.

### cloud-init

QEMU has no flag for handing a guest a cloud-config, so it is delivered the way
cloud-init's NoCloud datasource expects: a small ISO labelled `CIDATA`
containing `user-data`, `meta-data`, and a `network-config`. The label *is* the
discovery mechanism. The ISO is built in-process with `pycdlib` — no
`genisoimage`, which does not exist on a stock Windows host.

The `network-config` file exists to work around QEMU's user-mode DNS proxy,
which answers NXDOMAIN for everything on this host while NAT itself works. It
points guests at real resolvers.

### Control

Each VM gets a QMP socket on loopback. `kurukuru/engines/qmp.py` is a ~120-line
blocking JSON-over-TCP client — hand-rolled rather than using the asyncio-first
`qemu.qmp` package, because every caller lives in a synchronous background task.
It supports the three commands the lifecycle needs: `query-status`,
`system_powerdown`, `quit`.

**Liveness is two-factor**: the pid must exist *and* QMP must answer. A pid alone
can be a recycled number; QMP alone cannot distinguish "not booted yet" from
"gone".

### Process handling

VMs are spawned **detached** (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`,
closed std handles) so they survive a `uvicorn --reload` restart. QEMU's
`-daemonize` is POSIX-only and is not used.

`os.kill(pid, 0)` is **not** a liveness probe on Windows — CPython maps it onto
`TerminateProcess` for every signal but two, so the POSIX idiom would kill the VM
it was asked about. `kurukuru/engines/process.py` uses `OpenProcess` +
`GetExitCodeProcess` instead.

### Ports

Three host ports are allocated per instance and pinned for its lifetime: SSH
(2200–2299), QMP (4400–4499) and VNC (5900–5999). Allocation bind-probes for a
free port and also excludes ports recorded in other instances' `runtime.json` —
a stopped VM's ports are unbound but still belong to it. Pinning is what keeps a
copied SSH command valid across a stop/start cycle.

## The console path

```
browser (noVNC)  ──WebSocket──▶  FastAPI  ──TCP──▶  127.0.0.1:<vnc_port>  (QEMU)
```

`WS /instances/{id}/console` accepts the socket, dials the VM's VNC port, and
pumps bytes in both directions with two asyncio tasks until either side closes.

**Nothing parses RFB.** Not one byte is framed, inspected or rewritten. noVNC and
QEMU negotiate versions, encodings and authentication end to end, which makes the
bridge immune to changes in any of that. Reads are bounded at 64 KB so a stalled
browser applies backpressure instead of growing a buffer.

Refusals are reported as WebSocket close codes *after* accepting the socket
(`4404` unknown, `4409` not available, `4502` VNC unreachable). A socket rejected
during the handshake gives the browser nothing to show the user.

VNC is bound to loopback, so this proxy is the only route to a VM's framebuffer.

## Adding an engine

The practical proof the abstraction is real. To add, say, a Hyper-V driver:

1. Write `kurukuru/engines/hyperv.py` with a class implementing `ComputeEngine`
   (`is_available`, `provision_instance`, `start_instance`, `stop_instance`,
   `destroy_instance`, `get_instance_info`, `list_instances`, and optionally
   `describe`). Map the platform's native state vocabulary to `InstanceStatus`
   *inside* the driver — no native state names may leak out.
2. Add one entry to `_FACTORIES` in `kurukuru/engines/__init__.py`.
3. Add the name to `KNOWN_ENGINES` in `kurukuru/models.py`. An assertion at import
   time fails if the two disagree.

That is the whole contract. Routers, reconciler, models and frontend need no
change. Capabilities a driver cannot offer arrive through `LaunchOptions`
(accelerator, ISO path, backing image, display, whether to seed cloud-init) and
drivers are expected to ignore what they cannot honour rather than fail the
launch — except where honouring it is the whole request, in which case refusing
with a clear error is right.

`InstanceInfo` is the return channel: status, address, and the optional runtime
fields (ports, pid, accelerator, display, `ssh_enabled`) that only some engines
have.
