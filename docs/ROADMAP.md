# Roadmap

What is next, and what has been considered and set aside. Forward-looking —
`docs/history/ROADMAP_v2.md` is the archived plan from the phase work and is
kept for the record rather than followed.

Nothing here is a commitment to a date. The ordering is by what unblocks what.

---

## 0.2.0

### 1. Upgrade FastAPI, and Starlette with it

**The first task, before features.** FastAPI 0.115.12 requires
`starlette<0.47.0`, and every open Starlette advisory is fixed at 0.47.2 or
later — several only in 1.x. So the fixes are all on the far side of a major
upgrade, and 0.1.0 shipped pinned with that written down rather than attempted
in a release week.

What it closes: a quadratic `Range`-header parse in `FileResponse`
(PYSEC-2026-1942), reachable unauthenticated through the dashboard's asset
route. The worst case is CPU burn on the machine already running the server,
triggered by a page the user visited — see the browser-as-client threat model
in [SECURITY.md](SECURITY.md).

What it will cost: 961 backend tests to re-validate against a major version of
the framework the whole API is built on. That is the work, and it is why it is
first rather than squeezed in beside something else.

Two other Starlette advisories were assessed and do not apply: the
`HTTPEndpoint` method-lookup issue (FastAPI does not use `HTTPEndpoint`) and the
`StaticFiles` UNC SSRF on Windows (the dashboard is served by this project's own
handler). The second is worth reading anyway — the same mistake *was* present
here, in `dashboard` and `isos`, and is fixed in `kurukuru.safe_paths`.

### 2. Roles, and audit by actor

Not for their own sake: they are the two things standing between this and being
runnable anywhere other than loopback. Today every account is a full
administrator of every VM, and the event log records that a thing happened
rather than who did it. Until both exist there is no honest way to expose this
beyond a reverse proxy on a trusted network, which is what SECURITY.md says.

---

### 3. Verify the base image against Ubuntu's published checksum

`fetch_into_store` already takes an `expected_sha256` and refuses the download
when it does not match. Nothing passes one for the default base image, so the
600 MB qcow2 every first launch pulls from

    https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

is trusted on TLS alone. TLS authenticates the host and protects the bytes in
flight; it says nothing about whether the file on that host is the one that was
tested, and `current/` is a rolling path whose contents change as Canonical
republishes.

Canonical publishes `SHA256SUMS` and `SHA256SUMS.gpg` beside the image, so the
fix is small in code and mostly a decision about *which* checksum to trust:

- Pinning a literal in `config.py` means the value is reviewed with the commit,
  and breaks the moment `current/` is republished. Correct, and it turns a
  routine upstream refresh into a broken first launch for everyone until a
  release ships. That failure mode is worse than the risk it closes, so a
  pinned literal alone is not the answer.
- Fetching `SHA256SUMS` at download time and matching the entry follows the
  rolling path without going stale, but the checksum then comes from the same
  host and the same TLS session as the image, so it detects corruption and a
  bad mirror rather than a compromised origin.
- Verifying `SHA256SUMS.gpg` against Canonical's signing key is the version
  that actually closes the origin case, and costs a bundled public key plus a
  signature check.

The third is the right one; the second is worth having on the way if the third
slips. Not today's work because the honest severity is low for the threat model
this tool has — a single-user desktop pulling from a Canonical host over TLS —
and because getting it wrong breaks every new install rather than degrading
quietly. It is written down here because the parameter already exists and is
unused, which is exactly the kind of gap that stays open for years by default.

### 4. An install path for a machine with no Python

`pip install kurukuru` covers "script-based" only for someone who already has
Python. The people this tool is for often do not, and telling them to install a
language runtime before they can install a VM manager is a strange first step.

The shape other CLI tools use is a PowerShell one-liner that fetches a small
script, which downloads the release artefact and unpacks it into the user's
profile — no elevation, no runtime prerequisite. The pieces already exist: the
release publishes a self-contained build and a SHA-256 beside it, and the
installer needs no administrator rights.

Two things to get right rather than fast. The script must verify the checksum
it just downloaded, or it is a worse supply chain than the installer it
replaces. And `irm ... | iex` asks people to pipe a URL into a shell, so the
script wants to be short enough to read and served from somewhere the project
controls.

### 5. Say what the resource footprint actually is

