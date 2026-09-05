# Decisions

Architecture decision log. Newest concerns last within each area. Each entry is
Context / Decision / Consequences.

---

## 1. The hypervisor sits behind an ABC

**Context.** The project began on Multipass but was always intended to reach
deeper than Multipass could go. Committing the routers, models and frontend to
one hypervisor's vocabulary would have made changing it a rewrite.

**Decision.** All hypervisor access goes through `ComputeEngine`, an abstract
base class returning a normalised `InstanceInfo`. Native state names, command
strings and file layouts are confined to the driver. A registry maps a row's
`engine` column to a driver instance.

**Consequences.** Adding a driver is one module plus one factory entry. This has
been tested in both directions: Multipass was added in Phase 5 and removed in
Phase 7 with no change above the seam. The cost is one indirection and a
`LaunchOptions` object for capabilities only some drivers have; drivers ignore
what they cannot honour.

---

## 2. The database is the source of truth, and rows outlive their VMs

**Context.** Two systems hold state — our database and the hypervisor — and they
drift. Something has to be authoritative about *intent*.

**Decision.** The database holds desired state. The hypervisor holds actual
state. A reconciler folds the second into the first. Terminated instances keep
their row rather than being deleted.

**Consequences.** Every mutating route writes the DB first, then drives the
hypervisor. Users get an audit trail of what ran. The cost is that the DB can be
wrong until the next reconcile — mitigated by a background pass — and that
"deleted" rows have to be excluded from most queries. It also forced decision 6:
retained rows must not reserve their names forever.

---

## 3. Multipass first, then retired

**Context.** Multipass gave a working VM lifecycle on day one with almost no
code: a CLI, an image catalog and cloud-init already wired together.

**Decision.** Build on Multipass to get the control plane right, then move to
QEMU for depth, then retire Multipass once QEMU covered everything.

**Consequences.** Retired in Phase 7 for three reasons, in order of weight:
(a) no capability overlap with where the product was going — no framebuffer, no
arbitrary ISO boot, no image import, so every feature after Phase 5 would have
been QEMU-only with a disabled affordance; (b) the `multipassd` daemon wedged
three times during Phases 5–6, each with the same signature (`Start-VM` succeeds,
"Waiting for SSH to be up", a Qt event-loop warning about timers started from
another thread, then permanent silence needing an elevated service restart);
(c) two engines meant per-engine dispatch in every route, doubled test fakes and
UI branches for capabilities only one side had. Historical `multipass` rows are
kept and never rewritten — they render with a badge, are skipped by the
reconciler, and can be terminated but not controlled.

---

## 4. QEMU driven directly over QMP, not libvirt or a platform API

**Context.** Options were libvirt, the platform's native API (Hyper-V's HCS/WMI
on Windows), or QEMU's own process and monitor protocol.

**Decision.** Launch `qemu-system-x86_64` as a subprocess and control it over
QMP on a loopback TCP socket.

**Consequences.** One code path across Windows, macOS and Linux, with the
accelerator swapped per host (WHPX/HVF/KVM). libvirt would have added a daemon
that is awkward on Windows; a native platform API would have meant a different
engine per OS and would forfeit qcow2 overlays and image import, which are the
primitives the image catalog is built on. The cost is that we own process
supervision, port allocation and liveness detection ourselves — see decisions 9
and 10.

---

## 5. WHPX and VGA text mode: std graphics stays the default

**Context.** Under WHPX, the browser console showed a black screen for cloud
images. The Phase 6 conclusion recorded in the code was "QEMU renders nothing
under WHPX", and ISO instances were forced onto software emulation to
compensate — a ~30× boot-time penalty.

**That conclusion was wrong**, and the error is worth recording. It came from an
experiment that tested display devices **with no guest OS booted**. In that
setup only SeaBIOS's legacy VGA text output can appear, so virtio-gpu, ramfb and
bochs-display "failing" proved nothing — they render nothing without a guest
driver by design.

**Decision.** Re-tested with real guests. The actual limitation is narrower:
QEMU's VGA emulation depends on memory dirty-tracking that WHPX does not
provide, so only a guest sitting in **VGA text mode** comes up blank. A guest
that switches to a framebuffer renders fine, and virtio-gpu renders regardless
because the guest driver pushes updates over a virtqueue instead of QEMU polling
memory. Measured: Ubuntu cloud image on std VGA — blank; the same guest on
virtio-gpu — live (33,616 pixels changed on a guest write); Alpine ISO on std
VGA — live (38,376 pixels changed on a keystroke).

So: hardware acceleration became the default for every boot mode, the forced-TCG
rule for ISO was deleted, **std VGA remains the default display**, and the
display became a per-instance choice.

**Consequences.** std stays the default because it needs no guest driver, so
*something* renders for any OS — which is the entire point of arbitrary-guest
ISO boot; virtio would give a Windows installer or a rescue ISO no picture at
all. Users who want a console on a cloud image select Modern graphics.
`console_supported` no longer gates on the accelerator; a `console_caveat`
string warns about the one uncertain combination instead, because whether a
guest leaves text mode cannot be known in advance. This matches QEMU's own
documented WHPX limitation and is not a regression in the build we use.

---

## 6. Name uniqueness moved from a DB index to application logic

**Context.** `instances.name` had a unique index. Combined with decision 2
(rows outlive their VMs), a name became permanently unusable after its first
instance was terminated — every reuse returned 409 forever.

**Decision.** Drop the unique index; keep a plain index for lookups. Enforce
uniqueness in `POST /instances`, scoped to non-Terminated rows.

**Consequences.** Names are reusable once freed, and several terminated
generations of one name coexist as history. The uniqueness constraint is no
longer enforced by the database, so two truly concurrent POSTs could both pass
the check — acceptable for a single-user local tool, and closable later with a
partial unique index. This also made the DELETE route's early return on
already-terminated rows load-bearing: without it, terminating a stale audit row
would destroy the live VM that reused its name.

---

## 7. Provisioning is asynchronous

**Context.** A launch takes 30 seconds with hardware acceleration and minutes
without, plus a one-time ~600 MB image download. An HTTP request cannot hold
that open.

**Decision.** `POST /instances` writes a `Pending` row, returns 202, and runs the
launch in a background task. The client polls.

**Consequences.** The UI stays responsive and shows real progress through
`Pending` → `Provisioning` → `Running`. The cost is a class of bug where the
background job and the reconciler disagree about a row mid-launch, which is why
the reconciler will not promote a `Pending`/`Provisioning` row unless the
hypervisor reports it ready *with* an address — only the job, which knows the
launch returned, can finish that transition for address-less guests.

---

## 8. Reconciliation is periodic, with a three-phase transaction boundary

**Context.** Originally the DB learned the truth twice in an instance's life:
once after launch, and whenever a human clicked Refresh. Anything missed in that
single sample stuck permanently — including a row left `Running` with no
address — and out-of-band changes never appeared.

**Decision.** A background pass every 30 seconds, structured as: decide which
engines to ask → do the slow listing with **no transaction open** → read, decide
and write in one boundary, reading rows *after* the I/O.

**Consequences.** The DB converges without user action. Reading rows after the
slow phase is what makes the write safe: a terminate landing mid-pass is already
visible, so a stale snapshot cannot overwrite it. An earlier version held rows
across the listing and patched the resulting race with a per-row re-read; moving
the boundary replaced that with one pattern. Cost: a wedged hypervisor makes each
pass block for a full timeout in a worker thread.

---

## 9. Capacity is allocatable, not total; CPU oversubscribes and memory does not

**Context.** Users type sizes now. Raw host totals would happily let someone
allocate 16 GB on a 16 GB machine and watch the launch die inside the
hypervisor, with an error from QEMU rather than an explanation.

**Decision.** Report every resource three ways — total, committed, allocatable —
and validate against allocatable, server-side. Hold back a host reserve (2 GiB
default). Let vCPUs oversubscribe by a factor (2.0 default) since they
timeshare, but cap any single VM at the real core count. Bound disk by free
space rather than committed size, because qcow2 overlays are sparse. Count
`Pending`/`Provisioning` instances as committed; do not count `Stopped` ones.

