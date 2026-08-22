# Command-line interface

`iaas` is the terminal client for the orchestrator. It talks to the same HTTP
API as the dashboard and has no other access to the system — no database, no
QEMU, no reaching into `~/.local-iaas`. Anything it can do, a script can do.

```
iaas launch web-01 --wait && iaas ssh web-01
```

- [Install](#install)
- [Configuration](#configuration)
- [Commands](#commands)
- [Exit codes](#exit-codes)
- [Scripting](#scripting)

## Install

From the repository root, into the backend's virtualenv:

```bash
pip install -e backend
```

That puts `iaas` on `PATH`. `pip install -r backend/requirements.txt` still
works for a plain backend install; the editable install is what adds the
command.

Check it:

```bash
iaas doctor
```

`doctor` is the first thing to run when anything looks wrong. Every line is
`PASS`, `WARN` or `FAIL`, and every failure comes with the fix:

```
PASS API: Local IaaS Orchestrator at http://127.0.0.1:8000
PASS iaas CLI: v0.1.0 on Python 3.13.7
PASS QEMU: QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)
PASS Accelerator: whpx
PASS Base image: C:\Users\you\.local-iaas\qemu\base-images\noble-server-cloudimg-amd64.img
PASS Instance store: C:\Users\you\.local-iaas\qemu\instances - 35.8 GB free
PASS SSH keypair: C:\Users\you\.local-iaas\keys\id_ed25519
PASS Backend: v0.1.0 on Python 3.13.7
```

`WARN` never fails the run — a host with no accelerator is slow, not broken.
`FAIL` exits 1, and an unreachable API exits 3.

### Shell completion

```bash
iaas completion bash >> ~/.bashrc
iaas completion zsh  > ~/.zfunc/_iaas
iaas completion fish > ~/.config/fish/completions/iaas.fish
iaas completion powershell >> $PROFILE
```

## Configuration

The API URL is resolved in this order, and the first one that answers wins:

1. `--api-url http://host:8000`
2. `IAAS_API_URL` in the environment
3. `~/.local-iaas/cli.toml`
4. `http://127.0.0.1:8000`

```toml
# ~/.local-iaas/cli.toml
api_url = "http://127.0.0.1:8000"
dashboard_url = "http://127.0.0.1:5173"   # only used by `iaas console`
```

There is no authentication anywhere in this project yet, so pointing the CLI at
a remote host means pointing it at an unauthenticated control plane. See the
security note in the [README](../README.md#project-status).

When the API cannot be reached, the CLI says which URL it tried and what to do
about it, and exits 3:

```
error: Cannot reach the API at http://127.0.0.1:8000 (ConnectError).
hint: Is the backend running? Start it with 'iaas serve', or point the CLI at
      another host with --api-url or $IAAS_API_URL.
```

## Commands

Run `iaas --help`, or `iaas COMMAND --help`, for the full option list. Every
read command takes `--json`.

### Instances

#### `iaas launch NAME`

```bash
iaas launch web-01                                  # small preset, returns immediately
iaas launch web-01 --wait                           # ...or block until it is Running
iaas launch big --cpus 4 --memory 8G --disk 40G     # explicit sizing
iaas launch big --preset large --disk 40G           # a preset, with one thing changed
iaas launch alpine --iso alpine-virt-3.21.7-x86_64.iso --disk 8G
iaas launch mine --image "My Ubuntu image" --wait
```

| Option | Meaning |
|---|---|
| `--preset small\|medium\|large` | Starting point for sizing. Anything you set explicitly wins |
| `--cpus N` | vCPU count |
| `--memory` | `2G`, `2048M`, or a plain number of **MB** |
| `--disk` | `20G`, `20480M`, or a plain number of **GB** (rounded up) |
| `--mode quick\|iso\|image` | Intent. Inferred from `--iso`/`--image`; naming it makes a mismatch an error rather than a surprise |
| `--iso NAME` | Boot media, from `iaas isos ls`. No SSH key is injected — the console is the way in |
| `--image NAME_OR_ID` | An image from `iaas images ls`, by name or id |
| `--accel auto\|whpx\|tcg` | Accelerator. `auto` lets the driver choose |
| `--display std\|virtio` | `virtio` is what makes a text-mode guest render on the console |
| `--wait`, `--timeout N` | Poll until Running or Error (default 600s) |
| `--json` | Emit the instance record |

Without `--wait` the command prints the id and exits as soon as the request is
accepted, mirroring the API's 202. With `--wait` it shows a spinner with the
live status and exits non-zero if the instance lands in `Error`, printing the
reason the backend recorded.

Sizing is checked against what the host can actually spare, and a refusal
carries the arithmetic:

```
$ iaas launch too-big --memory 900G
error: Requested 921600 MB but only 10708 MB allocatable (2048 MB host reserve,
       3072 MB committed to running instances).
$ echo $?
6
```

#### `iaas ls`

```bash
iaas ls                 # live instances
iaas ls --all           # including terminated ones
iaas ls --watch         # re-render until Ctrl-C
iaas ls --json
```

```
NAME    STATUS   ADDRESS         SIZE      SOURCE                    AGE
web-01  Running  127.0.0.1:2200  1c/1G/5G  Ubuntu 24.04 LTS (cloud)  41s
web-02  Running  127.0.0.1:2201  2c/2G/8G  Ubuntu 24.04 LTS (cloud)  18s
```

The address includes the port because that is how a QEMU VM is reached: every
guest sits behind a loopback port forward and never has an address of its own.

`STATUS` shows `Degraded` for an instance that is Running, was expected to
publish an address, and has not — a green *Running* you cannot reach is the one
thing worth interrupting a listing for.

#### `iaas show NAME_OR_ID`

Everything on the record: ports, pid, accelerator, display, whether SSH was
enabled, plus the console caveat, the degraded reason and the error message when
they are set. Accepts a name or an id; a name resolves to the live instance, so
reusing a name never points a command at an old audit row.

#### `iaas start|stop NAME_OR_ID`

```bash
iaas stop web-01 --wait
iaas start web-01 --wait
```

Both block on the API, which blocks on the hypervisor. `--wait` additionally
polls until the reconciler has folded the hypervisor's view back into the
record, which is what a script wants before it SSHes in.

The API's refusals are printed as they arrive:

```
$ iaas stop web-02
error: Cannot stop an instance in state 'Stopped' (only Running instances can be stopped)
$ echo $?
5
```

Ports are pinned for an instance's whole life, so the SSH command that worked
before a stop works again after the start.

#### `iaas rm NAME_OR_ID`

```bash
iaas rm web-01              # asks, if stdout is a terminal
iaas rm web-01 --yes        # doesn't
iaas rm wedged --force --yes
```

Destroys the VM and its disk and marks the record Terminated; the row is kept as
an audit trail and the name becomes reusable.

**It never prompts when stdout is not a terminal.** A prompt a pipeline cannot
answer is a hang, so a non-interactive `rm` without `--yes` is a usage error
(exit 2) that says so, and destroys nothing.

`--force` clears the record even if the hypervisor refuses to destroy the VM.
Without it, a driver failure is reported and the row is marked `Error` — the
right default, since a record claiming Terminated while the VM still runs is a
lie. Use `--force` when the hypervisor is wedged and the row has to go anyway.

#### `iaas ssh NAME_OR_ID [-- ARGS...]`

```bash
iaas ssh web-01                          # interactive shell
iaas ssh web-01 -- uname -a              # one command, its exit status is yours
iaas ssh web-01 -- "df -h /; free -m"
```

Builds the same command as the dashboard's *Copy SSH* — the orchestrator's
private key, the forwarded port, the cloud-init user — and hands the terminal
over to `ssh`. On POSIX the process is replaced, so job control and Ctrl-C
behave exactly as if you had typed the command yourself.

Two behaviours worth knowing:

- **It waits for the guest to really answer SSH** (up to `--timeout`, default
  30s) before connecting. On a cloud image's first boot, cloud-init regenerates
  the host keys and restarts `sshd` shortly after it first comes up;
  connections landing in that window are accepted and then dropped, which `ssh`
  reports as `kex_exchange_identification: read: Connection aborted`. Waiting
  for the banner rather than the port is what makes
  `iaas launch web-01 --wait && iaas ssh web-01` reliable.
- **Host-key checking is bypassed by default.** Every VM is `127.0.0.1:<port>`
  from a small, recycled pool, so the same address legitimately presents a
  different key for each instance that holds it — the check would prompt on
  first use and then hard-refuse forever after. `known_hosts` is pointed at the
  bit bucket instead. Pass `--strict` to keep OpenSSH's defaults.

An instance with no SSH access (an ISO install, or an image without cloud-init)
is refused with the way in:

```
$ iaas ssh installer
error: 'installer' has no SSH access: no key was injected (ISO installs and
       images without cloud-init cannot receive one).
hint: Use 'iaas console installer' instead.
```

#### `iaas console NAME_OR_ID`

Opens that instance's console in the dashboard (`?console=<id>`), printing the
URL and any console caveat first. `--print` prints the URL without opening a
browser. A terminal VNC client is out of scope; the framebuffer arrives over a
WebSocket the browser already knows how to render.

### Projects

```bash
iaas projects ls
iaas projects create client-a -d "billable work"
iaas projects rename clietn-a client-a
iaas projects rm client-a --yes
```

```
NAME               INSTANCES  IMAGES  KEYS  AGE  DESCRIPTION
default (default)          0       1     1  39m  Everything that has not been filed anywhere else.
client-a                   1       0     0   3s  billable work
```

Scope any command to one project with the global `--project` flag or
`$IAAS_PROJECT`. It goes **before** the subcommand, like `--api-url`:

```bash
iaas --project client-a ls
iaas --project client-a launch web --wait     # filed under client-a
IAAS_PROJECT=client-a iaas ls
```

Unscoped means every project — what `ls` did before projects existed. A scoping
flag that defaulted to narrow would hide instances from a script that never
asked to be scoped.

**Projects group; they do not isolate.** There is no authentication, so
`--project` filters what you see and nothing else. Instance names are unique
across all projects, and the 409 says which project holds the name:

```
$ iaas --project training-lab launch web
error: An instance named 'web' already exists in project 'client-a'
       (state: Running). Instance names are unique across all projects.
$ echo $?
5
```

Deleting a project never deletes what is filed under it — images and key pairs
move to the default project, and live instances block the delete by name.

### Events

```bash
iaas events                       # everything, newest first
iaas events web-01                # one instance's history
iaas events web-01 --kind stopped
iaas events --limit 200 --json
```

```
   AGE  EVENT                                       ACTOR
=   1m  Corrected to Stopped (record said Running)  reconciler
>   6m  Started                                     api
#   6m  Stopped                                     api
*  32m  Provisioned at 127.0.0.1:2200               api
+  32m  Created from Ubuntu 24.04 LTS (cloud)       api

2026-08-12 17:50:45
  status: Running -> Stopped
  pid: 53444 -> none
```

The log records what the instance row cannot: every stop and start rather than
the most recent one, every error rather than the last, and restores — which
rewrite a disk and change no record anywhere else. `actor` is `reconciler` when
nobody asked: the background pass found the hypervisor disagreeing with the
record and corrected it.

The newest entry's detail is printed under the table when it has one. A
terminated instance still resolves, which is the point — its history outlives
it, until it passes the retention window.

### Networking

```bash
iaas net ls                                   # networks
iaas net modes                                # what ships, and what the rest would need
iaas net forwards web-01                      # what reaches this guest
iaas net forward web-01 18080 80              # host 18080 -> guest 80, live
iaas net unforward web-01 18080
```

```
ID        HOST  GUEST  PROTO  NOTE
derived   2200     22  tcp    SSH — created with the instance and pinned for its lifetime
9f2c1a4b 18080     80  tcp
```

**Forwards bind to `127.0.0.1` only** — reachable from this machine, not from
other devices on your network. A web server forwarded to port 18080 answers in
a browser here and not on your phone; that is the design, not a fault.

A forward takes effect **immediately** on a running instance — no restart — and
is replayed at the next launch. The SSH row is `derived`: it comes from the
instance's pinned port rather than the forwards table, and cannot be removed.

`iaas net modes` explains why bridged and host-only are unavailable and exactly
what each would require (Administrator plus the tap-windows6 driver on Windows;
root or `CAP_NET_ADMIN` plus a pre-made bridge on Linux).

Colliding ports are refused with the reason:

```
$ iaas net forward web-01 2250 80
error: Host port 2250 is inside the SSH port pool (2200-2299), which this system
       allocates from when it launches instances. Forwarding it would make some
       future launch fail. Pick a port outside every pool.
$ echo $?
6
```

### Volumes

```bash
iaas volumes create data 10G --wait
iaas volumes ls
iaas volumes attach data web-01        # instance must be stopped
iaas volumes detach data
iaas volumes rm data --yes             # deletes the data too
```

```
NAME      SIZE  STATUS    ATTACHED TO  DEVICE  AGE
data-one    1G  Attached  dbhost       vdb     50s
data-two    1G  Attached  dbhost       vdc     48s
```

A volume outlives the instances it attaches to: `iaas rm` on an instance
detaches its volumes and leaves them `Available` with their data. Only
`volumes rm` deletes one, and it is refused while attached.

Attach and detach need a **stopped** instance. This hypervisor removes a
hot-unplugged disk without waiting for the guest, which corrupts a mounted
filesystem — so the refusal is a real protection, not a formality:

```
$ iaas volumes attach data-one dbhost
error: Cannot attach a volume to 'dbhost' while it is Running. Volumes are
       attached at boot: this hypervisor removes a hot-unplugged disk without
       waiting for the guest, which corrupts a mounted filesystem. Stop the
       instance first.
$ echo $?
5
```

A new volume is unformatted, and `attach` prints the commands that finish the
job. Device names follow attach order, and that order is stable across
restarts. For anything you care about, mount by **UUID**, not by device path: `blkid /dev/vdb` gives the UUID, and a `/etc/fstab` entry written as `UUID=… /mnt/data ext4 defaults 0 2` survives a volume being detached, reordered, or moved to another instance. Device order is stable here, but the path is a position and the UUID is the disk.

### Snapshots

```bash
iaas snapshot create web-01 before-upgrade -d "clean install" --wait
iaas snapshot ls web-01
iaas snapshot ls web-01 --json
iaas snapshot restore web-01 before-upgrade --yes
iaas snapshot rm web-01 before-upgrade --yes
```

The instance must be **stopped**. Snapshots capture the disk at rest: this
hypervisor cannot save a running guest's memory, and taking its disk underneath
it would preserve a filesystem mid-write. A running instance gets exit 5 with
that explanation.

`restore` asks for confirmation like `rm` does, and for the same reason —
"restore" sounds like recovery, but for anything written since the snapshot it
is a deletion. `--yes` skips it; nothing prompts when stdout is not a terminal.

### Volume snapshots

```bash
iaas volumes snapshot create win-data before-upgrade -d "clean install" --wait
iaas volumes snapshot ls win-data
iaas volumes snapshot ls win-data --json
iaas volumes snapshot restore win-data before-upgrade --yes
iaas volumes snapshot rm win-data before-upgrade --yes
```

A **separate thing** from `iaas snapshot`, which captures an instance. An
instance snapshot does not include attached volumes; a volume snapshot does not
include the instance. Restoring one does not restore the other.

The volume may stay **attached** — what it may not be is held by a *running*
instance. Stop the instance and the snapshot works with the volume still
attached; a running one gets exit 5 with that explanation.

`restore` confirms by default, same as above and for the same reason.

### Images and boot media

```bash
iaas images ls
iaas images ls --json
iaas images import /srv/images/mine.qcow2 --name "My image" --wait
iaas images import ./installer.qcow2 --no-cloud-init
iaas images rm "My image" --yes
iaas isos ls
```

`import` is not an upload: the path is resolved on the **backend's** filesystem
and the file is copied into the image store, so the original can be moved or
deleted afterwards. `--no-cloud-init` says the guest will not consume a NoCloud
seed, which means no SSH key can be injected and instances built from it are
console-only.

Deleting an image that a live instance is backed by is refused, and the message
names the instances: every overlay holds a hard reference to its backing file,
and removing it corrupts that instance's disk irrecoverably.

ISO files are placed in the backend's ISO directory by hand (`IAAS_ISO_DIR`,
default `~/.local-iaas/isos`); there is no upload endpoint.

### System

```bash
iaas serve --port 8000 --reload
iaas capacity
iaas doctor
iaas version
iaas completion bash
```

`serve` runs uvicorn in the foreground from the backend's own directory, so the
database and `.env` resolve the same way wherever you launch it from. It is a
convenience wrapper, not a process manager: no daemonising, no PID file. Ctrl-C
stops it. It binds `127.0.0.1` by default — the API has no authentication, so
binding a LAN interface publishes unauthenticated control of every VM on the
host. Installing the backend as a service comes later.

`capacity` shows what the host can still give:

```
RESOURCE  TOTAL  COMMITTED  ALLOCATABLE  MAX/INSTANCE
vCPU         14          3           25            14
memory    15.5G         3G        10.5G         10.5G
disk       473G        13G          35G           35G
```

vCPU *allocatable* exceeds the core count because vCPUs timeshare; no single
instance may exceed `MAX/INSTANCE`. Disk *allocatable* is free space, not total
minus committed — qcow2 overlays are sparse, so committed size is not consumed
space.

## Exit codes

| Code | Meaning | Typical cause |
|---:|---|---|
| 0 | Success | |
| 1 | Failure | The instance landed in `Error`; a 5xx from the API |
| 2 | Usage error | Unknown flag, contradictory options, a confirmation that cannot be asked for |
| 3 | API unreachable | The backend is not running, or `--api-url` points somewhere else |
| 4 | Not found | No instance or image by that name or id (API 404) |
| 5 | Conflict | Wrong state for the operation — stopping a stopped VM (API 409) |
| 6 | Invalid | Validation failure or a capacity refusal (API 422) |
| 7 | Timed out | `--wait` gave up. Nothing was cancelled; the instance may still be starting |

7 is deliberately distinct from 1: "still provisioning" and "broken" call for
different reactions in a script.

## Scripting

Three rules hold everywhere:

- `--json` writes JSON to stdout and **nothing else**. Progress, spinners,
  warnings and errors always go to stderr, so a pipe stays clean even while a
  human watches it.
- Colour and spinners disable themselves when the output is not a terminal, or
  when `NO_COLOR` is set.
- Nothing prompts when stdout is not a terminal.

The `--json` document is the API's own, unreshaped — [API.md](API.md) is the
reference for both.

### Launch a fleet, wait, collect the addresses

`launch --wait` blocks, so a loop of them provisions one VM at a time. To start
several at once, fire them all off first and wait afterwards — `launch` without
`--wait` returns as soon as the request is accepted:

```bash
#!/usr/bin/env bash
set -euo pipefail

settle() {                       # wait for one instance, already launched
  local name=$1 status
  while true; do
    status=$(iaas show "$name" --json | jq -r .status)
    case $status in
      Running) return 0 ;;
      Error)   iaas show "$name" >&2; return 1 ;;
      *)       sleep 3 ;;
    esac
  done
}

for n in 1 2 3; do
  iaas launch "web-0$n" --preset small >/dev/null   # 202, provisions in parallel
done

for n in 1 2 3; do
  settle "web-0$n" || { echo "web-0$n failed to start" >&2; exit 1; }
done

iaas ls --json | jq -r '.[] | select(.status=="Running") |
  "\(.name)\t\(.ip_address):\(.ssh_port)"'
```

For a single instance, `--wait` does all of that:

```bash
iaas launch web-01 --preset small --wait
```

### Run a command on every instance

```bash
for name in $(iaas ls --json | jq -r '.[] | select(.ssh_enabled) | .name'); do
  echo "== $name"
  iaas ssh "$name" -- uptime
done
```

### Tear down everything

```bash
iaas ls --json | jq -r '.[].name' | while read -r name; do
  iaas rm "$name" --yes
done
```

### Branch on the exit code

```bash
if iaas launch web-01 --wait; then
  iaas ssh web-01 -- 'cloud-init status --wait'
else
  case $? in
    6) echo "too big for this host — check: iaas capacity" ;;
    7) echo "still coming up — check: iaas show web-01" ;;
    *) iaas show web-01 ;;
  esac
fi
```

### PowerShell

Windows PowerShell 5.1's `ConvertFrom-Json` reads pipeline input line by line,
so multi-line JSON has to be joined first. This is a PowerShell quirk, not a
CLI one:

```powershell
$instances = (iaas ls --json | Out-String) | ConvertFrom-Json
$instances | Where-Object status -eq 'Running' |
  Select-Object name, ip_address, ssh_port
```

PowerShell 7 pipes it directly:

```powershell
iaas ls --json | ConvertFrom-Json | Select-Object name, status
```

PowerShell also renders anything on stderr as an error record. That is what the
spinner and warnings are; redirect with `2>$null` when it gets in the way.