**Low priority, raised by a real user.** The first external installer reported
the memory use as alarming. The number shown was a 106 MB working set, which is
ordinary for a background service — but they had also been given a 2.2 TB
*virtual memory* figure by an AI assistant, which is address-space reservation
and normal for any modern process, and had no way to tell which number meant
what.

Nothing is wrong with the product here. The gap is that Settings and `doctor`
report raw process statistics and leave the interpreting to the reader, and raw
process statistics are exactly the thing a non-specialist cannot interpret. A
plain sentence — "typically under 150 MB while idle" — turns a number that
needs expertise into one that does not.

Worth measuring before writing, so the figure is a fact rather than a guess.
## Considered, not scheduled

Suggestions from an external backend review. Logged with what they would be
worth so the reasoning is not re-derived, and so a "no" does not read as
"nobody thought about it".

### Cloud-init dry-run and validation

**The idea.** `POST /cloud-init/validate` — accept `user-data`, merge it with
the system defaults, and validate the result before anything is provisioned.

**Why it is worth doing.** The debugging loop for `user-data` is genuinely
awful and it is the sharpest edge left in the launch flow: you launch, wait for
a boot, SSH in, and read `/var/log/cloud-init-output.log` to discover a typo in
`write_files`. Minutes per iteration for a mistake a schema check catches
instantly. Of the three suggestions this is the one with the clearest value,
because it removes a wait rather than adding a capability.

**What makes it non-trivial.** Validating properly means the `cloud-init`
schema, and `cloud-init` is a Linux package — it is not present on a Windows
host and cannot be assumed on any host. So this is either a vendored copy of
the schema that will drift from whatever the guest actually runs, or a
best-effort YAML-and-shape check that risks passing something the guest then
rejects. A validator that says "fine" and is wrong is worse than no validator,
because it moves the same failure later while adding confidence. Worth doing
once there is an answer to which schema is being validated against.

### An event stream for CI

**The idea.** SSE or a WebSocket at `/events/stream`, broadcasting what
`record_event()` already writes, so a script can block on "instance is Running"
instead of polling every few seconds.

**Why it is worth doing.** Provisioning from a script is a real use of this, and
`while true; sleep 5` is the current answer. A stream would make integration
tests react the moment a VM is ready rather than up to a poll interval later.

**What it needs first.** The events table is the natural source, and the
plumbing largely exists — `record_event()` is already the single writer. The
open question is what happens to a subscriber that stops reading: a local tool
with one user is exactly where an unbounded buffer goes unnoticed until it is a
memory leak. Not hard, but not free either, and polling loopback every three
seconds costs approximately nothing today, which is why this is behind
cloud-init validation.

### Linked clones

**The idea.** `qemu-img create -b base.qcow2 overlay.qcow2` so a clone is an
overlay rather than a copy — instant, and nearly free on disk.

**The appeal is real.** Ten VMs from a template in the time it takes to write
ten qcow2 headers, and a five-node environment that can be torn down and rebuilt
in seconds rather than minutes. For anyone testing a playbook against a cluster
that is a qualitative difference, not a saving.

**And it is still refused, for the reason it was refused in Phase 11.** A
backing chain means the clone does not own its own disk. Delete or terminate the
source and every clone built on it is silently corrupted — not refused, not
warned about: corrupted, discovered later, at a moment of the filesystem's
choosing. The qcow2 header records the backing file as *the absolute path it was
created with*, which is why the state-directory rename needed a repair pass at
all (DECISIONS #54's neighbourhood), and it is why a chain is a durable
commitment rather than an implementation detail.

`test_the_clone_disk_is_flattened_not_chained` exists to keep that decision from
being quietly reversed by someone who notices the copy is slow. The copy *is*
slow. That is the price of a clone that survives its parent.

**What would change the answer.** Not performance — a safety story. Reference
counting that refuses to terminate a source with live descendants, or a
promote-on-delete pass that flattens dependants before the parent goes, plus a
UI that shows the relationship so "delete" is never a surprise. That is a
feature with a data-loss failure mode, so it needs designing rather than
enabling, and it should arrive with roles and audit rather than before them.

---

## Not planned

See the README's non-goals. In short: not a VirtualBox replacement, not a
container runtime, not multi-tenant. The target is one machine with
cloud-shaped ergonomics, and most requests that do not fit are requests for a
different product.
