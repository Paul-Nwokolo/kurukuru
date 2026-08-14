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

## 24. Every path the backend owns is resolved against the state directory, including the database

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

- **No license chosen.** Until one exists, the code is not usable by anyone else.
- **No authentication.** Anything beyond a single trusted machine needs it first.
- **SQLite, single writer.** Fine now; a multi-process deployment would need
  more.
- **Concurrent-launch name race.** See decision 6 — closable with a partial
  unique index if it ever matters.