**Consequences.** Refusals name the arithmetic ("Requested 60000 MB but only
13780 MB allocatable (2048 MB host reserve, 0 MB committed…)"), which is
actionable in a way "too big" is not. Counting in-flight launches prevents two
concurrent requests from each passing and jointly overcommitting. Not counting
stopped instances means a user who stops everything can launch again. If psutil
is unavailable, limits go permissive with a warning rather than blocking every
launch — a broken capacity *check* must not be a worse outage than the problem
it prevents.

---

## 10. Launch options are framed by intent; the accelerator is derived

**Context.** The launch form had grown into a list of QEMU mechanics: boot
source, engine, accelerator, display. Each was a real choice, but "whpx vs tcg"
is not a question a user can answer without knowing what it does.

**Decision.** Three outcome-named modes — Quick launch, Install from ISO,
Existing disk image — which decide boot source, cloud-init seeding and access
method between them. The accelerator is derived and lives behind an *Advanced*
disclosure as an override. Sizing presets are one-click defaults that fill
editable numbers rather than a fixed menu.

**Consequences.** The common path is one choice and a name. The mechanics remain
reachable for people who need them. Labels describe outcomes ("Standard
graphics — works everywhere") rather than devices ("std"), because the device
name carries no information for the person choosing. The risk is a mode whose
sub-text drifts from what the code does — as happened when ISO's "Slower to run"
survived the removal of forced software emulation; that text now carries a
comment explaining why it must not come back.

---

## 11. The CLI is a second API client, and its exit codes are the contract

**Context.** Phase 8 added a terminal client for people who script. The
temptation with a CLI shipped *inside* the backend package is to let it import
`app.models` and open the database — it is right there, and it would be faster.

**Decision.** `app/cli/` reaches the system only through HTTP, via one module
(`app/cli/client.py`). Two capabilities it needed and the API could not express
were added *to the API* rather than worked around: `GET /diagnostics` (the
host-side facts `doctor` reports) and `DELETE /instances/{id}?force=true`. The
scripting surface is treated as a contract: a documented exit code per failure
class, `--json` that puts JSON and nothing else on stdout, progress and warnings
always on stderr, and no prompt when stdout is not a terminal.

**Consequences.** The dashboard and the CLI cannot diverge in capability, and a
gap in one is a gap in the API that both then get. Command tests inject
FastAPI's `TestClient` as the transport, so they run against the real
application without a server. The cost is round trips the CLI could have
skipped — `show NAME` lists instances to resolve the name, then re-reads by id
— which is the correct price for having exactly one way in. Two names are
pinned rather than scattered: `CLI_NAME` in `app/cli/naming.py` and the
`[project.scripts]` entry, so renaming the product is two lines.

**A note on `ssh`.** It bypasses host-key checking by default and waits for the
guest's SSH *banner* before connecting. Both are consequences of the loopback
port-forward model rather than convenience: the same `127.0.0.1:<port>`
legitimately presents a different key for every instance that recycles that
port, and a cloud image's first boot restarts `sshd` after cloud-init
regenerates its keys — which was observed breaking
`iaas launch web-01 --wait && iaas ssh web-01` before the wait was added.

---

## 12. Platform differences are resolved by data, not by branching

**Context.** The portability audit found the codebase had quietly encoded
"Windows" in several places that read as general: the accelerator probe asked
for WHPX unconditionally, `-cpu qemu64` (a WHPX crash workaround) was applied to
every accelerator, and three call sites answered "is this accelerated?" by
testing `accel == "whpx"`.

**Decision.** Where a platform difference is real, it is expressed as a table
rather than a conditional — `HOST_ACCELS` maps platform to accelerator,
`HARDWARE_ACCELS` defines what "accelerated" means, and `cpu_model` maps
accelerator to CPU model. Branches are reserved for genuinely different
mechanisms: process liveness and termination, which differ in kind rather than
in value.

**Consequences.** Adding macOS was one map entry, not an audit. The conflation
that caused the worst finding — "accelerated" meaning "WHPX" — cannot recur,
because the question now has one definition in one place. The counter-example
is instructive and is recorded in
[PORTABILITY.md](PORTABILITY.md): the null device *looked* like the most
obvious platform switch in the codebase, and the correct answer turned out to
be a hardcoded POSIX string on every platform, because what matters is the
platform of the program being handed the string, not the one handing it over.

---

## 13. An address means the guest answers, not that the process is up

**Context.** `QemuEngine.get_instance_info` reported `ip_address` whenever the
VM process was alive and QMP responded — both true about a second after spawn.
The reconciler treats an address as evidence that a launch has finished, so on
any host slow enough for a reconcile pass to land mid-boot, rows were promoted
to `Running` while the guest was still starting. Measured on a software-emulated
host: `Running` at 30 s, actually reachable at 254 s.

**Decision.** The address is reported only when the guest's SSH banner answers.
Liveness decides `Running` vs `Stopped`; reachability decides whether there is
an address to publish.

**Consequences.** `launch --wait` now returns on a usable instance, and
`degraded` — "Running, expected an address, has none" — became reachable for
QEMU at all, having been dead code while the engine always supplied an address.
The cost is one loopback probe per running instance per reconcile pass; a
stopped VM refuses instantly, so only a booting one can spend the budget. That
budget is the same 5 s the boot wait already used: the first attempt invented a
tighter 1 s, and on an emulated host where the banner takes 1.0–1.24 s it
turned healthy guests into address-less ones. Two numbers for one question is
one number too many.

---

## 14. On-demand images may launch before they are downloaded

**Context.** The built-in Ubuntu image is fetched on first use by the engine, so
its catalog row reads `Importing` until something asks for it. The launch path
required `Available`, which meant the first launch on a fresh install was
refused for the absence of the file that launch was supposed to fetch — then
succeeded on the second attempt, because the failed first one had left the file
behind.

**Decision.** `Importing` is launchable for the built-in image specifically.
`Error` is not, for any image: that means the file is present and unreadable,
which downloading does not fix. Imported images still require `Available` —
nothing fetches those.

**Consequences.** The documented first-run experience works. The wider lesson is
about *where* this was found: it needed a machine that had never launched an
instance, which no developer machine is after its first day. The regression test
constructs that state explicitly, because the environment cannot be relied on to
reproduce it.

---

## 15. Snapshots are taken with the instance stopped

**Context.** Desktop hypervisor users expect snapshots, and expect to take them
while a VM runs. The brief asked for one mode chosen on evidence rather than
both attempted.

**Decision.** qcow2 *internal* snapshots via `qemu-img`, and only while the
instance is stopped. Running instances get a 409 that explains why.

**The evidence.** This QEMU build (10.0.94) exposes the whole snapshot family —
`snapshot-save`, `snapshot-load`, `snapshot-delete`, `blockdev-snapshot-internal-sync`,
and HMP passthrough. Availability was not the constraint; the accelerator was:

| | WHPX (reference platform) | TCG, same binary and host |
|---|---|---|
| `snapshot-save` | fails | — |
| HMP `savevm` | fails | succeeds, 979 KiB of state |
| `blockdev-snapshot-internal-sync` (running) | succeeds, **0 B of VM state** | — |

Both full-state routes fail identically under WHPX with *"State blocked due to
non-migratable CPUID feature support, dirty memory tracking support, and
XSAVE/XRSTOR support"* — the same missing dirty-memory tracking that makes a
text-mode guest render a blank console (decision 5). The third row is the
dangerous one: it reports success while saving no memory at all, so restoring
it would return a filesystem that was never quiesced — a power-cut, offered
under the name "snapshot".

**Consequences.** Snapshots are honest about what they capture, and the API
says so instead of half-working. Live snapshots would additionally require
giving every drive an explicit `node-name` (ours are anonymous `#block154`
nodes, which cannot be referenced), a change to every VM for a feature the
reference platform cannot run.

A second finding came out of the same investigation and is worth stating
separately, because it inverts an assumption: **on Windows, `qemu-img` does not
lock a disk that a running QEMU holds open.** Measured — `qemu-img snapshot -c`
against a live instance succeeded and wrote the snapshot. On Linux, QEMU's OFD
lock makes qemu-img decline. So the engine's own "is it running?" guard is not
a politeness that duplicates the hypervisor's; on Windows it is the only thing
preventing two writers on one qcow2.

If a KVM host ever becomes the reference platform, live snapshots are worth
revisiting: TCG's success above suggests the mechanism works wherever the
accelerator supports state save.

---

## 16. User-data is merged structurally, and lists concatenate

**Context.** Users need their own cloud-config, but ours is what puts a login on
the machine. Replacing one with the other loses something either way.

**Decision.** Parse both with `yaml.safe_load` and deep-merge, never
concatenate text. Mappings merge recursively; on a leaf conflict **the user's
value wins**; lists are **concatenated**, duplicates dropped.

**Consequences.** The list rule is the exception to "the user wins", and it is
the one that matters. Every list cloud-config uses is additive in meaning —
`packages` is things to install, `runcmd` is commands to run. More importantly,
replacing `users:` would drop the account our SSH keys were installed on, and
the instance would boot with no way in at all. Concatenating keeps both; cloud-
init is happy to create two users. A test is named for exactly that case.

Invalid YAML is a 422 carrying the parser's own line and column, because
"invalid YAML" on a fifty-line document leaves the user nowhere to start. ISO
instances refuse user-data outright rather than accepting and discarding it.

---

## 17. Deleting a key pair does not revoke it

**Context.** A key pair can be deleted while instances launched with it are
still running.

**Decision.** Allow the deletion, keep the instance association (name and
fingerprint denormalised onto the link), and mark it `deleted` in the detail
view. The generated private key file is left on disk.

**Consequences.** The catalog is a record of keys we know about, not a control
plane for access. The key is already written into those guests'
`authorized_keys`, and removing it would mean logging into every affected
instance and editing a file — something this system does not do and should not
pretend to. So the UI says plainly what deletion does and does not do, and the
detail view keeps naming the key so nobody concludes the access went with it.

The related failure mode is handled separately: if *every* key an instance was
launched with is deleted before it provisions, the launch fails with `Error`
rather than producing a guest that reports SSH access it cannot honour.

---

## 18. Events are observability, never control flow

**Context.** Phase 10 shipped an Activity panel derived from timestamps, with a
caveat admitting what it could not show: `updated_at` is a single field, so a
stop/start collapsed to one entry and an earlier error was overwritten by a
later one. Restore was the sharpest case — it rewrites a disk and changes no row
anywhere, so nothing recorded that it had happened. Phase 11 replaced the
derivation with an append-only `instance_events` table.

**Decision.** Three rules govern the writer:

1. **`record_event` cannot raise.** A full disk, a locked database or a bug in
   the event code must never turn a successful terminate into a failed one.
   Every failure is logged and swallowed.
2. **Events are written after the commit, on their own session.** A caller's
   rollback must not be able to take an event with it, and an event must never
   describe a change that was later undone.
3. **Events are emitted from the code that already performs the mutation.** No
   second path exists that could drift from the first.

**Consequences.** A missing event is a gap in a history; a raised exception is a
broken operation. The first is always the better trade, so the blanket `except`
in `app/events.py` is deliberate and is tested by breaking the writer and
asserting the terminate still succeeds.

The cost is that the log is not a guarantee. It is an account of what happened,
kept as faithfully as can be done without ever becoming a dependency of the
thing it describes.

---

## 19. An operation this process is running is not an "out-of-band change"

**Context.** Start, stop, provisioning and terminate all block on QEMU for tens
of seconds; the reconciler runs every 30. So the background pass routinely
observes an operation that has not returned yet.

This produced two problems, and the event log made the first one impossible to
ignore: every launch grew a `Corrected to Running (record said Provisioning)`
entry and every start a `Corrected to Running (record said Stopped)` one.
Nothing had been corrected — the reconciler had seen our own work and had no way
to know it was ours.

Chasing that surfaced the second, which had been there since Phase 6 and was a
real defect. `reconcile_all` lists the hypervisor *before* it reads the rows, so
a stop landing in that window was overwritten by a listing taken while the VM
was still up: the row flipped back to `Running` a second after `stop` returned,
and the next `start` was refused with *"only Stopped instances can be started"*.
`iaas stop x --wait && iaas start x` was intermittently broken.

**Decision.** An in-process advisory claim (`_api_operation`) marks an instance
while this process drives its hypervisor. The reconciler skips a claimed row
entirely — it neither writes nor reports it. A row counts as claimed if it was
in flight when the listing began, is in flight at write time, or had an
operation begin and end in between (a per-instance completion counter catches
the last case, which neither set snapshot would).

**Consequences.** The reconciler's job is unchanged for everything it is
actually for: a VM stopped outside the API, a guest that vanished, an address
that appeared late. Those are still detected and now recorded with both sides of
the change.

The claim is advisory and in-process, not a lock. A multi-host or multi-process
deployment would lose it and be back to the Phase 6 behaviour, which is the
honest scope of the fix — this system is a single process by design, and the
alternative (a lease column, or serialising the reconciler against every route)
buys nothing here.

---

## 20. Instance names stay globally unique; projects are organisational

**Context.** Projects group instances, images and key pairs. The obvious
cloud-shaped follow-on is per-project instance names — AWS lets you have a
`web` in two accounts — so the question was whether `client-a/web` and
`client-b/web` could coexist.

**Evidence.** The instance name is the hypervisor's identity in four places:
the on-disk directory `instances/<name>/`, the QEMU `-name` process label, the
cloud-init `instance-id`, and the guest's `local-hostname`. Measured against
the real engine:

```
project client-a, instance 'web' -> ...\qemu\instances\web
project client-b, instance 'web' -> ...\qemu\instances\web
same directory: True
list_instances keys: ['web']
```

That is not a naming collision but a **data** collision: two instances would
share one `disk.qcow2`, one `runtime.json` and one set of pinned ports. Worse,
`list_instances()` is keyed by directory name and is what `reconcile_all`
matches rows against, so both rows would bind to the same `InstanceInfo` and
stopping one would mark the other Stopped.

Any per-project scheme therefore needs a different on-disk identity, which
means renaming existing directories. Measured:

```
RENAME FAILED while the disk was open: [WinError 5] Access is denied
RENAME SUCCEEDED once the disk was closed
```

Windows refuses to rename a directory containing an open file, and a running VM
holds its overlay open. POSIX would allow it (rename is inode-based), so this
is a platform split in which the reference platform is the one that breaks. The
migration would have to be "stop every VM first".

**Decision.** Names are unique across all projects, over live rows only. A
project is a `project_id` column and nothing more.

The three alternatives and why each was rejected:

| Approach | Rejected because |
|---|---|
| `<project>-<name>` directories | Puts project identity into a global filesystem namespace, implying a separation the model does not provide. Renaming a project would need the same impossible directory rename. |
| UUID directories | Clean, but needs the rename above *plus* an id-based rewrite of all eight name-keyed `ComputeEngine` methods — the architectural seam. |
| New scheme for new instances only | Two naming schemes in the engine forever. |

**Consequences.** The 409 names the project holding the name — *"an instance
named 'web' already exists in project 'client-a'"* — which is only defensible
because a project is not a security boundary. If it ever became one, that
message would be an information leak and would have to change. The comment at
the call site says so.

One argument was considered and **not** used: that a `<project>-<name>` prefix
would not fit the name limit. The 31-character cap is a leftover Multipass
constraint, not a hostname rule (DNS labels allow 63), so it could have been
raised. It is not a reason for anything.

---

## 21. A project is a label, and deleting one never deletes what it held

**Context.** Deleting a project could plausibly mean "delete everything in it",
which is what a folder metaphor suggests.

**Decision.** Deleting a project is refused while **live instances** are filed
under it, with the 409 naming them. Otherwise its images and key pairs are
**moved to the default project**, and terminated instance rows move with them.
Nothing filed under a project is ever destroyed by deleting the project. The
default project cannot be deleted at all.

**Consequences.** Deleting a 4 GB image because someone removed a label it
happened to carry would be indefensible, and quietly re-filing a running VM as
a side effect of tidying the sidebar is not something the user asked for. The
asymmetry is deliberate: instances block because they are running processes
with ports and disks, images and keys do not because they are inert.

Terminated instances never block — they are audit history, and a project full
of destroyed VMs would otherwise be undeletable forever. For the same reason,
the counts shown on a project exclude them.

---

## 22. Volumes attach and detach on a stopped instance only

**Context.** QEMU exposes `device_add` / `device_del`, so hot-plugging a disk
into a running guest looks available. It was assessed before building.

**Evidence.** All the commands are present in this build — `device_add`,
`device_del`, `blockdev-add`, `blockdev-del`. They still fail:

```
QMP 'device_add' failed: GenericError: Bus 'pcie.0' does not support hotplugging
```

The engine launches `-machine q35`, whose root complex does not accept
hot-plug, and the live topology has no PCIe root ports — every device sits flat
on bus 0. So **hot-plug is impossible on every instance this product has ever
launched**.

Adding spare `pcie-root-port` devices at boot does make it work; hot-add then
succeeded and the guest saw `/dev/vdb`. But root ports can only be created at
launch, so N of them must be reserved in advance and N becomes a hard cap on
hot-pluggable volumes — chosen before anyone knows how many they want. Existing
instances have none and would need a restart, which is the stopped-only
workflow anyway.

The decisive finding is hot-**unplug**. With the volume formatted, mounted and
holding a file, `device_del` returned success immediately and the device was
gone in under a second, without asking the guest:

```
device_del returned: {}          ← success, immediately
after 1s: device still attached = False

guest:  ls /dev/vdb        → No such file or directory
        mount | grep -c vdb → 1     ← still in the mount table
        cat /mnt/vol/data.txt → Input/output error
dmesg:  EXT4-fs (vdb): shut down requested (2)
        Aborting journal on device vdb-8.
```

The API would report a clean detach while the guest's filesystem aborted its
journal. The file survived that particular test — `fsck` came back clean, with
a journal awaiting recovery — but that is timing, not a property of the
mechanism.

A related finding settled the ordering design: re-attaching the *same* volume
brought it back as `/dev/vdc`, not `/dev/vdb`. Guest device names follow attach
order within a boot, so hot-plug makes them a function of session history.

**Decision.** Attach and detach require a **stopped** instance; 409 otherwise,
with the message naming the measurement rather than stating a rule. Attached
volumes become `-drive` arguments at launch, emitted in the order recorded by
`Volume.attach_order` and replayed from the instance's runtime file, so the
guest's device names are a stable function of the database.

Measured on Windows/WHPX. Neither finding is platform-specific: the root-port
restriction is a property of the `q35` machine type and the unplug semantics
are QEMU's device model plus the guest kernel, both identical on Linux.

**Consequences.** Attaching is a two-step workflow — stop, attach, start — and
the UI and CLI say so up front rather than letting a user discover it from a
409. In exchange, there is no code path in which this system pulls a disk out
from under a mounted filesystem.

Volume names are unique **per project**, unlike instance names (DECISIONS #20),
because the file is named by uuid and nothing outside the database refers to
it. A volume name is a label; an instance name is the hypervisor's identity.

---

## 23. Terminating an instance detaches its volumes; it never deletes them

**Context.** Terminating an instance destroys its overlay and, with it, its
snapshots — they live *inside* that file. An attached volume is a separate
file that happens to be plugged in.

**Decision.** Terminate detaches every attached volume and leaves it
`Available`, data intact. Deleting a volume is a separate, explicit operation
that is refused while the volume is attached.

**Consequences.** This is the AWS behaviour and the only defensible one: a
volume often holds the only copy of something, and destroying it because
someone destroyed the VM it was plugged into is not a trade-off, it is a bug.
The terminate event records which volumes were kept, so the history does not
read as though they went with the instance.

The asymmetry with snapshots is deliberate and is stated at the call site:
a snapshot is *part of* the instance's disk, a volume is *attached to* it.

That same distinction is a footgun in the other direction — **a snapshot does
not include attached volumes**, so restoring rolls the OS back while the data
disks move on. The snapshot dialog names the attached volumes and says they are
not included, rather than leaving it to the docs.

## 24. Networking is user-mode NAT only; the other modes are deferred with their reasons

**Context.** Phase 11 set out to move past user-mode NAT. Three modes were
assessed against what an operator would actually have to do on Windows and
Linux, and two of them turned out to be blocked by things this project cannot
supply.

**Decision.** Ship `user` alone — QEMU's built-in NAT, which every instance has
always been on — and model the other two as *deferred with a stated blocker*
rather than hiding them or offering a disabled control with no explanation:

- **Host-only / internal.** QEMU's portable shared-segment backend is a
  multicast socket, and it fails outright on Windows: `can't bind ip=230.0.0.1
  to socket`. The alternative makes one VM the switch for the others, so the
  whole segment dies when that instance is stopped. Nothing the operator can
  install fixes this — it needs a switch process this project does not have.
- **Bridged.** Needs a host tap device, which is elevated on every supported
  platform. Windows wants Administrator to install the tap-windows6 driver
  (QEMU has the backend compiled in, but there is no adapter for it to open).
  Linux wants root or `CAP_NET_ADMIN` — `/dev/net/tun` is world-writable, yet
  `ip tuntap add` still returns "Operation not permitted" — plus a pre-made
  bridge and a setuid `qemu-bridge-helper` with an `/etc/qemu/bridge.conf` ACL.

Every claim there was measured on this build, and the three consumers — API,
dashboard, docs — all read `DEFERRED_NETWORK_MODES` in `app/models.py` so they
cannot drift into disagreeing about what is required.

Port forwards are the compensating feature, and they are applied **live** to a
running guest. That is safe for reasons that do not generalise to anything else
in the system: SLIRP owns the listening socket entirely, the guest is never
told, and QEMU starts listening immediately — so unlike a volume, a forward has
no in-guest state to get out of step with.

**Consequences.** A guest has no address of its own, so a forward is the only
way in and everything binds to loopback. "Port forwards done well over bridged
done badly" was the trade, and the deferred entries are what stop that reading
as an omission.

## 25. The SSH forward is not a row in the port-forwards table

**Context.** Phase 11 added a `port_forwards` table. The SSH forward — pinned
on `Instance.ssh_port` since Phase 5 and emitted by the engine from the
instance's runtime file — was an obvious candidate to fold into it, since it is
the same kind of object.

**Decision.** Leave it exactly where it is, and synthesise a read-only row for
the listing.

Folding it in would put *the one forward every instance depends on* behind a
data migration. A single row that failed to backfill is an instance nobody can
reach, and it would fail silently — the VM boots, the dashboard looks right,
and only an SSH attempt discovers it. There is no upside to weigh against that:
the table would be tidier, and tidiness is not worth a class of unreachable VM.

**Consequences.** `GET /instances/{id}/forwards` returns the SSH entry first,
marked `derived: true` with the synthetic id `ssh`, so the listing still tells
the whole truth about what reaches a guest. Clients must render it read-only,
and `DELETE` on it is refused with a rule rather than a 404 — offering a button
that fails, or worse appears to work and then shows the row again on the next
poll, is worse than offering nothing.

## 26. Every path the backend owns is resolved against the state directory, including the database

**Context.** `database_url` defaulted to `sqlite:///./iaas.db` — resolved
against the process's *working directory* — while every other path was rooted
in `IAAS_STATE_DIR`. Started from `backend/` the dashboard showed your fifty
instances. Started from the repo root it showed an empty one, and created a
second database to be empty in. Nothing was lost and nothing said so, which is
the worst combination: it is indistinguishable from data loss while you are
looking at it, and the evidence — a stray zero-byte `iaas.db` — looks like
nothing at all.

**Decision.** The database follows `state_dir` like the other four paths, by
the same rule: re-rooted only if it was left at its default, so naming it
explicitly still wins. `IAAS_DATABASE_URL=sqlite:///./iaas.db` remains
available for anyone who actually wants the old behaviour.

A one-time relocation moves a database found at the old location, sidecars
first and the `.db` last so an interruption cannot separate a database from its
write-ahead log. It is deliberately timid, and the refusals are the design:

- it does nothing unless the configured path is still the default *and* the
  engine about to be used is the one pointing at it — two conditions, because
  a file-moving migration guarded by one is a fixture away from dragging a
  developer's real database into a `tmp_path`;
- it never writes over a target that has rows in it. Two databases with history
  is a question for a human; picking one silently is how a migration destroys
  what it was written to rescue.

A zero-byte target is treated as absent, because that is what a database
created by merely pointing at a path is.

**Consequences.** Existing installs move once, loudly, and keep their history.
`~` has to be expanded before the URL reaches SQLAlchemy, which would otherwise
open a directory literally named `~` — so `resolved_database_url` exists and
every consumer goes through it. The database also appears on the Settings
screen now, as a path you can navigate to rather than a URL.

## 27. Windows guests install on compatible devices, not on virtio

**Context.** Windows Setup ships inbox drivers for a conservative set of
hardware. The paravirtualised devices Linux prefers are not in it: with an
`if=virtio` root disk and a `virtio-net-pci` NIC, Setup reaches the disk-select
screen and shows an empty list. It does not say "missing driver" — it shows
nothing, which reads as broken hardware.

Two strategies were available. (a) Give Windows devices it already knows —
AHCI/SATA disk, e1000e NIC, standard VGA — and let it install unaided. (b) Keep
virtio and attach the virtio-win driver ISO as a second CD-ROM, so the user
loads the storage driver during setup via "Load driver → browse".

**Decision.** (a), with (b) documented as a post-install upgrade in
[docs/WINDOWS.md](WINDOWS.md).

Measured on this host at QEMU 10.0.94: `ich9-ahci` and `e1000e` both attach, a
guest boots off them under WHPX, and standard VGA renders at 1280x800 with 5
distinct colours — a real picture, not a black rectangle. The device set works.

The deciding argument was not performance but **driver signing**. virtio-win
binaries are test-signed for Windows 8+ and Microsoft-attestation-signed for
Windows 10+; they are explicitly *not* WHQL-signed, which needs a paid RHEL
subscription. Attestation signing is accepted on Windows 10/11 client, but it
is a genuine risk on Server and under Secure Boot — where the vendor's own
instructions are to install a test certificate first. Making "browse to a
folder, then trust a test certificate" a mandatory step of first boot, on a
screen with no copy-paste, is a poor first experience to buy I/O throughput the
user has not yet had a chance to want.

The I/O gap is recoverable and (b) gets cheaper later: virtio can be installed
from a running desktop with a working network, where a wrong click is an error
message rather than an install that cannot proceed.

**Consequences.** Windows guests are slower at I/O than Linux ones until the
user upgrades them. `guest_os` became load-bearing rather than advisory — it
now selects disk bus, NIC model, display and CPU model together, from
`GUEST_PROFILES` in `app/engines/qemu.py`, and is persisted in the runtime file
so a restart re-attaches identical hardware. Handing an installed Windows guest
a different disk bus on its second boot would be an unbootable VM, not a slow
one.

## 28. Windows 11 is not supported, and no QEMU upgrade would change that

**Context.** Windows 11 requires TPM 2.0. QEMU's TPM support needs an external
`swtpm` daemon, and the build here rejects `-tpmdev` as an invalid option with
no `tpm-tis`/`tpm-crb` device offered.

The obvious reading is "this build is missing a feature; upgrade". That reading
is wrong, and the difference matters because acting on it wastes an afternoon.
QEMU's `meson.build` gates the feature on the host OS:

```meson
have_tpm = get_option('tpm') \
  .require(host_os != 'windows', error_message: 'TPM emulation only available on POSIX systems')
```

**No QEMU release on a Windows host has TPM emulation.** It is not a version
problem, and there is no version to upgrade to.

UEFI has a related but separate story. OVMF firmware ships and works — measured
rendering at 1280x800, 66 colours — but only via `-bios` with a recombined
firmware image. The normal `-drive if=pflash` route dies instantly under WHPX
with `Failed to emulate MMIO access with EmulatorReturnStatus: 2`, an upstream
bug open since 2019 and unfixed in every release since.

**Decision.** Support Windows Server and Windows 10, which require neither TPM
nor UEFI. Refuse to ship a Windows 11 option that would fail at the ISO's own
hardware check.

**Consequences.** ⚠️ **Read this before enabling UEFI for anything.** The
`-bios` workaround maps firmware **read-only**, so UEFI NVRAM does not persist.
Windows Setup writes its boot entry to NVRAM, so a UEFI install would appear to
succeed and then fail to boot on first restart — a failure that looks like
success right up until the reboot. If that bites, **legacy BIOS is a valid
fallback**: neither Windows Server nor Windows 10 requires UEFI, and SeaBIOS is
the proven path under WHPX.

Registry bypasses for the Windows 11 check exist and are not built in. Users
may apply them; a product-level switch for defeating a vendor's hardware check
is not a configuration this supports.

## 29. A known-good QEMU range, and capability probes because the range cannot see enough

**Context.** Correction to decision 5's "WHPX means `-cpu qemu64`". That rule
was written as though the accelerator were the only input. It is not: `qemu64`
exposes neither SSE4.2 nor POPCNT, and **Windows 11 and Windows Server 2025
refuse to run without them.** A Windows guest on `qemu64` is not slow, it is
unbootable.

The narrower true fact is that only *host-derived* models break WHPX. Measured
here, each surviving an 18-second run: `Nehalem`, `Westmere`, `SandyBridge`,
`Skylake-Client` and `qemu64,+sse4.2,+popcnt` all start; only `max` and `host`
die with "Unexpected VP exit".

**Decision.** The CPU model is per accelerator *and* per guest OS. Linux keeps
`qemu64` under WHPX unchanged; Windows gets `Westmere`, the oldest named model
carrying SSE4.2, so it asks the accelerator for as little as possible while
still clearing the bar Windows sets.

Alongside it, a known-good version range (`qemu_version_min`,
`qemu_version_max_tested`) is checked at startup and surfaced in `doctor`,
`/health` and Settings — **plus capability probes, which are the more important
half.** Every finding above is invisible to a version comparison: a current,
in-range, perfectly healthy QEMU still cannot give a guest a TPM. A check
reporting "10.0.94 — supported" over a Windows 11 install that cannot work
would be worse than no check at all.

"Unreleased build" is its own state rather than a comparison. This host runs
10.0.94 from a tree described `v10.1.0-rc4-12093-g…` — numerically *below*
10.1.0 while containing 12k commits more than the rc. Neither "newer" nor
"older" says the useful thing, which is that nobody else can install this exact
build.

**Consequences.** Every check here is advisory and nothing gates a launch: a
warning that is wrong costs a sentence, a gate that is wrong costs a VM. The
QEMU version is stamped on each instance at launch, so "it worked before the
host was upgraded" becomes checkable. **No download or upgrade machinery
exists, and none should**: for packaging we pin and bundle a tested QEMU, and
an application that replaces system binaries is a security and support problem.

Writing the probe surfaced a bug the unit tests could not: `qemu_system_binary`
is normally a bare PATH name, so looking for firmware "beside the binary"
resolved against the backend's working directory and reported OVMF missing on a
host where it was measurably present. Found by running the probe against the
real install — the argument for live verification, in miniature.

## 30. Windows Setup does not complete on this host, and the reason is not the accelerator

**Context.** Phases 13's device work landed and a real Windows Server 2022
install was attempted. It does not complete. This entry records what was
measured, because the obvious diagnoses are all wrong and each one costs hours.

**What happens.** Windows Setup boots, draws its logo, and stops. The
framebuffer goes byte-identical, the process drops to idle — 0.2 CPU-seconds in
20 wall-seconds — and **the disk is never written**: the qcow2 stays at its
393,216-byte empty size. An idle guest is a halted guest, not a slow one.

**What it is not.** Sixteen configurations, all stuck:

| Varied | Values tried |
|---|---|
| Accelerator | WHPX, TCG |
| Chipset | q35, i440fx, pc-q35-8.2 |
| Interrupts | `kernel-irqchip=off`, default |
| CPU model | `Westmere`, `qemu64`, with Hyper-V enlightenments |
| vCPUs | 1, 2 |
| Storage | AHCI disk+CD, CD on IDE, `-cdrom`, all-IDE on i440fx |
| Firmware | SeaBIOS, OVMF via `-bios` |

Two things were ruled out that looked promising. **The display is fine**:
Alpine's framebuffer demonstrably updates under WHPX (two distinct frames
across three samples), so a frozen picture really is a frozen guest. **The ISOs
are fine**: both are structurally complete — `boot.wim` (414 MB),
`install.wim` (4.3 GB), `bootmgr`, `setup.exe` all present and readable, tail
bytes included.

**What was learnt.** Under UEFI the firmware reaches its interactive shell and
maps the installer as `FS0:`, but auto-boots nothing — with `-bios` the
firmware is read-only, so there is no NVRAM to hold a boot entry. Launching
`FS0:\EFI\BOOT\BOOTX64.EFI` by hand produces the real prompt:

```
Press any key to boot from CD or DVD......
```

Nobody was pressing one. Under legacy BIOS that prompt is drawn in **VGA text
mode** — precisely the mode decision 5 records as unrenderable under WHPX — so
for every earlier run the screenshot showed a stale logo while the guest was
asking a question. Two separately documented facts in this repository combining
into a third that neither predicts.

Pressing through the prompt gets further: on the UEFI path Setup starts, the
disk grows, and the VM then resets back to firmware.

**A second round of instrumentation narrowed this considerably, and corrected
part of it.** With `pvpanic`, a serial capture, the firmware debug console
(port 0x402) and `-no-reboot` all enabled:

- **On the legacy-BIOS path — the one the engine actually uses — there is no
  crash at all.** No `GUEST_PANICKED`, no `RESET`, no `SHUTDOWN`; the QMP event
  stream is empty and QEMU never exits despite `-no-reboot`. The guest simply
  **halts**. The "it resets" description belongs only to the UEFI path.
- **It is perfectly deterministic.** Three identical runs produced the same
  frame hash, the same untouched disk, and **byte-identical firmware logs**
  (same md5). That points at a device or firmware interaction rather than a
  timing or memory race.
- **Memory is not it.** 2 GB, 4 GB and 8 GB all halt at the identical frame.
- **The display adapter is not it.** `std`, `cirrus` and `vmware` are identical.
- **The device profile is not it.** Alpine on exactly the same hardware —
  `ich9-ahci` + `ide-hd` + `e1000e` + std VGA under WHPX — boots, reports
  `sda` and `e1000e ... eth0`, and writes 33.9 MB to the disk. So AHCI, the
  NIC and the accelerator are all fine with a non-Windows guest.
- **Serial says nothing, and that instrument is known-good**: Alpine wrote 185
  bytes to it in the same harness, every Windows run wrote zero. Windows does
  not use serial without boot debugging enabled, so this is expected rather
  than informative.

The firmware log ends in the same place every time:

```
Booting from DVD/CD...
Booting from 0000:7c00
VBE current mode=3
VBE mode set: 4118        <- 1024x768, which is the logo we see
set VGA mode 118
… ~140 lines of "VBE mode info request" …
```

— then silence. So the guest gets as far as the Windows boot manager
enumerating video modes and stops at or just after the handoff into the kernel.

**A cross-platform repeat then removed the most attractive remaining
explanation.** The same boot was run on Ubuntu 24.04 with QEMU 8.2.2 under TCG
— a different operating system, a different QEMU, a different SeaBIOS — and it
halted identically: disk untouched, one distinct frame in eighteen minutes, no
QMP events, the firmware log ending on a VBE mode set exactly as it does here.

That test was chosen because a positive result would have been *useful*:
"Windows guests need KVM or HVF" is a clean, documentable platform limitation.
It came back negative. The fault is not WHPX, not the host OS and not the QEMU
version, which leaves the media or something general about how these VMs are
constructed.

(The Linux run used a 467 MB boot-only repack of the Windows 10 ISO, since that
host has 3.5 GB free and an 11 Mbit/s link. The repack was validated here first
and reproduces the original's halt with a byte-identical firmware log, so it is
a stand-in rather than an extra variable.)

**Two further cheap tests, both negative.** `-vga none` — no display device at
all, and therefore no VBE calls whatsoever — halts identically, which falsifies
the graphics reading of the trace. The firmware log falling silent after a mode
set is simply what a normal handoff looks like; it marked the handoff, not the
fault. And the boot binaries on both ISOs (`bootmgr.efi`, `bootx64.efi`,
`cdboot.efi`, `setup.exe`) carry **valid Microsoft Authenticode signatures**, so
the boot chain is not patched. The Server ISO's filename and volume label match
Microsoft's own Evaluation Center download; only the Windows 10 one is a
third-party ESD conversion (`ESD_ISO`), and the two fail identically.

That is where the investigation stands: **a deterministic halt in early Windows
boot, with the accelerator, the host platform, the QEMU version, the devices,
the display (including no display at all), the memory size, the Windows version
and the integrity of the boot binaries all independently cleared.**

**Decision.** Ship the device work — it is correct and independently
verified — and **do not claim Windows installs**. `guest_os` selects hardware
Windows has drivers for, the version and capability probes are honest about
TPM and UEFI, and [docs/WINDOWS.md](WINDOWS.md) states plainly that the install
does not currently complete.

A Windows-specific TCG default was added mid-investigation on the strength of
"WHPX stalls, TCG progresses" and **withdrawn** when longer runs showed TCG
stalling too. It would have cost a ~30x slowdown for no working install.
Retracting it is the point: the measurement that justified it did not survive
more measurement.

**Consequences.** Windows guests can be created and boot far enough to prove
the device selection works, and cannot yet be installed. Legacy BIOS remains
the engine's boot path; UEFI is not wired in, and would need a writable pflash
varstore that WHPX cannot provide (decision 28).

The cheap instruments are now exhausted — pvpanic is silent because nothing
panics, and serial is silent because Windows does not write there. Getting
further needs evidence from *inside* the guest, which means modifying the boot
media: enabling `bcdedit /bootdebug` and a debug transport in `boot.wim`, or
booting a WinPE built with a serial debugger attached. That is a materially
bigger undertaking than another command-line sweep, and sweeping further
without it would just re-measure the same halt.

## 31. The toolchain is verified as one install, not trusted to PATH order

**Context.** For the entire project up to this point, `qemu-img` on this host
was **not** the one belonging to the installed QEMU. Multipass ships its own
`qemu-img` and puts `C:\Program Files\Multipass\bin` on the system PATH, ahead
of `C:\Program Files\qemu`. Every image probe, every overlay, every snapshot
ran through Multipass's leftover **8.0.0-dirty** binary while
`qemu-system-x86_64` resolved from the real install. Multipass itself was
retired in Phase 7 (decision 3); the binary outlived it.

Nothing detected this, and the reason is worth stating: each tool answers
`--version` about *itself*, and both answers looked fine. The version check
asked `qemu-system-x86_64` what it was and believed it, which is true and
irrelevant — it says nothing about the other half of the toolchain.

**What it actually broke: nothing measurable.** Checked after the fact rather
than assumed. The parsers were re-run against 11.1.0 output: `qemu-img snapshot
-l` still has the same columns including `ICOUNT`, `_SNAPSHOT_ROW` parses both
a plain tag and one containing a space, and `qemu-img info --output=json` still
carries `format`, `virtual-size` and `actual-size` at the top level. No
instance overlays survived on the host, so there are no artefacts written by
the old binary to re-validate. The bug was latent, not active.

**Decision.** Compare the two binaries rather than trusting PATH, as a
capability (`toolchain`) alongside TPM, UEFI and devices — so it flows into
`doctor`, `/health` and Settings through machinery that already exists. Both
the resolved directory *and* the reported version are compared. Same directory
with different versions is a half-finished upgrade; same version from different
directories is benign but still reported, because it means PATH is deciding
something nobody chose.

**Why a warning and not a gate.** Same rule as every other check here
(decision 29): advisory, never refusing a launch. A skew between the two is a
real corruption risk — they write and read the same qcow2 files — but a wrong
gate costs a user their VM, and "these came from different directories" has
benign causes.

**Consequences.** The failure this guards against is quiet by construction: a
mismatch surfaces later as a corrupt overlay or unparsable snapshot output, a
long way from the version skew that caused it. That is exactly the class of bug
worth a startup check. The `_SUPPORT_CACHE` key gained `qemu_img_binary`,
without which a caller pointing the image tool somewhere new would get an
answer computed for the previous one.

## 32. QEMU 11.1.0: the WHPX pflash bug is fixed; the Windows halt is not

**Context.** The host moved from `v10.1.0-rc4-12093-gbd0a254583` (reporting
10.0.94) to `v11.1.0-12130-ge470268ff4` (reporting 11.1.0). Still a development
snapshot, not a tagged release. All capability probes were re-run and the
Windows halt re-tested against it.

**Before and after.**

| Probe | 10.0.94 | 11.1.0 |
|---|---|---|
| TPM (`-tpmdev`) | invalid option | invalid option — **unchanged** |
| Windows devices | `ich9-ahci`, `e1000e` present | present — **unchanged** |
| OVMF via `-drive if=pflash` under WHPX | dies instantly, `WHPX: Failed to emulate MMIO access with EmulatorReturnStatus: 2` | **boots** |
| `q35` alias | `pc-q35-10.1` | `pc-q35-11.1` |
| Guest hypervisor CPUID leaf | `0x40000000` signature empty | signature `Microsoft Hv` |
| SeaBIOS `phys-bits` | 46, `valid=yes` | 40, `valid=no` |
| Windows Setup | halts | **still halts** |

**The UEFI finding is real and was measured, not read.** OVMF through a
`pflash` pair under WHPX now survives: firmware initialises the adapter to
1280x800, reaches its console, and hands off to a real bootloader — verified by
booting the Alpine ISO that way to a Linux kernel console (green/cyan console
colours in the screendump), not merely by the VM failing to die. This closes a
bug open upstream since 2019 and contradicts decision 28's claim that WHPX can
never provide a writable pflash varstore. That claim is now version-bounded
rather than permanent.

So `_uefi_capability` gained a threshold, `_UEFI_WHPX_FIXED = (11, 1, 0)`,
rather than continuing to hardcode "WHPX means no UEFI". A threshold and not a
probe because the only honest probe is booting a VM, and `/health` is polled by
the dashboard. Two measured points bracket it and the constant names the build
actually tested; builds in between are guessed, and the message says so.

**TPM is unchanged and always will be.** It is a `meson.build` gate on
`host_os != 'windows'`, not a version bug. Decision 28 stands on that point.

**The version range moves to 8.0.0 – 11.1.0.** Recording a snapshot's number in
`qemu_version_max_tested` is deliberate: `status` reports `prerelease` from the
*build string*, not from this number, so the range stays a statement about
released versions while the snapshot is still flagged as something nobody else
can install.

**The halt did not survive both new variables — see decision 33.** The upgrade
alone does not fix it, but it moves every run measurably further, and combined
with fresh install media Windows Setup starts.

## 33. Windows Setup does start — the blocker was the media, not the platform

**Context.** Decision 30 recorded a deterministic halt with the accelerator,
host platform, QEMU version, devices, display, memory and Windows version all
independently cleared, and concluded that the remaining candidates were the
media or something general about Windows on QEMU. Two cheap variables then
changed at once: QEMU 11.1.0, and fresh Windows 10 media from Microsoft's Media
Creation Tool. **It was the media.**

**The measurement.** One command line, held constant — `q35`,
`whpx,kernel-irqchip=off`, `-cpu Westmere`, 4 GB, 2 vCPU, `ich9-ahci` + `ide-hd`
+ `ide-cd`, `e1000e`, `-vga std` — varying only the ISO and the QEMU build.
`debugcon` size is a good progress proxy here because the firmware is called
back for every mode query the boot manager makes.

| Media | QEMU | debugcon | Last firmware event | Screen | Verdict |
|---|---|---|---|---|---|
| Server 2022 | 10.0.94 | 6,182 B | `VBE mode set: 4118` | logo | halt |
| Server 2022 | 11.1.0 | 6,193 B | `VBE mode set: 4118` | logo **+ spinner** | halt, later |
| Win10 (2965) | 10.0.94 | 6,250 B | `VBE mode set: 4118` | logo | halt |
| Win10 (2965) | 11.1.0 | 8,745 B | full mode enumeration, no mode set | black | halt, later |
| **Win10 (3636)** | **11.1.0** | **8,803 B** | **`VBE mode set: 4144`** | **Setup, language screen** | **boots** |

The two Windows 10 rows are the isolating pair: identical QEMU, identical
command line, differing only in the ISO. The old media enumerates every VBE
mode and then stops without setting one; the new media sets `4144` — 1024x768
at 32bpp, WinPE's GUI mode — and Windows Setup appears at +60 s.

The media differ in exactly the place that matters: `bootmgr.efi`
10.0.19041.**2965** vs **3636**, and a different `boot.wim` (446 MB vs 450 MB).
`setup.exe` is byte-identical between them, which is the tell — the fault was in
the boot chain, ahead of Setup, exactly where the firmware trace said it was.

**QEMU 11.1.0 is a real contributor but not the fix.** Every run on it goes
further than the same run on 10.0.94, and the guest-visible reason is in the
firmware trace: the new build reports the hypervisor CPUID leaf `0x40000000`
signature as `Microsoft Hv` where the old build left it empty, and SeaBIOS
`phys-bits` drops from 46/`valid=yes` to 40/`valid=no`. Windows takes materially
different early-boot paths when it believes it is on Hyper-V. Neither change on
its own starts Setup.

**What this retracts.** Decision 30's "It is not the ISOs" line was wrong, and
the reason it was wrong is instructive: the check behind it was *structural* —
`boot.wim` present, `install.wim` present, `setup.exe` present, tail bytes
readable — and every one of those was true of media whose boot manager could not
get through. Structural completeness is not functional integrity. The
Authenticode check had the same blind spot: all four boot binaries on the old
ISO are validly Microsoft-signed. A correctly signed, structurally complete,
officially built ISO still failed to boot.

The docs also inferred from the `ESD_ISO` volume label that the Windows 10 media
was a third-party repack. That inference is separately wrong — the new media
from Microsoft's own Media Creation Tool carries the same `ESD_ISO` label, so
the label describes how the ISO was assembled, not by whom.

**Consequences.** Boot debugging — `bcdedit /bootdebug` plus a debug transport
in `boot.wim`, the planned next step and a materially larger undertaking — is
**not needed** and was not started. Windows Server 2022 still halts on its
existing media; by the same logic that resolved Windows 10, fresh Server media
is the cheap next test, not another command-line sweep. Nothing in the engine
changed to produce this result: the device profile from decision 27 was correct
all along and is now confirmed against a Setup that actually runs.

## 34. The media checks passed because they were aimed at a boot path the engine never uses

**Context.** Decision 33 established that the blocker was the install media. This
entry is about why every check available at the time said the media was fine.
That is a reasoning failure rather than a wrong fact, and it is the more useful
half to keep.

**What was checked.** `verify_media.py` asserted a Microsoft volume label, ran
Authenticode over `/bootmgr.efi`, `/efi/boot/bootx64.efi`,
`/efi/microsoft/boot/cdboot.efi` and `/setup.exe`, confirmed `boot.wim`,
`install.wim` and `setup.exe` were present and readable, and reported a SHA256
with nothing published to compare it against. Everything passed.

**What actually runs.** The engine boots legacy BIOS under SeaBIOS — decision 30
fixed that as the engine's path and it has not moved. The legacy chain is
El Torito `boot/etfsboot.com` → `bootmgr` → `sources/boot.wim` → WinPE →
`sources/setup.exe`. Three of the four signature-checked files — `bootmgr.efi`,
`bootx64.efi`, `cdboot.efi` — are UEFI-only and are never executed on that path.
The fourth, the root `/setup.exe` stub, runs only once WinPE is already up, which
is well past where the failure happened.

**The measurement, taken now against both Windows 10 ISOs.** Legacy-path
components first, UEFI components below the rule:

| Component | old ISO (halts) | new ISO (boots) | |
|---|---|---|---|
| `boot/etfsboot.com` | `F425E135…`, 4,096 B | `F425E135…`, 4,096 B | identical |
| `bootmgr` | `4EEAC11B…`, 413,738 B | `4EEAC11B…`, 413,738 B | identical |
| `sources/boot.wim` | `C3EDD4A5…`, 446,626,405 B | `F54F1398…`, 450,382,622 B | **differs** |
| `/setup.exe` (root stub) | `30043368…`, 74,184 B | `30043368…`, 74,184 B | identical |
| `bootmgr.efi` | 10.0.19041.2965 | 10.0.19041.3636 | differs, not executed |
| `efi/boot/bootx64.efi` | 10.0.19041.2965 | 10.0.19041.3636 | differs, not executed |
| `efi/microsoft/boot/efisys.bin` | `1525B5AB…` | `1525B5AB…` | identical |

On the path that actually ran, **exactly one component differs: `boot.wim`.**
Everything ahead of it is byte-identical. Decision 33 cited the `bootmgr.efi`
2965→3636 gap as the tell; that is a fair proxy for the media's servicing
vintage, but it is not the faulting component, because on a legacy boot that
file is never opened.

**The caveat named the wrong file.** At the time, `etfsboot.com` was flagged as
the one component that could not be signature-checked, and was then ranked below
other hypotheses. Ranking it down was the visible error, and it is the one that
looks obvious in hindsight. The measurement says something less comfortable:
`etfsboot.com` is byte-identical between media that halts and media that boots,
so the flagged component was not the fault either. The caveat picked the right
*category* — unsignable early-boot code sitting in the real path — and the wrong
member of it. Naming a gap at all then made the remaining checks feel more
thorough than they were.

**The gap that mattered was never named.** `boot.wim` is 446 MB, sits in the
executed path, contains the entire WinPE that failed to start, and was checked
only for existence. It is the one file that differed. It was also perfectly
checkable — a WIM carries an integrity table and its contents are signed PEs — so
this was not a blind spot forced by the format, the way `etfsboot.com` genuinely
is. It was simply not looked at.

**The rule this leaves.** A verification suite reports on the files it opens, not
on the media. Before a green result is allowed to move a ranking, check the
overlap between what the suite validates and the code path under test. Here the
overlap was empty: four files validated, none of them executed. "All checks
passed" was true, repeatable, and carried no information whatsoever about the
failure. Where the checked set and the executing set do not intersect, a pass is
not weak evidence — it is no evidence, and it should have moved the ranking by
nothing rather than by a little.

**Follow-up, not yet done.** `verify_media.py` should verify along the boot path
instead of along the list of conveniently signable files: hash `etfsboot.com` and
`bootmgr` and compare them across media rather than trying to sign them, and
validate `boot.wim`'s integrity table and record its size and hash. The tool as
it stands would clear the same bad ISO again.

## 35. UEFI NVRAM persists through a pflash varstore, closing decision 28's open risk

**Context.** Decision 28 ruled UEFI out partly because WHPX could not carry a
writable `pflash` varstore, leaving `-bios` as the only route — read-only
firmware with no persistent variable store at all. Decision 32 measured that
`pflash` itself survives under QEMU 11.1.0. That left the question one step
short: surviving is not the same as retaining. Whether a varstore holds a
variable across a power cycle was still untested, and it is the property that
actually matters. It has now been measured.

**Method — no guest OS involved.** OVMF puts its console on the serial port as
well as on the VGA adapter, so `-serial tcp:` makes the firmware's own UEFI Shell
(v2.2, EDK II, UEFI v2.70) fully scriptable. That removes the guest, its
bootloader and its drivers from the experiment, so nothing but the firmware and
the varstore is under test. Three boots against a per-VM copy of `OVMF_VARS.fd`
(540,672 B), with `OVMF_CODE.fd` (3,653,632 B) attached read-only:

```
qemu-system-x86_64 \
  -machine q35,accel=whpx,kernel-irqchip=off -cpu Westmere -m 2048 \
  -drive if=pflash,format=raw,unit=0,readonly=on,file=OVMF_CODE.fd \
  -drive if=pflash,format=raw,unit=1,file=<per-VM copy of OVMF_VARS.fd> \
  -vga std -display none \
  -serial tcp:127.0.0.1:45551,server,nowait \
  -qmp tcp:127.0.0.1:45552,server,nowait -net none
```

| Boot | Varstore | Action | Result |
|---|---|---|---|
| 1 | fresh copy of template | write `P13Nv` with `-nv`, `P13Vol` without; read both back; `reset -s` | both readable in-session; QEMU exits 0; file sha256 `50c03914…` → `61707c2f…` |
| 2 | **the same file** | read both | `P13Nv` → `41 50 41 53 53 31 33`; `P13Vol` → `setvar: Unable to get` |
| 3 | fresh copy of template | read `P13Nv` | `setvar: Unable to get` |

**Why there are two controls.** Boot 2's volatile variable is the discriminator:
had `P13Vol` come back too, the result would only have shown that the shell can
re-read a live variable store, not that anything reached disk. Boot 3 is the
other half — it proves the surviving value is carried by the varstore *file*, and
not by the firmware image, by QEMU, or by something cached on the host.
Non-volatile survives, volatile does not, and a fresh file has neither. That
combination is the signature of working NVRAM and of nothing else.

**A note on the syntax, because it cost a run.** The UEFI Shell's `setvar` reads
the first character inside the quotes as a type prefix — `P` device path, `L`
unicode, `H` hex. `="PASS13"` therefore stores a device path built from "ASS13",
not the string. The first attempt asserted on ASCII bytes that were never
written, and printed "NVRAM DOES NOT PERSIST" directly underneath a transcript
showing the variable surviving. The verdict line was wrong and the data was
right — the same failure mode as decision 34 at a much smaller scale: the
assertion and the thing being tested had drifted apart.

**What this changes.** Decision 28's NVRAM objection is now bounded the way
decision 32 bounded the pflash bug: on QEMU ≥ 11.1.0 under WHPX, UEFI has a
working, persistent, per-VM variable store. The reasons Windows 11 stays
unsupported are TPM — a `meson.build` gate on `host_os != 'windows'`, which no
version will move on this host — and Secure Boot key enrolment. NVRAM is no
longer one of them.

**What this does not change yet.** The engine has no pflash support: `qemu.py`
emits no `-drive if=pflash` arguments, and `_uefi_capability` reports an
availability that nothing consumes. Wiring it in means copying `OVMF_VARS.fd`
per instance at launch and recording it in the runtime file so a restart
re-attaches the same one — the rule disks and volumes already follow, and it is
load-bearing here, since a shared template would let one instance's boot entries
show up in another. Legacy BIOS remains the default boot path: Windows Setup
boots on it, and nothing measured so far argues for moving.


## 36. The Windows "halt" tracks host free memory, not Windows, QEMU or the accelerator

> **SUPERSEDED BY DECISION 39.** The main claim below is wrong. The blocker is
> `-vnc`, which the engine attaches to every launch; host memory is a real but
> secondary effect confined to guests larger than the free RAM. The reasoning
> that produced this entry — and the confounded control that retired the correct
> hypothesis — is dissected in decision 39. Kept unedited below, because how it
> was got wrong is worth more than a silent fix.

**Context.** Decision 30 called it a deterministic halt. Decision 33 blamed the
media and was partly right — fresh media does reach Setup. But Setup still never
finished, Server 2025 on January-2026 media never got past the boot logo, and the
leading hypothesis had become Hyper-V enlightenments, since QEMU 11.1.0 newly
exposes the `0x40000000` CPUID leaf and that is exactly when the symptom changed
from "dies in firmware" to "spins in kernel". That hypothesis was tested. It is
wrong, and testing it turned up the actual variable by accident.

**The enlightenments result, first, because it is cleanly negative.** One command
line, Windows 10 media, varying only `-cpu`:

| `-cpu` | Time to the Setup language screen |
|---|---|
| `Westmere` | 21 s |
| `Westmere,-hypervisor` (leaf hidden) | 21 s |
| `Westmere,hv-relaxed,hv-vapic,hv-spinlocks,hv-time,hv-synic,hv-stimer,…` | no Setup in 315 s |

Hiding the hypervisor leaf changes nothing; supplying the enlightenments makes it
strictly worse. The leaf is not the cause.

**The number that mattered was 21 s.** The same media through the engine had taken
**~13 minutes** to reach that screen. A 37× gap between two nearly identical
command lines is a bigger fact than the thing being tested, and chasing it is
what found the cause. The first suspect was `-vnc`, which the engine always passes
and the hand-rolled line omitted — and adding `-vnc` did reproduce the slowness.
That looked conclusive. It was not: **the control failed.** Re-running the
*unmodified* baseline immediately afterwards also failed to reach Setup in 300 s.
The variable was not `-vnc`; something was drifting between runs.

**What was drifting is host free RAM.** This host has 15.5 GB total, and at the
time of the failing runs 3.2 GB free against 26.4 GB committed. A 4 GB guest does
not fit in that, and QEMU under WHPX does not fail when its guest RAM is being
paged by the host — it thrashes, indefinitely and silently.

| Guest RAM | Result | Free host RAM at start |
|---|---|---|
| 2 GB | Setup at 21 s | ~3.2 GB |
| 2 GB (repeat) | Setup at 21 s | ~3.2 GB |
| 3 GB | no Setup in 300 s | ~3.2 GB |
| 4 GB | no Setup in 300 s | ~3.2 GB |
| 4 GB (earlier, more free RAM) | Setup at 21 s | higher |

The last row is the important one and the reason this is stated as "tracks free
memory" rather than "4 GB is too big": the same 4 GB configuration succeeded
earlier in the session and failed later. The controlling variable is what is
*available*, not what is requested.

**Every symptom fits, and fits better than any previous explanation.** Both vCPUs
pegged at ~200% with zero guest disk writes for 36 minutes is what a guest looks
like when its pages are being served from a swap file. So is a framebuffer that
changes once every several minutes. So is Setup reaching "Getting files ready for
installation (1%)" and stopping there — that step expands `install.wim`, the most
memory-hungry part of the install. And so is the loose end in decision 30's own
test comment, that TCG stalled too, "further along, still writing nothing to
disk": host memory pressure is accelerator-independent, where a WHPX
enlightenment bug would not be.

**What this retracts.** "Windows Setup does not complete on this host" stands as
an observation and falls as a diagnosis. It was never a Windows defect, a QEMU
defect, a WHPX defect, or — for the halt itself — a media defect. The media
finding in decision 33 is separately real (old media genuinely does not reach
Setup) but it was never the whole story, and the phase spent a long time
searching the guest for a fault that was in the host's memory budget.

**What is NOT yet proven.** That a 2 GB Windows guest *installs to completion*.
Only "reaches Setup quickly" is measured. The install phase is longer and
hungrier, and 2 GB is Microsoft's floor rather than a comfortable figure, so it
may simply move the wall. Establishing that needs one uninterrupted install run
and has not been done.

**Consequences for the product.** The `windows` preset is 2 vCPU / 4 GB / 40 GB
and the Windows floor is 2 GB. Capacity checking (decision 9) allocates against
*configured* totals, which is why nothing refused this launch: by its books the
memory was available. A host-level check of genuinely free RAM at launch time is
the obvious follow-on, and refusing — or at least warning — beats handing someone
a VM that thrashes forever while reporting Running. That is a scoped item, not
done here.

## 37. Liveness has three states, because a busy monitor is not a stopped VM

**Context.** `_is_running` was `pid_alive(pid) and is_responsive(qmp_port)`. QEMU's
QMP chardev serves **one client at a time**, so while anything else holds that
socket the handshake cannot complete and the check returned False. The reconciler
then rewrote the row to Stopped and cleared the pid — for a VM that was alive and
healthy throughout.

**Measured.** A 40-minute Running/Stopped flap at roughly one flip per reconcile
cycle, on an instance whose process never died. It stopped within one cycle of the
competing client disconnecting. The competing client was this project's own
screendump tooling; it would equally be a future console feature, a second backend
process, or anyone with `nc`.

**Why the check was not simply loosened to the pid.** The QMP half was guarding a
real case: a *recycled* pid, where the stored number now belongs to an unrelated
process and `pid_alive` means nothing. Dropping it would trade a flap for a VM
that reports Running forever after it has gone.

**Both are served, because the two cases are distinguishable one level down.**
QEMU holds its QMP port for as long as it lives. A busy monitor still has
something bound to that port; a dead QEMU has released it. So the tie-break is
whether the port can be bound:

| pid alive | QMP answers | port bindable | verdict |
|---|---|---|---|
| no | — | — | `stopped` |
| yes | yes | — | `running` |
| yes | no | no | `unreachable` — something else holds the monitor |
| yes | no | yes | `stopped` — QEMU is gone, the pid is someone else's |

`_liveness` returns those three states; `_is_running` keeps its boolean contract
and counts `unreachable` as up, which is the conservative answer in both places it
matters. The reconciler will not demote a live VM, and `_refuse_if_running` will
not let a clone or snapshot read a disk a live QEMU may be writing merely because
its monitor was busy. The unreachable case logs a warning naming the one-client
limitation, so the next person sees a cause instead of a flap.

## 38. UEFI stays detected-but-unwired, and says so

**Context.** Decision 35 established that OVMF through a `pflash` pair works under
WHPX on 11.1.0 and that its NVRAM genuinely persists. The capability probe then
reported `available: true`, which was true of the *host* and false of the
*product*: the engine emits no `-drive if=pflash` arguments at all, so no instance
can boot UEFI. Nothing consumed the flag, so nothing broke — it simply told
readers a feature existed.

**The probe now answers "can an instance boot UEFI here", which is no.** Both
firmware-present branches return `available=False` with a detail that says
"detected, not wired into launches", while still carrying the pflash-under-WHPX
finding so decision 35 is not lost. Deliberately with **no `consequence`**: that
field feeds `/health` warnings, which are about defects in *this build* and are
meant to be acted on. An unbuilt feature is a roadmap item, and putting it in the
warning list would blur two different things. Below the 11.1.0 threshold the entry
keeps its consequence, because there the build really cannot do it.

**The wiring, scoped and not started.** It is not the pflash arguments, which are
two lines. It is:

- `InstanceRuntime` gains `firmware` and `nvram_path`; `LaunchOptions` gains
  `firmware`; `_allocate_runtime` copies `OVMF_VARS.fd` per instance
- `build_launch_command` emits the pair
- `boot_cloned_instance` needs its own varstore copy — a clone must not share one
- DB column and migration, `InstanceCreate` field, API refusal when the capability
  is unavailable, CLI flag, launch-modal control, instance-detail display
- **An open design question that should not be answered in passing:** snapshots
  capture `disk.qcow2` only. A UEFI instance's NVRAM is not in the snapshot, so
  restoring one leaves boot entries describing a disk state that no longer exists.
  Options are snapshotting the varstore alongside, refusing snapshots for UEFI
  instances, or accepting the desync and documenting it. That is a decision, and
  it is not taken here.

Legacy BIOS remains the default and the only boot path. Windows Setup boots on it,
and nothing measured argues for moving.


## 39. It is `-vnc`, not memory: the console the engine always attaches is what breaks Windows

**This corrects decision 36, which is wrong on its main claim.** Host memory is a
real but secondary effect. The blocker is `-vnc`, which the engine passes on every
single launch.

**How decision 36 went wrong is the useful part.** The `-vnc` hypothesis was
raised, tested, and appeared confirmed — adding `-vnc` reproduced the failure
exactly. It was then discarded because the control failed: re-running the
"unmodified baseline" *also* failed, which looked like run-to-run drift and sent
the investigation to host memory. **The control was confounded.** It ran at the
script's default `-m 4096`, while the runs it was being compared against had been
2 GB. So it varied two things at once and its failure proved nothing. A control
that does not hold every other variable is not a control, and here it did more
damage than having no control at all — it retired a correct hypothesis.

**The completed 2×2, one command line, Windows 10 media, time to Setup:**

| Guest RAM | `-vnc` | Result |
|---|---|---|
| 2 GB | no | **21 s** (reproduced 3×) |
| 2 GB | yes | nothing in **900 s** |
| 4 GB | no | 21 s early, nothing later (memory-dependent) |
| 4 GB | yes | nothing in 300 s |

The decisive pair is the first two rows, run back to back at identical free RAM,
differing only in `-vnc`: 21 seconds against nothing in fifteen minutes.

**It is not memory pressure, and that is measured rather than argued.** During the
900-second `-vnc` run the QEMU process held a working set of **2112 MB** — the
whole 2 GB guest, fully resident — while both vCPUs sat pegged at ~197%. Nothing
was being paged. Decision 36 read "pegged CPU, no disk writes" as thrashing; it is
the same signature, and the resident working set is what tells the two apart.

**What memory does explain.** At 4 GB *without* `-vnc`, results genuinely varied
with host free RAM (~3.2 GB free against a 4 GB guest). So the decision-36 effect
is real, just secondary and confined to guests larger than what is actually free.
At 2 GB it never appears.

**What this explains that nothing else did.** Every Windows failure in this phase
came through the engine, and the engine always emits `-vnc`. Server 2025 stopping
at the boot logo, Windows 10 taking thirteen minutes to reach Setup and then
wedging at "Getting files ready for installation (1%)", both vCPUs pegged with no
guest disk I/O — all of it is one cause. The hand-rolled command lines that looked
"the same" were never the same: they had no `-vnc`, which is exactly why decision
33's hand-rolled run reached Setup when the engine's could not.

**The tension this creates, which is not resolved here.** Windows guests have no
SSH and no cloud-init; the console *is* the access path, and the console is VNC.
So the device profile is correct, the media is fine, the accelerator is fine — and
the one feature Windows depends on is the one that makes it unusable. That is a
design problem, not a bug to patch in passing. Options not yet assessed: attaching
the VNC display only while a client is connected, a different display path for the
install phase, or accepting a slow install and measuring whether an *installed,
idle* Windows tolerates `-vnc` better than Setup's continuous redraw does.

**Mechanism, hypothesised and not confirmed.** With `-display none` alone QEMU
registers no display listener and the VGA surface is only read when something asks
for it. `-vnc` registers one with a refresh timer, so the std VGA surface is pulled
periodically. Under WHPX — the accelerator this project already documents as
lacking dirty-memory tracking (see the snapshot notes on decision 15's
implementation) — that read appears to be ruinously expensive. Stated as a
hypothesis because it has not been isolated; the *effect* is measured and
reproducible, the explanation is not.

**Amendment, same session: dropping `-vnc` is not a usable fix on its own.**
Removing it makes the guest fast, and simultaneously makes it undriveable: with
`-display none` and no VNC, QMP `send-key` does not reach the guest at all.
Measured, not inferred — an entire Setup key sequence was sent blind and
`disk.qcow2` never grew past its initial 393,216 bytes, while the same sequence
with a display present advanced Setup screen by screen. The guest was healthy
throughout (1% CPU, idle, waiting for input), so this is lost input rather than a
hung VM.

**The fix for that is a USB HID keyboard**, and it is cheap: `qemu-xhci` plus
`usb-kbd` (and `usb-tablet` for pointer). With those attached and still no
`-vnc`, keystrokes land immediately and Setup drives normally through all seven
screens to disk selection. The engine currently emits neither — Windows guests
get only the implicit PS/2 devices — so this is a concrete engine change, not a
workaround.

**And memory is further demoted.** With the install running and free memory down
to 1.35 GB available of 15.5 GB, `\Memory\Pages/sec` measured **zero** hard
faults. Nothing was paging. The install's crawl through "Getting files ready for
installation" is one core at ~98% with no disk writes — CPU-bound, not
memory-bound. Decision 36's mechanism is wrong in the install phase too, not only
at boot.

**Status: the install is still not completed.** It progresses — 1% to 2% over
roughly ten minutes — which does not finish in any reasonable session. Whether
that rate is intrinsic to this host or another instance of the same
display/accelerator interaction is **not established**, and is the open question
this phase now turns on.

**Standing correction to method.** Two hypotheses in this phase were retired by
evidence that did not actually bear on them: "it is not the ISOs" (decision 34,
checks aimed at an unexecuted boot path) and now "it is not `-vnc`" (a control
that changed memory as well). Both times the error was the same shape — a test
whose result could not have distinguished the hypotheses it was used to separate.
Before a negative result is allowed to kill a hypothesis, the question is not
"did it fail" but "would it have come out differently if the hypothesis were
true".


## 40. Windows guests on this host are bimodal, so a result needs three alternating runs

**Context.** Decisions 36 and 39 were each argued from runs that could not have
distinguished the hypotheses they were used to settle. The reason both slipped
through is a property of the host that is worth recording on its own, because it
invalidates the obvious way of testing anything here.

**The observation.** The same QEMU command line, unchanged, either reaches the
Windows Setup language screen in about **21 seconds** or pegs both vCPUs and
reaches nothing at all within the cap. Not slower — bimodal. Over roughly twenty
runs in one session there was no intermediate outcome.

**How badly that misleads.** One configuration failed three consecutive times,
which is exactly the shape of a deterministic defect. Two single-variable
variants of it — one dropping `hostfwd`, one moving QMP off the port the
reconciler polls — then both passed on the first try. If either had been run
alone it would have "proved" its variable was the cause. Neither is: the 3/3 was
a streak. Three separate hypotheses in this phase (`-vnc`, host free RAM, then
`hostfwd`/reconciler polling) were each confirmed and then unconfirmed this way.

**Contributing factor, not the whole story.** The install ISO is 4.9 GB and the
page cache is warmed by whatever ran before, so the first run after any cache
disturbance — deleting and recreating the disk, a `pytest` sweep, killing a 2 GB
VM — is much slower. That accounts for some of the variance and not all of it:
runs bracketed by warm-cache successes still fail.

**The rule.** No Windows-guest result on this host is admissible from fewer than
three alternating runs. A-B-A minimum, with the repeated arm on the outside, so
that a failure in the middle is bracketed by successes of the thing it is being
compared against. That is how the `-vnc` finding in decision 39 was eventually
established, and it is the only result in this phase that survives the standard.

**Consequence for the phase.** Every timing claim about Windows here that rests
on a single run — including the "1% per ten minutes" install-phase figure — is
unproven, and is flagged as such rather than quietly kept. Settling the remaining
questions wants either a quieter host or a harness that runs each arm several
times and reports a distribution instead of a number.


## 41. The database is backed up before a migration, and at no other time

**Context.** Migrations here are additive and applied on every startup by
`_apply_additive_migrations`. They have never lost data, which is not the same
as never being able to. Review item #6 asked for a backup before one runs; it
was deferred until the state directory existed to put backups in.

**The backup is taken through SQLite's online backup API, not by copying the
file.** With WAL journalling the committed state is spread across `iaas.db` and
`iaas.db-wal`: copying them one at a time captures two different moments, and a
`.db` paired with a `-wal` from a later instant is not a database, it is a
corruption waiting to be opened. The online API reads a consistent snapshot
through SQLite itself, over a separate connection so a live writer is never
blocked, and folds the WAL contents in. The result is a single self-contained
file with **no sidecars to keep with it** — which is also what reduces the
documented restore to one `cp`. Measured on the real database: a 286,720-byte
`iaas.db` with a 4 MB `-wal` produced a 290,816-byte backup, larger than its
source because it absorbed committed WAL content the file alone did not have.

**It is skipped when the migration is a no-op, and that half matters as much.**
A copy on every startup fills the retention window with identical files and
ages the one useful restore point out of it. So `_pending_migrations` computes
what the apply loop would touch *before* it runs. It deliberately mirrors that
loop's conditions rather than approximating them: if the two ever drift apart,
the result is either a copy on every restart, or — much worse — a schema change
that lands with no restore point. That coupling is the fragile part of this
design and is called out in the code, because the two must be changed together.

**Retention prunes only what this module wrote.** The backup directory is
shared with hand-made copies (Phase 13 left one, as a directory). Pruning globs
the automatic naming pattern and considers files only, so somebody's manual
backup is never tidied away — a much worse bug than keeping one file too many.

**A backup failure does not stop the backend.** The migrations it guards are
additive; refusing to start because a backup directory is unwritable would turn
a precaution into an outage. It logs an error naming the path and continues,
which is the same posture `relocate_legacy_database` already takes.

**Restore is manual and deliberately not an API.** Putting "replace the
database" behind an HTTP route means a mis-click discards live state, and the
situations that call for a restore are exactly the ones where a human should be
reading filenames. The procedure is in CONTRIBUTING, and it moves the current
database aside rather than deleting it — you cannot tell which copy is better
until after you have looked at the other one.

## 42. UEFI stays unwired — and the NVRAM snapshot question is settled anyway

**Context.** Decision 38 left UEFI detected but not wired, with one open design
question blocking it: a UEFI instance's NVRAM is not covered by a qcow2
snapshot, so restoring a snapshot desyncs the firmware's boot entries from the
disk they describe. Three options were on the table — snapshot the varstore
alongside, refuse snapshots on UEFI instances, or allow the desync and warn —
and none had been chosen.

**The question is now answered, and the answer is cheap.** It was blocking on an
assumption that turned out to be false: that a varstore has to be a raw `.fd`,
and therefore that snapshotting it means a second, external artifact per tag,
with its own naming, listing and deletion path. Measured instead:

| Probe | Result |
|---|---|
| `qemu-img convert` raw `OVMF_VARS.fd` → qcow2 | works, 540,672 B → 917,504 B |
| `qemu-img snapshot -c` on the qcow2 varstore | works |
| OVMF boots from a **qcow2** pflash varstore under WHPX | reaches the UEFI shell |
| An `-nv` variable persists across reboot in it | yes — `41 51 43 4F 57 32` read back |
| `qemu-img snapshot -a` reverts NVRAM state | yes — a variable written *after* the snapshot was gone after restore |

The last row is the one that matters. It is not enough that the file can be
snapshotted; the snapshot has to actually capture firmware variable state, and
it does.

**So the design is settled: the varstore becomes a qcow2 and is snapshotted
internally, with the same tag as the disk.** Same tool, same format, same
deletion semantics, stored inside the file. No external per-tag artifact to
orphan, no second naming scheme. The two-file partial-failure mode that made
this look expensive shrinks to two calls to the same tool, and ordering makes
the residual failure the harmless one:

- **Create:** varstore first, disk second; roll the varstore tag back if the
  disk snapshot fails. The other order leaves a *recorded* snapshot with no
  NVRAM behind it — exactly the desync being eliminated — whereas this order
  leaks an orphan NVRAM tag, which is invisible and cleanable.
- **Restore:** take an auto-tagged pre-restore snapshot of both, then apply
  both, re-applying the pre-restore pair if the second fails. Internal
  snapshots are close to free, and this is the same instinct as decision 41's
  pre-migration backup: a restore point before a destructive operation.
- **Delete:** disk first, then varstore; a missing tag is not an error.

**And none of it is being built.** The wiring is deliberately not done, because
UEFI has no user:

- Windows 11 is the only thing that requires it, and Windows 11 is permanently
  out of reach on a Windows host — QEMU excludes TPM emulation at build time
  (decision 28), which no upgrade changes.
- Legacy BIOS boots everything that currently works, including the Windows
  Setup that Phase 13 drove to disk selection.
- The wiring is not small: `firmware` and `nvram_path` on `InstanceRuntime`,
  `firmware` on `LaunchOptions`, a per-instance varstore copy, the pflash pair
  in `build_launch_command`, `boot_cloned_instance` carrying it through, a DB
  column and migration, an `InstanceCreate` field, API refusal when
  unavailable, a CLI flag and a UI control — plus the snapshot work above.

That is a lot of surface area, all of it reachable only by a user who does not
exist yet. `_uefi_capability` continues to report "detected, not wired into
launches", which is the honest description and remains true.

**What this entry is for.** The expensive part of a decision like this is
usually the investigation, not the code — and the investigation is done. If
UEFI ever acquires a reason to exist (a Linux guest that needs it, a host where
TPM is available, Secure Boot testing), the design does not have to be
rediscovered: the mechanism is measured, the ordering argument is written down,
and the only remaining work is typing. Recording a settled decision that is
deliberately not acted on is cheaper than leaving the question open and paying
to reopen it.


## 43. Volume snapshots get their own table, and are gated on "no running instance"

**Context.** Phase 11 built volumes and deliberately left snapshots out: a volume
is a plain qcow2 and `qemu-img` would work on it, but it needed its own lifecycle
and its own confirm semantics rather than being folded into instance snapshots.
Two questions had to be answered first.

### Why a separate table rather than one with a discriminator

`Snapshot.instance_id` is `NOT NULL` with a real foreign key. Sharing the table
would need it nullable, alongside a nullable `volume_id` and a mutual-exclusion
rule enforced in application code — two columns that are each meaningless for
half the rows.

The semantics are also opposite. `Snapshot`'s own docstring says destroying the
instance destroys its snapshots, because they live inside an overlay that goes
with the instance directory. A volume outlives every instance it is attached to
— that is the entire point of the feature (decision 23) — so its snapshots must
outlive them too. A shared table with an instance foreign key invites exactly the
cleanup that would delete them at the wrong moment.

The decisive argument is practical. **Making a `NOT NULL` column nullable in
SQLite is a table rebuild** — the create-copy-swap that `_REDEFINED_INDEXES`
explicitly notes this project does not do, its migrations being additive only.
Reuse would have required the one migration this codebase has never performed, on
the table that holds users' restore points. A new table is created by
`create_all` with no migration at all.

### Why the gate is "no running instance", not "detached"

The invariant that matters is the one decision 15 established: `qemu-img` writing
to a qcow2 that a live QEMU has open is how the file gets corrupted, and on
Windows there is no lock to prevent it. A volume attached to a **stopped**
instance is not open by anything, so snapshotting it is exactly as safe as
snapshotting a detached one.

Requiring a detach first would therefore add no safety and real friction, because
decision 22 already requires a stopped instance to detach at all: the strict rule
turns `stop → snapshot → start` into `stop → detach → snapshot → attach → start`.
So the check asks about the *instance's* state, which is the thing that actually
determines whether a process holds the file, and the 409 says the volume can stay
attached rather than implying it cannot.

The same guard applies to restore, where it matters more. Snapshotting under a
live guest captures a bad copy; restoring under one replaces the filesystem the
guest believes it has.

### What is said in both dialogs

An instance snapshot covers the instance's overlay and **not** its attached
volumes. A volume snapshot covers the volume and **not** the instance it happens
to be attached to. Two independent operations on two independent files; restoring
one does not restore the other. Both dialogs say their own half and point at the
other, because the moment this matters is the moment someone is about to discard
data on the assumption that one covered both.

### What was left out

**No events.** The event log is an instance's history by schema
(`InstanceEvent.instance_id`), and a detached volume has no instance to hang one
on. A null-instance row would make the feed look complete at the cost of every
reader of that table having to special-case it. If volume history is wanted later
it needs its own thinking, not a nullable column here — which is the same
argument as the table above.

**One thing the tests caught that reasoning did not.** Deleting a volume drops its
snapshot rows, and with no ORM relationship declared between the two tables
SQLAlchemy is free to emit the volume's `DELETE` first — which trips the foreign
key under `PRAGMA foreign_keys=ON` and fails the whole request. The rows are
flushed explicitly before the volume is deleted. Ordering statements is the
deleting function's job because it is the only place that knows the dependency
exists.


## 44. Change detection that omits a field silently discards writes to it

**Context.** Phase 14 Part E added `monitor_reachable` — the engine's report that
a VM's process is alive but its QMP monitor will not answer. Every layer tested
green: the probe returned False under contention, the engine returned
`monitor_reachable=False` with the status still Running, and the model turned
that into Degraded with its own explanation. Driven through the API against a
live backend, the row stayed `monitor=True`. The phase shipped with that gap
flagged rather than explained, and "probably uvicorn reload flakiness" as the
guess. **The guess was wrong.**

**The mechanism.** The reconcile pass is deliberately structured as
read-decide-write in one boundary, and it commits only when something changed:

```python
before = _row_snapshot(instance)
_apply_info(instance, info)
after = _row_snapshot(instance)
if before != after:
    session.add(instance)
    updated += 1
if updated:
    session.commit()
```

`_apply_info` assigned `instance.monitor_reachable = False`. `_row_snapshot`
returned a hand-written tuple of seven attributes that did not include it. So
`before == after`, the row was never added, `updated` stayed zero, no commit was
issued — and the assignment was discarded when the session was next rolled back.
The write happened. It just never landed.

**It was wider than the new field.** `_apply_info` also writes `accel`,
`display` and `ssh_enabled`, and none of those were in the tuple either. They had
survived by luck: they are set on the same pass that first moves `status`, and
that pass commits for the status change, carrying them along. A change to any of
them *on its own* — a VM relaunched under a different accelerator, say — would
have been discarded the same way. `monitor_reachable` only exposed it because it
is the first such field that flips repeatedly while everything else holds still.

**The fix is structural, not a longer list.** `_SNAPSHOT_FIELDS` is now the one
definition and `_row_snapshot` builds its tuple from it, so the names used to
describe a correction and the values used to detect one cannot drift apart. The
guard is a test that drives `_apply_info` with an `InstanceInfo` differing in one
field at a time and asserts the snapshot notices — rather than re-listing the
fields, which is the mistake being guarded against. Reverted against the old
list, it fails on `accel` first, which is how the wider hole was found.

**The lesson, which is not new here.** This is the same shape as decision 34: a
check that passes because it is looking at something adjacent to the thing that
matters. There, four signed files were validated and none of them executed. Here,
seven fields were compared and the one that changed was not among them. In both
cases every individual layer was correct and the composition was not, and in both
cases the failing observation was available early and got explained away —
"structurally complete media" then, "probably a reload artefact" now.

The general rule: **when a system decides whether to persist by comparing a
projection of its state, the projection is part of the write path.** A field
absent from it is not merely unreported; it is unwritable. Adding a field to the
model and to the code that computes it is two of the three edits.

**Verified end to end on a freshly started backend**, no `--reload` involved:
holding the QMP socket from another process moved the row to Degraded with the
monitor's own explanation, kept `status` Running and the pid intact across
repeated reconciles — where the pre-Phase-13 code flapped to Stopped — and
returned to healthy when the socket was released.


## 45. Every localhost port is the same site, so CSRF tokens are permanent

**Context.** Phase 15 gave the browser a session cookie. The usual reasoning is
that `SameSite=Strict` plus a same-origin deployment makes CSRF protection
unnecessary, and that packaging would eventually deliver the same-origin half.
Both halves are wrong here, and the second is wrong in a way that would never
have shown up as a bug.

**`SameSite` is computed from scheme and registrable domain. Port is not part of
a site.** So `http://localhost:9999` and `http://localhost:8000` are the *same
site*, and a cookie set by this backend is sent with requests originating from a
page on any other local port — a stale dev server, a docs preview, a package's
build tool. For a tool that binds to loopback, `SameSite` therefore defends
against nothing that matters, and no amount of same-origin packaging changes it.

**Measured, because the argument is easy to get backwards.** Chrome, an isolated
backend on 8100, two pages:

| Step | Result |
|---|---|
| login from `http://localhost:8101` | 200, cookie set, `document.cookie` empty (httpOnly holds) |
| plain form POST from `http://localhost:8102` | **403**, not 401 |

401 would have meant the cookie was never sent. **403 means it was sent** and
the CSRF token refused it. Controls, so the discriminator is not assumed: no
credential → 401; cookie without the CSRF header → 403; cookie with it → 200.

So the CSRF token is not defence in depth. It is the only thing standing there.
It is required on every state-changing request authenticated by cookie, stored
on the session row rather than derived, and returned in a response body rather
than a cookie — a cookie would travel automatically, which is precisely what
makes cookies unsuitable for this job.

**Bearer tokens are exempt, and that is not a hole.** A browser cannot be
induced to attach an `Authorization` header to a cross-origin request, so the
header's presence is itself evidence the caller intended the request.

**The same finding retired `cors_origin_regex`.** It existed so a developer
could widen origins for a session when Vite took a different port, and its
documented example matched *any* port on localhost. With no authentication that
cost nothing — the API was open, so CORS was not what protected it. With
`allow_credentials=True` and a session cookie it would make any page on any
local port a fully authenticated API client. Removed rather than narrowed; the
fix for a port collision is to free the port.

## 46. Token secrets are hashed with SHA-256, deliberately not with a KDF

**Context.** Passwords here use argon2id. Session secrets and API tokens use a
single SHA-256. That looks inconsistent — "secret means KDF" is a strong and
usually correct instinct — and it is the kind of thing a later contributor
"fixes". This entry exists so that fix does not happen by accident.

**A KDF exists to make *guessing* expensive.** It is slow on purpose, because
the attacker's advantage against a password is that humans choose from a small,
predictable space: a stolen hash can be attacked with a dictionary, and argon2's
cost per attempt is what makes that uneconomic. Every parameter it exposes —
time, memory, parallelism — is about raising the price of a guess.

**None of that applies to these secrets.** They are `secrets.token_urlsafe(32)`:
256 bits from the OS CSPRNG. There is no dictionary, no structure and no
plausible guess. Brute force against 2^256 is not slowed usefully by making each
attempt a hundred thousand times more expensive — it is already impossible, and
argon2 would move it from impossible to impossible.

**And the cost is not theoretical.** A password hash is verified when someone
logs in. A token hash is verified on **every authenticated request** — every
poll of the instances list, every CLI call, every dashboard refresh. argon2id at
sane parameters is tens of milliseconds and tens of megabytes *by design*. Using
it here would put that on the request path of the whole API: a self-inflicted
rate limit that protects nothing, and one whose memory cost is trivially
turned into a denial of service by an attacker sending unauthenticated garbage
tokens.

**What SHA-256 is doing here is the job that remains**: making the stored form
useless to someone who reads the database. It is a one-way function over a
high-entropy input, and that is exactly the situation it is fit for. The
comparison is `hmac.compare_digest`, so a timing side channel does not leak the
digest either.

**The rule, stated for whoever finds this next.** Choose the KDF when the secret
was chosen by a human. Choose a plain cryptographic hash when the secret was
chosen by a CSPRNG and is verified often. Both are in this codebase, for those
reasons, and swapping either one is a regression.

## 47. The first account is created on the host, not over the API

**Context.** Something has to create the first account, and every option has a
cost. The brief listed three: an interactive CLI command, a generated password
printed once at startup, or a setup screen reachable only from localhost.

**The setup screen is out on the same grounds as bind-dependent auth** — it
trusts topology, and any process or page on the machine is "local".

**Printing a generated password at startup** works headlessly but puts a
credential into the log stream, where it is captured by whatever collects logs
and read by anyone who can see them. It also fails the case where the backend
runs as a service and nobody is watching the console.

**So: `iaas auth init`, prompted interactively, host-local.** The password is
never a flag — a flag lands in shell history and in this project's own approved
-command list, which is the exact leak CONTRIBUTING documents — and it is
refused outright when stdin is not a terminal rather than quietly accepting a
pipe.

**It does not go through the API, and that is the point.** Creating the first
account over HTTP needs a public, state-changing route: reachable without a
credential, callable once, and whoever calls it first owns the machine's VMs. On
a non-loopback bind that is a race. Doing it on the host requires filesystem
access to the state directory instead — a *stronger* requirement than any
credential this product could check, because whoever has that directory can
already read the orchestrator's SSH private key and every VM disk in it. The
local path protects more and adds no public surface.

`auth reset-password` is host-local for the same reason plus one more: it is the
recovery path for someone who has lost their credential, so it cannot require
one. It invalidates every session and token, including this machine's.

**This is a named exception to a rule the project enforces with a test.**
`test_the_cli_never_reaches_past_the_api` forbids the CLI from importing the
database, models or settings, because a CLI that reaches past the API works on
the developer's machine, breaks when the backend is elsewhere, and gives the CLI
capabilities no other client can have. The exception is confined to one file,
`app/cli/host_admin.py`, so it is a filename rather than a scattering of
imports, and the test names it and says why.

**On not being locked out.** A backend that finds no account logs, at WARNING,
the exact command to run. The failure mode this replaces is every request
answering 401 with nothing explaining why — which is precisely what upgrading to
this version looks like from the outside.

## 48. The console is opened with a single-use ticket, not with the session

**Context.** Every other route is authenticated by a session cookie or a Bearer
header. The VNC console cannot use either. The browser `WebSocket` constructor
takes a URL and nothing else — no headers, no options object — so a Bearer token
is not available to it, and while the cookie *is* sent on the upgrade request,
relying on that would make the console the one endpoint whose authentication
cannot be reasoned about the same way as the rest.

**The URL is the only channel, and a URL is not a safe place for a credential.**
It appears in the browser's network panel, in any proxy log, and in whatever
diagnostic someone pastes into an issue. So the thing put there is built to be
worthless by the time anyone reads it:

- **Single use.** Redemption deletes the row inside the same transaction that
  reads it. A replay of a captured URL is refused.
- **30 seconds.** Long enough for a ~100 kB viewer chunk to load and a socket to
  open on loopback; short enough that a ticket in a log is expired before the
  log is read.
- **Bound to one instance.** The instance id is checked against the one in the
  path. A ticket minted for a VM you are entitled to see cannot open a
  different one.
- **Bound to the session that minted it.** It dies when that session is logged
  out or expires, so a ticket cannot outlive the authority that created it.
- **Bound to `credential_version`.** A password change invalidates every
  outstanding ticket along with every session and token, so "change the password
  because something leaked" does not leave a live console behind it.

**All four failure modes are tested explicitly**, in `tests/test_console_auth.py`
— no ticket, a ticket for another instance, a redeemed ticket replayed, and a
ticket whose session or password is gone. Rejection is uniform: close code 4401
with one message, so the socket does not become an oracle for which instances
exist.

**The consequence in the client** is that minting is a separate request that has
to complete *before* the socket is opened, which is awkward: `openSocket` in
`src/lib/console.ts` is synchronous by design — the ordering guarantee that the
viewer is loaded first depends on nothing awaiting inside it, and
`scripts/check-console-handshake.mjs` fails the build if that order is lost.
So the ticket is fetched before `connectConsole` is called at all, spending one
loopback round trip to leave that invariant untouched.

**The alternative considered and rejected** was accepting the session cookie on
the upgrade. It is less code and it works. It also means the console's
authentication is a different mechanism from every other route's, silently
depends on `SameSite` behaviour on a WebSocket upgrade, and gives a leaked URL
nothing to expire. A ticket costs one request and is auditable.

## 49. The rename is a data migration, and the guest username is not part of it

**Context.** Phase 16 renamed the product to **Kurukuru** — Yoruba for
fog/cloud. That moved the command (`iaas` → `kurukuru`), the environment prefix
(`IAAS_` → `KURUKURU_`), the state directory (`~/.local-iaas` → `~/.kurukuru`)
and the database filename (`iaas.db` → `kurukuru.db`). Every one of those has an
existing install sitting on the old value.

**The tree is not self-contained**, which is what makes this more than a
directory rename. Three kinds of absolute path point *into* it:

| Where | What |
|---|---|
| `qemu/instances/<name>/runtime.json` | `iso_path`, and each attached volume, replayed verbatim on every start |
| database rows | `keypairs.private_key_path`, `volumes.path` |
| **qcow2 headers** | an overlay's backing file, written *inside the image* as the absolute path it was created with |

The third is the dangerous one. Nothing in the database or the runtime file
mentions it, so a migration that rewrote only the first two would move the tree,
present a completely healthy dashboard, and fail every VM built on a shared base
image at launch with "Could not open backing file". It is repaired with
`qemu-img rebase -u` — the *unsafe* form, which is the correct one here: the
content did not change, only its path, so writing the header and touching
nothing else is the whole job. The safe form would be hours of I/O to produce an
identical file.

**Decision.** `kurukuru/state_migration.py` runs before the first database
connection, and is timid in the same shape as decision 26:

- only on the default layout, and only when the engine about to be used is the
  one pointing there — two conditions, because a *tree*-moving migration
  guarded by one is a fixture away from relocating a developer's real install;
- never onto a target that has contents;
- a database backup first, through Phase 14's machinery;
- the move is a `rename`, which either happens or does not — there is no
  half-moved state to recover from;
- a marker records what happened, and a second run is a no-op.

**Running VMs refuse the whole thing, and the backend does not start.** Windows
will not rename a directory containing an open file and a running QEMU holds its
disk open (Phase 13). Starting anyway would open a fresh empty database at the
new path while twenty gigabytes of the user's VMs sat untouched at the old one —
decision 26's "indistinguishable from data loss", reintroduced by a rename. The
error names each instance and its pid, and gives the one-variable escape hatch
for stopping them cleanly.

Liveness uses **both** the pid and its QMP port, the same tie-breaker
`QemuEngine._liveness` uses. A live pid alone can be a recycled number, and
refusing an upgrade because some unrelated program inherited an old pid is a
failure the user can neither diagnose nor work around.

**`IAAS_*` variables are honoured for one release, with a warning.** The three
options were refuse, ignore and honour. Ignoring is the actively dangerous one:
an operator who set `IAAS_STATE_DIR=D:ms` would find the backend pointed at
`~/.kurukuru` reporting an install with no instances in it. Refusing is safe but
turns an upgrade into an outage for the users who configured the tool most
carefully. And `IAAS_STATE_DIR` in particular has to be read *before* the
migration, because it is what says where this install actually lives. The shim
runs in the CLI too — it resolves its token file from the environment directly
and never touches `Settings`, so without it the two halves of one product would
disagree about where the install is.

**The wire identifiers moved too**, coherently on both sides: the cookie
(`iaas_session` → `kurukuru_session`), the CSRF header (`X-IAAS-CSRF` →
`X-Kurukuru-CSRF`), the API token prefix and the two `localStorage` keys. Each
had a different cost and each was paid rather than deferred:

- the cookie costs **one forced sign-in** on upgrade, and the old one is
  actively cleared rather than left to expire beside the live one;
- the token prefix is written at *issue* time only — presentation is checked by
  hash — so a token issued as `iaas_…` keeps working until it is revoked;
- the `localStorage` keys would have silently discarded a saved project
  selection and theme, so their values are carried across once;
- the CSRF header was spelled **twice**, in `kurukuru/auth.py` and in
  `kurukuru/cli/client.py`, and renaming one of them broke the CLI's own login. It now
  has one definition in `kurukuru/product.py`, which is the only module both the
  control plane and its client are allowed to import.

**The guest username stays `iaas`, deliberately.** `default_vm_user` is not a
product string: it is an identity written into a guest's `/etc/passwd` at
provision time, and the instance row does not record which name it got —
`Instance.ssh_user` returns the *live setting*. Renaming it would rewrite the
"Copy SSH" command of every existing instance into a username its guest has
never heard of, failing as "Permission denied (publickey)" and looking like a
broken key rather than a wrong user. Changing it is a two-step job: persist the
user on the row, backfill existing rows with `iaas` (correct for all of them),
and only then move the default. Until that is done, the rename stops at the
host.

**The mark appears with its descriptor.** There is a live Nintendo registration
for "KURUKURU KURURIN" in Class 009, video game programs. A single-host
hypervisor control plane is not in that category, but resembling one has a cost
and no upside — so "Kurukuru — local cloud infrastructure" rather than the bare
word, and the visual language stays plain: no pixel art, no retro-game styling,
no spinning characters. `src/ui/product.ts` is the one file allowed to spell the
brand, and `check-product-name.mjs` now enforces that as well as the command
name. Extending the guard found `Local IaaS` living in *two* files while the
script's own comment claimed it had exactly one home — the check only knew about
the lower-case command name, so the display name had never been guarded at all.

## 50. The API moved under `/api`, because the dashboard already owned those URLs

**Context.** Packaging means one process on one port serving both the API and
the dashboard. The dashboard has a client-side route per page, and the API had a
route with the identical path *and method* for each one:

| Path | The API meant | The dashboard meant |
|---|---|---|
| `/instances` | list instances | the Instances page |
| `/images` | list images | the Images page |
| `/isos` | list boot media | the ISOs page |
| `/volumes` | list volumes | the Volumes page |
| `/networks` | list networks | the Networks page |
| `/keypairs` | list key pairs | the Key pairs page |
| `/projects` | list projects | the Projects page |
| `/settings` | system settings | the Settings page |

Eight exact collisions, plus `/instances/{id}` — simultaneously a dashboard deep
link and an API resource. On two origins none of this was visible. On one it is
a direct conflict.

**The only same-path resolution is content negotiation, and it is a trap.** A
browser sends `Accept: text/html` and `fetch` sends `application/json`, so
branching on the header appears to work. But `curl` sends `*/*`, and so does
most tooling that was never told to care; each of those silently gets whichever
side the branch prefers. A tool whose response depends on a header nobody sets
deliberately is a tool that behaves differently in a terminal than in a browser,
for reasons invisible in the URL.

**Decision.** The API mounts under `API_PREFIX` (`/api`) and the dashboard keeps
the readable paths. The dashboard's are the ones a person types, bookmarks and
pastes into chat, and the brief's own requirement — that `/instances/{id}`
resolve as a deep link — settles which side moves.

One `APIRouter` collects everything and is mounted once, so there is a single
place that decides where the API lives, and a test asserts that no route is
registered outside it. The dashboard's catch-all is registered **last**;
Starlette matches in registration order, and that ordering is the entire
guarantee that an API route always wins.

`kurukuru/dashboard.py` serves the built bundle with the two rules a
single-page app needs, and one it is easy to miss:

- hashed assets are cached `immutable` for a year — the content hash *is* the
  cache key, so the name is never reused for different bytes;
- `index.html` is never cached, because it names the current hashed bundles and
  a cached copy pins the browser to the previous build's asset names, which
  after an upgrade are the files that no longer exist;
- an unmatched path **under the prefix** is a JSON 404 rather than the
  dashboard. Handing HTML to a JSON client turns a typo in a URL into a parse
  error one stack frame away from the mistake.

**Consequences, and they are breaking.** Every API path moved. The CLI and the
dashboard both append the prefix in one place each, so a user configures an
*origin* and never a path — and an origin that already carries `/api` (what you
get by copying out of the address bar after opening the docs) is tolerated
rather than doubled. `tests/test_dashboard.py` re-derives both route sets from
the frontend's own router and asserts they cannot overlap, plus a
guards-the-guard test asserting the historical collision is still visible with
the prefix stripped — otherwise the disjointness test would pass while proving
nothing.

The tables in `kurukuru/security.py` stay keyed **without** the prefix, through
one shared normaliser. Keying them by the mount point would mean that moving it
silently unprotects everything, since a path matching no key is simply not
public. Stripping fails the other way.

## 51. Loopback, an unfamiliar port, and CSRF confirmed rather than assumed

**Context.** Part B had three smaller decisions attached, and one obligation:
the brief asked whether making the dashboard same-origin lets the CSRF token
retire.

**It does not, and this was re-measured rather than reasoned about.** Decision
45 turns on `SameSite` being computed from scheme and registrable domain, with
port excluded — so a page on *any other local port* is same-site and the cookie
travels. Same-origin packaging changes nothing about a different port. Measured
in Chrome against the packaged build, signed in on `127.0.0.1:7842`, with a page
served from `127.0.0.1:8099`:

| From the other port | Result |
|---|---|
| `fetch()` with `credentials: include` | preflight `OPTIONS` → **400**; blocked by CORS before it was sent |
| plain form POST (no preflight; CORS does not gate it) | **403** |
| `document.cookie` on the backend's own origin | `""` — httpOnly holds |

**403, not 401** — the cookie *was* sent and the CSRF token is what refused it.
No project was created. Controls on the same run: no credential → 401, cookie
without the header → 403, cookie with it → 201. The token is not defence in
depth here; it is still the only thing standing there.

**CORS ships empty.** Same-origin means there is no cross-origin request to
permit, so the correct list is the empty one — and the measurement above shows
it doing real work, refusing the preflight outright. Development is the
exception and is stated explicitly rather than left as a default production
inherits: the Vite dev server needs `KURUKURU_CORS_ORIGINS` set, which
`backend/.env.example` carries commented for exactly that.

**Loopback by default, and 7842 rather than 8000.** Everything served is either
unauthenticated at the network layer or protected by a session cookie over plain
HTTP — VM consoles, SSH forwards, the API — so a wildcard bind publishes all of
it. `--host` is accepted and warns about precisely what was accepted.

8000 is contended enough to be taken on a developer's own machine most of the
time. But the number matters less than the handling, because **a fixed default
cannot be guaranteed bindable on Windows at all**: the TCP dynamic port range
starts at 1024 on a default install, and Hyper-V and WSL reserve blocks inside
it that move across reboots. So a port with nothing listening on it can still
refuse to bind, with `WSAEACCES` — which reads as "run me as administrator",
which is wrong and does not help. `serve` probes the port first and distinguishes
the two cases by name, giving the `netsh` command that lists the reserved ranges
for the one where that is the answer.

## 52. A build must not know its own origin

**Context.** Part B made the backend serve the dashboard so the product is one
process on one port. `client.ts` resolved the API's origin as:

```js
import.meta.env.VITE_API_URL?.replace(/\/$/, '') || window.location.origin
```

which reads as "prefer the configured origin, otherwise use this page's". It is
not that. **Vite inlines `import.meta.env.VITE_API_URL` as a string literal at
build time**, so once the variable is set on the build machine the literal is
non-empty, the `||` is dead code, and the fallback can never run. The build
machine's development `.env` is welded into the shipped artefact.

**Observed.** A bundle built while `frontend/.env` said
`VITE_API_URL=http://localhost:8000`, served from port 7842. The document and
every asset loaded 200 — same origin, nothing unusual — and both XHRs went to
`http://localhost:8000/api/...`, where nothing was listening. The dashboard
rendered "Cannot reach the backend. Start it, then reload" while the backend
answered `curl` on the very origin serving the page. The same path fetched from
the page's own origin returned 200 with real data.

The shape of the failure is worth recording, because it invites the wrong
diagnosis: **documents and assets succeed while XHR fails** looks exactly like
an extension blocking `xmlhttprequest` as a resource type. It was not. The
requests were not being blocked; they were being sent somewhere else. Reading
the request URL rather than the failure mode is what separates the two, and it
took one line of network log to settle what could have been an afternoon of
disabling extensions.

**Decision.** A build always talks to its own origin, and `VITE_API_URL` applies
in development only:

```js
export const API_ORIGIN = import.meta.env.DEV
  ? import.meta.env.VITE_API_URL?.replace(/\/$/, '') || window.location.origin
  : window.location.origin
```

`import.meta.env.DEV` is statically replaced with `false` in a build, so the dev
branch is eliminated and the variable never reaches the output.

**Even a correct value would be wrong to bake in.** The backend serves the
bundle, so the right origin is whatever the user reached it on — and they may
change the port, use `127.0.0.1` rather than `localhost`, or come through a
hostname or a proxy. Only `window.location.origin` is right in all of them.

**A build-time guard, because this is invisible where it is produced.**
`scripts/check-bundle-origin.mjs` fails when the built bundle contains a
loopback origin or the string `VITE_API_URL`, runs in `npm run verify` *after*
the build, and is also invoked by `tools/build_installer.py` so an automated
release cannot skip it. It refuses to pass when `dist/` is missing, because a
check that reports green with nothing to check is worse than no check. Axios's
own non-browser fallback base is exempted by name with its reason, so the check
does not cry wolf and get deleted.

Extending it immediately found two more things that had shipped: the copy
`"point VITE_API_URL at the one you meant"`, which names a build-time setting a
user of an installed copy cannot act on, and `IAAS_CORS_ORIGINS` in the
origin-refused remedy — the Phase 16 rename covered the backend, the tests and
the docs, but never `frontend/src`.

## 53. The installer is per-user, and the startup task is registered by API rather than by `schtasks`

**Context.** Part C had to put a working dashboard in front of somebody who has
never opened a terminal, without asking for administrator rights.

**Inno Setup, per-user.** `PrivilegesRequired=lowest` installs to
`%LOCALAPPDATA%\Programs`, writes the Start Menu entry and the `PATH` change
under `HKCU`, and never prompts. WiX was rejected: a per-user MSI is possible
but awkward, and its real advantage — Intune and Group Policy deployment — is
not what a stranger downloading from GitHub needs. That would be a second
installer, not a reason to start with the harder one.

**The startup mechanism is not the one recommended, because the recommendation
was wrong.** The plan said "a scheduled task at logon" and assumed `schtasks`
could create one. It cannot, unelevated:

| | |
|---|---|
| `schtasks /Create /SC ONLOGON` | **Access is denied** |
| the same, plus `/RU <me>` | **Access is denied** |
| `schtasks /Create /SC ONCE` | succeeds |
| `Register-ScheduledTask -AtLogOn`, trigger and principal scoped to the current user | **succeeds** |

A logon trigger created through `schtasks` is treated as applying to *any* user,
which is an administrator's decision. Scoped explicitly to one user through the
proper API, it is only ever asking to run something as the person asking. So the
design survives and the tool changes: `startup-task.ps1`, installed alongside so
the registration can be read rather than merely trusted.

A Startup-folder shortcut would also have worked unelevated and was rejected: a
console window at every sign-in, no restart-on-failure, and invisible in the
place Windows users are told to look.

**Uninstall keeps user data.** The state directory is frequently tens of
gigabytes of VM disks. The uninstaller asks once, explicitly, defaulting to No.

**Two bugs that only a real install could produce**, both invisible to every
test that ran before it:

- The `.ps1` was UTF-8 *without a BOM*, and Windows PowerShell 5.1 decodes a
  script as the system ANSI codepage unless it has one. The em-dashes in its
  prose became mojibake; mojibake inside a quoted string is a **parse error**.
  The installer reported complete success and registered nothing. The build now
  refuses to package a `.ps1` that lacks a BOM or that the parser rejects.
- `find_dashboard` looked for the bundle beside the **package**, and a
  PyInstaller onedir build puts the package two directories below the
  executable while the installer puts the dashboard beside it. The installed
  application served its entire API correctly and answered every dashboard URL
  with `{"detail": "Not Found"}`. The development checkout never showed it,
  because there `frontend/dist` is found instead.

Both are the argument for the live verification existing at all: neither is
reachable from a source tree.

**Verified end to end.** Installed silently with no elevation; the task
registered as the user at Limited run level and started the backend; a fresh
state directory reported `configured: false` and the dashboard offered a setup
form; an account was created and the route then answered 409; a VM launched and
reached Running; and installing 0.1.1 over a running 0.1.0 preserved the
account, the instance, every database row and the VM's disk, with the version
moving in `/health` and the CLI together.

## 54. Windows guests need an i440fx-family machine, `hpet=off`, and `rtc base=localtime` — the copy-phase stall is fixed

**Context.** Every Windows install attempted through this project's engine had
stalled somewhere in Setup's copy phase — 2% on the first host, up to 30% on a
second — and decision 39 (and the phase before it) treated this as inseparable
from the host's `-vnc` finding and its general bimodality. A direct comparison
against VirtualBox, which completes Windows installs on the same second host
through the same underlying platform (`WHvPlatform`/NEM, the API `-accel whpx`
also binds to), showed VirtualBox's stock Windows template differs from this
project's engine on exactly four points: chipset (`piix3` vs `q35`), HPET
(off vs QEMU's default on), RTC (local time vs QEMU's default UTC), and Hyper-V
paravirtualization enlightenments (present vs absent). See docs/WINDOWS.md for
the full comparison and measurements; this entry records the outcome only.

**Fix, isolated one variable at a time with `tools/ab_measure.py` (Decision
40's protocol) before ever touching a real install:**

- Chipset alone (`q35` → an i440fx-family machine, e.g. QEMU's `pc` alias):
  no measurable boot-phase effect — the boot phase was already 60-100%
  reliable before any change, so this variable could not be discriminated
  there. Carried forward as a base for the next test anyway, since it is at
  worst neutral and matches VirtualBox.
- `hpet=off` + `-rtc base=localtime`, layered on the i440fx base: **13/13
  (100%) across two alternating sessions**, with perfectly reproducible timing
  (36.3-36.4s every run) — the cleanest result measured anywhere in this
  project's Windows-guest work.
- The same three-part change (i440fx + `hpet=off` + `rtc=localtime`), with the
  real product's exact Windows device profile otherwise unchanged (`-cpu
  Westmere`, AHCI, `e1000e`, `std` VGA, `qemu-xhci`/`usb-kbd`/`usb-tablet`,
  `-vnc` attached) and a real Windows 10 install: **the entire "Getting files
  ready for installation" phase completed reliably** — 0% to "Windows needs to
  restart to continue" in roughly nine minutes, reproduced across multiple full
  runs, including with Hyper-V enlightenments added on top (no additional
  effect either way — ruled out as relevant to *this* stall, though tested
  further as a factor in the reboot investigation that followed).

**CPU model is unchanged and stays `Westmere`.** `-cpu host`/`max` still crash
WHPX outright (decision-adjacent finding, unrelated to this fix); VirtualBox's
`host` CPU profile under its own NEM binding does not inform this, since NEM's
CPUID virtualization is a different code path from QEMU's `-cpu host` under
WHPX.

**Status.** This fix is confirmed and ready to carry into
`QemuEngine.build_launch_command` for Windows guests specifically (Linux
guests are untouched — nothing here has been tested against or is claimed to
apply to them). It has not been merged into the engine yet: a second,
independent defect — QEMU/WHPX's `system_reset` hanging the guest on its first
in-process reboot, unrelated to any of the above and root-caused separately —
still blocks a Windows install from finishing end to end, and the two are kept
distinct so the confirmed fix here does not get lost inside that ongoing
investigation. See docs/WINDOWS.md for the reboot defect's status.

## 55. A heuristic workaround for the guest-reboot hang, confirmed upstream and unfixed at any tested version

**Context.** Decision 54 fixed the copy-phase stall but left a second,
independent defect: QEMU/WHPX's `system_reset` deterministically fails to
bring a Windows guest back up after its first in-process reboot, while a fresh
QEMU process against the identical disk state always works. See docs/WINDOWS.md
for the full measurement history — 36/36 across every chipset, RTC/HPET,
Hyper-V-enlightenment, and CD-ejection combination tried, reproduced
identically on the officially tagged QEMU 11.1.1 release and not just this
project's dev snapshot.

**Confirmed upstream before anything was built.** A related, unresolved bug
class exists in QEMU's own tracker (GitLab #2042, #2402) but neither is a
match — both report a crash (`WHPX: Unexpected VP exit code 4`) this project's
hang never produces, and both report workarounds (`-smp 1`,
`kernel-irqchip=off`) that do not help here. Filed as its own report:
**https://gitlab.com/qemu-project/qemu/-/issues/4410.** No released QEMU version fixes it, so there was
nothing to pin to instead of a workaround.

**What was built.** `kurukuru/reboot_watchdog.py` — a heuristic, explicitly
documented as one, not a general health check:

- Scoped to `guest_os == "windows"` only. A static, low-colour framebuffer is
  a *normal* steady state for a headless Linux console; treating it as a
  symptom there would be a serious bug, not a recovery.
- Samples a running Windows guest's QMP `screendump` on an interval
  (`windows_reboot_watchdog_interval_seconds`) rather than holding a second,
  persistent QMP connection open to watch for a `RESET` event — QEMU's QMP
  chardev serves one client at a time, and `QemuEngine._liveness`'s own
  docstring already records the incident a second permanent connection caused
  once (a 40-minute Running/Stopped flap). Watching the symptom instead of the
  event avoids reintroducing that class of bug.
- A frame at or below `windows_reboot_watchdog_colour_threshold` (8; measured —
  SeaBIOS's text-mode prompt renders as ~2 colours, every graphical stage
  observed as 12+) held continuously for
  `windows_reboot_watchdog_stuck_seconds` (300s default) is treated as stuck.
  300s was chosen with margin, not tightness: every legitimate boot-to-
  graphical transition measured anywhere in this project's Windows-guest work
  landed under 40 seconds or never happened at all, so the default carries
  roughly 8x headroom — a false positive here restarts a healthy VM and
  destroys unsaved guest state, a materially worse outcome than a human
  noticing a hang and restarting it themselves.
- Recovery reuses `restart_instance()` unchanged — the same stop-then-start
  path the manual `/restart` endpoint already uses, with its existing
  90-second ACPI grace period and forced-kill backstop.
- At most one automatic restart per `windows_reboot_watchdog_cooldown_seconds`
  (1 hour default). Stuck again inside that window marks the instance `Error`
  with an explanation instead of retrying — a bounded workaround, not a
  supervisor that loops forever.
- Its own event kind, `EventKind.AUTO_RESTARTED`, distinct from a
  user-requested `RESTARTED` and from a routine `RECONCILED` correction. A
  user has to be able to tell "the backend did this to your VM without being
  asked" apart from both "you asked for this" and "a field was silently
  corrected" — reusing either existing kind would have hidden that distinction.
- The console auto-reconnects (`ConsoleModal.tsx`) up to three attempts when a
  watched session drops and the backend still reports the instance Running,
  rather than leaving whoever was watching stranded on a dead viewer with no
  way back — the scenario the watchdog exists for is exactly the one where
  someone is likely to be watching.

**Removability is a design requirement, not an afterthought.** Every file this
workaround touches — `reboot_watchdog.py` itself, the four
`windows_reboot_watchdog_*` settings, `EventKind.AUTO_RESTARTED`,
`windows_reboot_watchdog_pass` in `routers/instances.py`, its scheduling in
`main.py`, and the frontend's auto-reconnect behaviour — is named in
`reboot_watchdog.py`'s own module docstring as a checklist, so that if the
upstream issue is fixed and this project's minimum QEMU version moves past it,
removing the workaround is one commit against a checklist rather than an
archaeology exercise.

**Status.** Built, tested (pure state-machine and colour-classification unit
tests, plus DB/engine integration tests for scoping, the restart action, the
cooldown guard, and the give-up path), and documented in code and in
docs/WINDOWS.md. Not yet validated against a real, complete Windows install —
that requires the copy-phase fix (decision 54) to also be carried into
`QemuEngine.build_launch_command`, which has not happened yet either.

## 56. Decision 54 carried into the engine; i440fx confirmed fine for volumes and the NIC

**Context.** Decision 54 fixed the copy-phase stall but was never wired into
`QemuEngine.build_launch_command` — it stayed a manually-reproduced command
line. Wiring it also opened a question nothing had tested: every other
Windows measurement in this project (disk, NIC, volumes, USB HID) was run on
`q35`, and decision 54 changes the chipset itself, to an i440fx-family
machine. Nothing established that i440fx carries the rest of the scorecard.

**Wired in, gated on `guest_os == "windows"`.** `GuestProfile` gained two
fields, `machine` and `rtc`, following the same per-guest-family data pattern
`disk_bus`/`nic_model`/`usb_input` already use rather than an `if guest_os ==`
scattered into the command builder. Linux's `machine="q35"`/`rtc=None` is
what the command builder already emitted, so a Linux launch command is
byte-for-byte unchanged — pinned by
`test_a_linux_guest_is_unchanged_by_the_windows_work`, the same test decision
54's predecessor work extended for exactly this regression. Windows gets
`machine="pc,hpet=off"` and `rtc="base=localtime"`, pinned by
`test_windows_gets_the_copy_phase_fix`. Full suite re-run clean after the
change: 943 backend tests, tools tests, and the frontend's `npm run verify`.

**i440fx validated for volumes and the NIC, using the real engine code and
Alpine as the fast measurement vehicle** — the same substitution decision
"Eliminated, with evidence" used originally ("Alpine on identical hardware
boots, sees disk and NIC"). `QemuEngine.build_launch_command` was called
directly (not reconstructed) for a `guest_os="windows"` runtime — AHCI root
disk, one attached volume, e1000e NIC, i440fx + hpet=off + rtc=localtime —
booting Alpine's ISO instead of Windows media, driven over QMP `send-key`
(as blind, deterministic keystrokes — the same technique Setup was driven
with) with screendumps read back as real text, not colour-fingerprinted:

- **Volumes:** `/proc/partitions` showed `sda` (root, 4 GB) and `sdb` (the
  attached volume, 2 GB) on the first boot. A marker written to `/dev/sdb`
  survived a full stop (QMP `quit`) and fresh-process restart — the same
  fresh-process mechanism `restart_instance()` uses, not an in-place
  `system_reset` — and read back correctly from `/dev/sdb` again on a third
  boot after a second restart. Naming stayed stable across both restarts.
- **NIC:** `eth0` (e1000e) came up and `udhcpc` obtained a lease
  (`10.0.2.15` from QEMU's SLIRP gateway `10.0.2.2`) on the first boot,
  identical to how the NIC has always been expected to behave — chipset made
  no difference.

Neither result is surprising in hindsight — AHCI and e1000e are both PCI
devices, and a chipset change does not usually alter PCI enumeration or
guest-visible block/network naming — but "usually" is not "measured", and
nothing had measured it against this specific chipset before. Both are now
confirmed rather than assumed.

**Status.** DECISIONS #54 is fully carried into the product. Nothing found
here changes the plan; it closes the one open question standing between the
copy-phase fix and a real, full Windows install attempt with both fixes
(this one and the reboot watchdog) in place.

## 57. First completed Windows install, the scorecard validated against it, and a real bug the attempt surfaced

**Context.** Decision 56 closed the last open question standing between the
copy-phase fix and a real, full Windows install attempt with both fixes in
place. Nobody had run one yet.

**The install.** A real Windows 10 install, through
`QemuEngine.provision_instance` (not a manual command line) with
`guest_os="windows"`: the copy phase completed 0% → 41% → 86% → done in the
same ~9 minutes decision 54 originally measured, with no stalling. Setup's own
internal reboot then hit the `system_reset` hang exactly as decision 55
predicted — a static SeaBIOS-prompt framebuffer, indefinitely. Calling
`restart_instance()` (the same fresh-process path `reboot_watchdog.py` uses in
production) recovered it, landing on Windows' first-boot "Getting ready"
screen. **That finalisation sequence then hit the identical hang a second
time** — Windows' own OOBE reboot, completely independent of Setup — and a
second `restart_instance()` call recovered it the same way, reaching OOBE
proper (region, keyboard, network, local account — the network step forced
offline via QMP `set_link ... up=false` to reach the local-account path
without a Microsoft account) and finally a normal, logged-in Windows 10
desktop. **This is the first Windows install this project has completed
end to end.** It also confirms something decision 55 could not have shown on
its own: the `system_reset` defect fires on the reboot mechanism itself, not
on anything specific to what Setup does before triggering one — the watchdog
has to stay scoped to *every* Windows reboot, not just the installer's.

**The validation scorecard, run against the completed install, all five
items:**

- **Network.** `ipconfig`/`ping` from inside the guest: DHCP lease from
  QEMU's SLIRP gateway, 4/4 ping, 0% loss. The NIC identified as Intel 82574L
  (e1000e) with its inbox driver bound, no install step.
- **Volumes.** Attached via `set_volumes()`, appeared as a fresh disk in
  Windows, partitioned/formatted/lettered (`E:`, NTFS) through `diskpart`,
  survived a `restart_instance()` with the same letter and an intact marker
  file.
- **Restart.** `restart_instance()` round-tripped in ~27s with a graceful ACPI
  stop when the guest was idle at a desktop — the first time this project has
  measured the *graceful* path's timing rather than only the
  ACPI-ignored-then-killed one.
- **Clone.** `clone_disk()` + `boot_cloned_instance()` produced an
  independently-running Windows guest on its own ports, booting cleanly to its
  own lock screen with no shared state against the source.
- **ACPI stop timing.** Both branches observed: a fast graceful stop (~27s)
  and, separately, a full 90s-timeout-then-forced-kill — both are the designed
  behaviour, not a bug.

**A real bug the attempt surfaced, now fixed:** `restart_instance()` failed
twice with "port in use" immediately after `stop_instance`'s forced-kill
backstop, because Windows had not released a pinned port (SSH once, VNC once)
yet even though the process was already confirmed dead — the exact moment
`reboot_watchdog.py` exists to recover automatically, mechanically failing at
it. Forced directly rather than waited for, per CONTRIBUTING's rule on
proving a guard fires: bind a port in a child process, kill it the way
`_force_off` does, and hammer-rebind with no delay at all. Every trial saw
several rebind attempts fail before one finally succeeded — confirmed, not
theorised. Fixed two ways:

1. `wait_for_port_free()` (`kurukuru/engines/ports.py`) replaces the single
   `is_port_free()` check in `start_instance` with a bounded poll
   (`Settings.qemu_port_release_timeout_seconds`, 5s default) — a real
   conflict still fails after the timeout; only the transient just-freed
   window is absorbed.
2. `_restart_with_retry()` (`routers/instances.py`) gives the watchdog's call
   to `restart_instance` one bounded retry before marking the instance
   `Error`, on a different axis from the existing stuck-again/cooldown budget
   (`InstanceWatch`'s `Action.GIVE_UP`): a mechanical failure of the restart
   *call itself*, retried and recovered, does not count against that budget
   at all.

Both are tested by forcing the real condition, not by mocking the retry's own
arithmetic: `test_wait_for_port_free_recovers_from_a_just_killed_processs_race`
spawns a real child process, kills it, and asserts the rebind succeeds with no
delay inserted by the test; `test_a_transient_restart_failure_is_retried_not_given_up_on`
and its sibling `..._is_still_given_up_on` exercise
`_restart_with_retry`'s two branches (recovers within the budget; still
fails, and is still reported, if it never clears) via a fault-injecting fake
engine. 4 new tests, full suite re-run clean: 947 backend tests, tools tests.

**Status.** Both defects this document tracks are proven fixed together, on a
real install, with the scorecard fully validated against it and one bug found
along the way already closed. What remains — fresh Server 2025 media, and the
smaller unexplained boot-phase failure rate — are the two items still open
below.

## 58. `qemu_snapshot_timeout_seconds` was too tight for a real Windows disk, and could report a completed clone as failed

**Context.** Decision 57's clone step converted a real Windows install's disk
(~14 GB actually used) and hit the then-default `qemu_snapshot_timeout_seconds`
(900s / 15 min) right at the finish line — the `qemu-img convert` had been
running roughly 17 minutes. Checked before assuming the clone was lost:
`qemu-img check` on the resulting file passed clean. The clone had actually
succeeded; only the timeout's own bookkeeping called it a failure.

**Root cause.** `subprocess.run(..., timeout=...)` kills the child process the
moment the timeout elapses, whatever the child was doing. A `qemu-img convert`
can finish writing every byte of a large file and be moments from returning
exit code 0 when the timeout fires — the file is complete, but the process
never gets to say so before it is killed. `_run` then raises
`ComputeTimeoutError`, and `clone_disk` had no way to tell that apart from a
conversion that is genuinely stuck or corrupt.

**Fixed two ways:**

1. **The default raised, with the arithmetic shown rather than a bare
   number.** At the measured ~1.2 min/GB, this project's own `windows` flavor
   preset (40 GB) could plausibly use the whole disk over time — call that
   worst case ~48 minutes — and `qemu_snapshot_timeout_seconds` now defaults
   to 5400s (90 min), real headroom above that ceiling rather than just above
   the one measurement.
2. **`clone_disk` verifies before trusting a timeout's verdict.** On
   `ComputeTimeoutError` from the `convert` step, a new `_image_is_intact()`
   helper runs `qemu-img check` (a fixed 120s budget — checking an image's own
   metadata is not a bulk copy and does not scale with the disk the way
   conversion does) against the partially-timed-out target file. Intact: log a
   warning and treat the clone as successful. Not intact, or the check itself
   fails: re-raise, now naming the setting in the message
   (`qemu_snapshot_timeout_seconds`) so whoever hits this on a larger disk
   knows what to change rather than assuming the clone mechanism is broken.

**Tested by forcing both branches**, not by trusting the arithmetic:
`test_clone_disk_treats_a_timed_out_but_intact_conversion_as_success` and
`test_clone_disk_still_raises_when_the_timed_out_image_is_broken` patch
`subprocess.run` to raise `TimeoutExpired` on the `convert` call and control
what the follow-up `check` call reports, proving `clone_disk` reads the
verification result rather than the timeout alone. 2 new tests, full suite
re-run clean: 949 backend tests.

**Status.** Fixed and tested. Only `clone_disk`'s `convert` step is wrapped —
`create_blank_disk` and a clone's `resize` step are metadata-scale operations
regardless of disk size and were never observed near this timeout.

## 59. The `~/.kurukuru/keys`/`cloud-init` test-isolation leak, root-caused and fixed

**Context.** Decision 58's own note recorded this as a real, reproducible,
unsolved leak — surfaced only because `~/.kurukuru` had just been deleted
from this machine as part of unrelated disk-space work, previously masked by
a real, already-existing directory the isolation guard
(`conftest.no_real_state_writes`) saw no diff against. Asked to fix it
properly rather than leave it recorded.

**Root-caused by forcing the actual call to appear, not by reasoning about
the fixture code.** Reasoning about `_app_modules_with`'s discovery loop and
import ordering kept concluding the patches *should* work; they did not.
What actually found it: patching `subprocess.Popen.__init__`/`subprocess.run`
at the lowest level, writing every matching call's full stack trace to a
file (bypassing pytest's own output capture, which swallowed print-based
attempts), and reading the trace. Two distinct, independently-introduced
bugs, both in `kurukuru/main.py`:

1. **`lifespan()` used the module-level `settings = get_settings()`
   (bound once at import time, before any test's patch exists) instead of a
   fresh call.** `conftest.isolated_state` patches the `get_settings`
   *function* on every already-imported module — including `main.py`, which
   gets imported at pytest *collection* time (before any fixture runs) by
   `test_auth_coverage.py`'s own module-level `from kurukuru.main import
   app`. Patching the function does nothing for a *value* `main.py` already
   computed by calling it once, before the patch existed. `lifespan()`
   passed that stale, real settings object straight into
   `ensure_orchestrator_keypair(settings)`, which reached
   `ssh_keys.ensure_keypair()` and ran real `ssh-keygen` against the real
   `~/.kurukuru/keys` the first time any test entered `TestClient` as a
   context manager (triggering a real ASGI lifespan startup) — which is
   also why the blamed test varied by run: whichever test was first to do
   that, in whatever order/subset was executing, generated the (only ever
   generated once per real, un-isolated state directory) real keypair, and
   every later reuse of it produced no *new* diff to catch.
2. **`GET /ssh-key` took no settings dependency at all**, reading the same
   stale module-level `settings` directly rather than the
   `request_settings: Settings = Depends(get_settings)` pattern every other
   route in this file already uses correctly.

**Fixed by making both read `get_settings()` at call time** — `settings =
get_settings()` as the first line of `lifespan()`'s body (shadowing the
module-level name for that function only), and `Depends(get_settings)` added
to `ssh_key()`, matching the established pattern. `get_settings()` is
`lru_cache`d, so in production this returns the identical object either way
— zero behaviour change there; the fix only matters where something
replaces what `get_settings()` returns after import, which today is only
ever a test.

**A third, unrelated bug of the same *shape*, in the tests themselves, once
the first two stopped covering for it.** `test_cli.py`'s `make_cli` and
`test_images_api.py`'s `settings` fixtures each built a `Settings(...)` by
naming a hand-picked subset of directory fields (`qemu_dir`, `ssh_key_dir`,
`iso_dir`) without ever setting `state_dir` itself — so every field *not*
named (`cloud_init_dir` foremost) silently fell back to
`DEFAULT_STATE_DIR`, the real `~/.kurukuru`, per `_apply_state_dir`'s
"only re-root a field still at its default" rule. Harmless as long as
nothing the test exercised touched one of the un-named fields; `lifespan()`
calling `ensure_builtin_image(settings)` (cloud-init) is exactly such a
touch. This is the identical mistake `conftest.py`'s own docstring already
names as the reason `isolated_state` exists — an opt-in, hand-maintained
list of what to redirect, the same shape as Phase 10 and Phase 11's
incidents — just recurring inside individual test fixtures rather than in
the shared harness this time. Fixed by adding `state_dir=str(tmp_path /
"state")` alongside the existing explicit overrides in both fixtures (which
`_apply_state_dir`'s explicit-wins rule leaves untouched), and in
`test_ssh_keys.py`'s `settings` fixture, which had the same latent shape but
hadn't yet been caught touching an affected field.

**`test_isolation.py`'s own two "prove the guard fires" tests were
inadvertently relying on the very leak being fixed.** Both wrote a stray
file directly into `REAL_STATE_DIR / "keys"` without creating the directory
first, silently depending on it already existing from ordinary use (or, on
this machine, from the bugs above) — once those stopped leaving it behind,
these tests failed with a bare `FileNotFoundError` instead of exercising
what they were written to prove. Fixed by having each outer test
`mkdir(parents=True, exist_ok=True)` the directory itself (recording
whether it pre-existed) before running its generated subprocess test, and
`rmdir()`-ing it afterward if this test was the one that created it. Marked
`@pytest.mark.real_state` — the same exemption the module docstring says
any test that "genuinely needs the real paths" should carry, with the
explanation living in this commit as that rule asks.

**Verified clean, not just plausible.** Three consecutive full-suite runs
from a freshly-deleted `~/.kurukuru`: 949 passed, 4 skipped, 0 errors every
time — the same suite that, before this fix, reliably left real key or
cloud-init files behind on at least one of `test_auth_coverage.py`,
`test_cli.py`, or `test_images_api.py` depending on run order.

**Status.** Fixed. `conftest.isolated_state`'s discovery-based redirect
mechanism itself was never wrong — every instance of this bug was either
code that bypassed it entirely (a value cached before the patch existed,
same as decision 58's dismissed-then-vindicated `database.py` suspicion) or
a test fixture that reinvented a narrower, hand-maintained version of what
it already does comprehensively. No change to `conftest.py` itself was
needed or made.

## Known limitations

- **The guest username is still `iaas`.** See decision 49; it needs a
  per-instance column before it can move.
- **No authorization.** Authentication exists (decisions 45-48); roles and
  project isolation do not. Every account is a full administrator.
- **No transport encryption.** Plain HTTP, so anything beyond loopback needs
  a TLS-terminating proxy in front of it. See docs/SECURITY.md.
- **SQLite, single writer.** Fine now; a multi-process deployment would need
  more.
- **Concurrent-launch name race.** See decision 6 — closable with a partial
  unique index if it ever matters.
