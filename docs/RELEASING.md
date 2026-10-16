# Releasing

The checklist for cutting a release, and the standard behind it.

**Every item below says what it proves and what it caught.** None of them is
ceremony: each exists because a release shipped without it, or nearly did, and
nothing failed at the time — which is how each defect survived. An item whose
reason you cannot state is the first one you skip, so the reasons are written
here, not left in someone's memory.

**The standard, in one line: the developer's machine is not evidence.** It has
QEMU on PATH, the previous version installed, a warm cache and the right
icon in the build folder. What counts is what a stranger gets: the published
bytes, downloaded with no credentials, installed on a machine that has never
seen the project. An automated step is still listed with what to read in its
output, because "the workflow went green" has been wrong before.

**Nothing published is ever removed.** Every GitHub release stays: releases
are the record. A superseded one says so in its notes and points at its
replacement, as 0.1.3's does; its assets are left alone. The same on PyPI:
**yank, never delete.** A yank can be undone and keeps the history; a deletion
frees nothing, because a version number is spent forever either way.

## Before the tag

1. **Version** in `backend/kurukuru/product.py` (`VERSION`). The wheel and
   the installer both read it, so they cannot disagree. Two copies are edited by
   hand with it: the placeholder in `.github/ISSUE_TEMPLATE/bug_report.yml` and
   `KURUKURU_APP_VERSION` in `backend/.env.example`.
2. **CHANGELOG**: move `[Unreleased]` under the new version, and add its link
   reference at the bottom. The date is the planned tag date, and it goes in
   *before* the rehearsal (item 5): the rehearsed commit must be the tagged
   one, so nothing changes between them, not even the date.
3. **Draft the GitHub release** for the tag. The workflow uploads to an existing
   release and never creates or publishes one: publishing stays a human act.

### 4. `verify` green on the commit being tagged, both pipx jobs included

**Proves:** the suites pass on Windows (real QEMU) and Linux, and the wheel
installs with pipx **on Linux and on Windows**, serves its dashboard, and gets
`doctor` advice that fits a wheel.

**Caught:** PyPI serves the wheel to Windows users whether or not that is
intended, and Windows is where most users are. Before 0.1.5, `doctor` told a
Windows pipx user that QEMU "ships with Kurukuru" and that reinstalling fixes
it. A wheel bundles no QEMU, so both were wrong, and reinstalling would have
sent them round in a circle. The first run of the Windows pipx job also found
`doctor` telling a Windows host to check `/dev/kvm`.

### 5. The TestPyPI rehearsal passed on the commit being tagged

**A release requirement, not a nicety.** Run the release workflow by hand on
`main` at the commit you will tag: *Actions → release → Run workflow*, with
**Rehearse the PyPI publish on TestPyPI** ticked. It runs the real chain:
`installer`, `checksum`, then `pypi` and `pypi-verify` against TestPyPI, with
your approval on the `testpypi` environment. It must end green. Put the run's
link in the release notes.

**Why:** the first real upload cannot be repeated and claims the name. Two jobs
that have never run end to end, guarding a step that cannot be undone, is the
shape of problem this project keeps rediscovering. See
[What the rehearsal proves](#what-the-rehearsal-proves-and-what-it-cannot).

## The release workflow (`v*` tag push)

### 6. Installed and run on a clean Windows runner — `installer`

**Proves:** the installer, installed, works on a machine with nothing else on
it. Not the staged tree: the actual setup `.exe`, then `kurukuru` driven
through its checkpoints, each reported as an annotation.

**Caught:** 0.1.0's bundled QEMU was unreachable. Nothing pointed at
`{app}\qemu`, and the bare name `qemu-system-x86_64` resolved through PATH,
which on the development machine found an already-installed QEMU. Every test
passed. **Anyone installing 0.1.0 without QEMU already present got a
non-working install**, found by the first real external install (CHANGELOG
0.1.1). 0.1.2's other packaging defects — `auth init` dying in the frozen
build, an uninstall leaving a PATH entry behind — were invisible the same way
until something ran what ships.

### 7. The icon check — `installer`, `build_installer.verify_icon`

**Proves:** `kurukuru.exe` *and* the setup `.exe` carry the product icon:
every RT_ICON frame of an icon group byte-equal to `kurukuru.ico`'s frames.

**Caught:** **every release before 0.1.4 shipped PyInstaller's default icon**
on `kurukuru.exe`, the binary that actually runs, because nothing passed
`--icon`. The installer, the shortcuts and Add/Remove Programs showed the right
one, so nobody looked. On a product Windows already declines to trust,
the stock packager icon is the visual signature of an untrustworthy build
(DECISIONS #67). Compared frame by frame, not by eye or by rendering: which
frame Windows picks and how it premultiplies alpha differ even for the same
icon.

### 8. Upgrade over the previous release — `installer`, `tools/verify_upgrade.py`

**Proves:** after installing this build *over the previous published release*
— the path a real user takes — the installed tree is this build byte for byte,
nothing extra, and the installed `kurukuru.exe` reports the pinned FastAPI and
Starlette as the modules it actually imported. The expected file set is read
from `kurukuru.iss` itself, so the check cannot drift from what the installer
installs.

**Negative control:** the same check run against the previous release first
must **fail**. A check that passes the old version passes anything.

**Caught:** Inno copies over the top and removes nothing, so **an upgrade
layered the new files over the old ones and the loaded version was
undefined** (DECISIONS #66). 0.1.2 → 0.1.4 left 51 files 0.1.4 never wrote,
including FastAPI 0.115.12's metadata beside 0.120.4's, on a security release
whose whole content was "a different Starlette now runs". Every upgrade from
0.1.1 to 0.1.3 shipped this way.

### 9. The checksum file, checked on Linux before it is attached — `checksum`

**Proves:** `sha256sum -c` on the published `.sha256` passes, and the file
contains **no carriage return**. Only then are the installer and its checksum
attached, so a checksum that does not verify is never published.

**Caught:** 0.1.3's and 0.1.4's checksum files ended in CRLF. GNU coreutils
8.32's `sha256sum -c` (Git for Windows' build) then looks for a file whose name
ends in `\r` and reports FAILED. The hashes were right; the one-step
verification a careful user runs was broken, and to that user a failing
checksum reads as tampering (DECISIONS #68).

**`sha256sum -c` was never the guard.** On the deliberate-break run, the
runner's coreutils 9.4 accepted the CRLF file and printed `OK`; only the
explicit carriage-return check failed it. `sha256sum -c` stays because it is
what users run. The carriage-return check is what stops the regression. The
job prints its `sha256sum` version for that reason.

### 10. PyPI — `pypi-gate`, `pypi`, then `pypi-verify`

See [PyPI](#pypi). If publishing is off for a tag, **`pypi-gate` fails the
run and says why**, rather than the PyPI jobs showing a quiet grey "skipped".

**Caught (before it shipped):** a skip reads as a finished release. Measured
on 2026-10-14: `PYPI_PUBLISHING` set on the `pypi` *environment*, where it was
first created, is invisible to a job-level `if`, so the publish would have
skipped silently with everything else green (DECISIONS #74). It must be a
repository variable. The gate's message names that trap.

## After the workflow: check, publish, check again

### 11. Download-and-hash: the bytes now, the stranger's view after publishing

Into an **empty** directory, download the installer and its `.sha256`, then
check **both**:

- `sha256sum -c` prints `OK`, which proves the file and its checksum agree; and
- the hash equals the one the `installer` job printed under "Record what was
  built", which proves these are the bytes CI built and tested, not a file
  from your machine or a stale asset.

**This check runs twice, and the anonymous half cannot run before publishing.**
A draft release is invisible to anyone logged out: the release page, the
assets and the API all answer 404 to a request with no token, and until
publication a draft's assets are served under `.../releases/download/untagged-.../`,
not `.../releases/download/vX.Y.Z/`. Measured on 0.1.5's draft (2026-10-16):
both assets and `GET /releases/{id}` returned 404 logged out. The 0.1.5
checklist put the anonymous check before publishing, and it could not be done.

- **11a. Before publishing, authenticated.** Download the draft's assets through
  the API (`gh api -H "Accept: application/octet-stream"
  repos/Paul-Nwokolo/kurukuru/releases/assets/<id>`) and run both checks. This
  proves the bytes attached to the draft are the bytes CI built, while the
  release can still be fixed quietly.
- **11b. Immediately after publishing (item 13), logged out.** `curl` with no
  token from `.../releases/download/vX.Y.Z/`, both checks again, before the
  release is announced or linked anywhere. Only this proves what a stranger
  gets; if it fails, the release is already public, so it is fixed by
  replacing the asset at once, and the notes say so.

**Proves:** what a stranger downloads is what was tested. This is item 6's
lesson applied to the published file: 0.1.0 worked for the person who built it
and for nobody else, and only a check made from a stranger's position would
have shown it.

**Caught:** it is how the CRLF checksum (item 9) was found. The 0.1.4 hashes
matched, and then `sha256sum -c` on the published file failed.

### 12. Release notes

What changed for a user. Every advisory fixed is named by its ID, and every
advisory recorded as not reachable is listed as such, never quietly left out.
A reader deciding whether to upgrade needs both.

**Editing a draft can unbind it from its tag. Read the release back after
every edit, and again just before publishing.** On 0.1.5, putting the notes
on the draft (`gh release edit` with the release id, and a `PATCH` of the body
as a fallback; which of the two did it was not established) reset the
draft's `tag_name` from `v0.1.5` to its placeholder `untagged-43d4494b9727c75ff04d`.
Nothing on the command line said so. Published like that, the release would
have been bound to a new tag named `untagged-...` on the target commit
instead of `v0.1.5`, with the release workflow's run, the attestation and the
PyPI upload all naming a tag the release did not. It was caught only because
the release was read back after the edit. Publishing the same draft
afterwards through the website did **not** reset `tag_name` (read back
after publication, 2026-10-16), so the reset has been observed only on
the command-line edit path, `gh release edit` or its fallback `PATCH`,
and not on the website's publish. The read-back:

```
gh api repos/Paul-Nwokolo/kurukuru/releases/<id> --jq '{tag_name, target_commitish, draft, assets: [.assets[].name]}'
```

`tag_name` must be exactly `vX.Y.Z`, `target_commitish` the tagged commit,
`draft` true, and both assets present. If `tag_name` is wrong, set it back
before anything else:
`gh api -X PATCH repos/Paul-Nwokolo/kurukuru/releases/<id> -f tag_name=vX.Y.Z`,
then read it back again. The draft's *page URL* saying `untagged-...` is
normal until publication and is not this fault; the `tag_name` field is what
matters. Sending `tag_name` in the same `PATCH` as the body is the obvious
guard, but it has not been measured to prevent this, so the read-back stays
the check.

### 13. Publish the release, then run item 11b at once.

Read the release back (above) immediately before clicking *Publish*.

## PyPI

**On PyPI since 0.1.5** (2026-10-16, release run 38882242582; DECISIONS #74).
The `pypi` job has exactly two ways in: a pushed `v*` tag with the switch on
(real PyPI), or a manual dispatch with `rehearse_testpypi` (TestPyPI only).
`tools/test_release_publishing.py` fails if either way in changes, if a third
appears, if a rehearsal could target real PyPI, or if the loud gate, the job
ordering, the tokenless publishing, the repository check or the propagation
handling is edited away. Each was watched to fail against a deliberate break.

### One-time setup, in this order

1. **Create the PyPI account** at pypi.org, verify the email, and turn on
   two-factor authentication. PyPI requires it before you can publish.
2. **Add a pending publisher**: pypi.org → *Your account* → *Publishing* →
   *Add a new pending publisher* → GitHub, with exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `kurukuru` |
   | Owner | `Paul-Nwokolo` |
   | Repository name | `kurukuru` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does **not** reserve the name. Anyone can still register
   `kurukuru` first; the first successful upload is what claims it.
3. **Create the GitHub environment `pypi`**: repository *Settings* →
   *Environments* → *New environment* → `pypi`. Add yourself as a **required
   reviewer** and limit deployments to tags matching `v*`. This is the human
   gate on every upload. The tag is pushed while the GitHub release is still a
   draft (item 3), and a PyPI version can never be uploaded twice, even after
   deletion. With a reviewer, the job waits for your approval after the
   installer and checksum jobs have passed.
4. **Turn it on**: repository *Settings* → *Secrets and variables* →
   *Actions* → *Variables* → *New repository variable*:
   `PYPI_PUBLISHING` = `enabled`. Only after 1–3. No token, no secret.
   **A repository variable, not an environment variable**: a job-level `if`
   cannot see an environment's variables, so the publish would skip (measured;
   `pypi-gate` now fails loudly if that happens).

### One-time setup for the TestPyPI rehearsal

TestPyPI is a separate service with separate accounts.

1. **Create a TestPyPI account** at test.pypi.org, verify the email, and turn
   on two-factor authentication.
2. **Add a pending publisher** there: test.pypi.org → *Your account* →
   *Publishing* → *Add a new pending publisher* → GitHub:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `kurukuru` |
   | Owner | `Paul-Nwokolo` |
   | Repository name | `kurukuru` |
   | Workflow name | `release.yml` |
   | Environment name | **`testpypi`** |

   The same values as real PyPI's except the environment, **deliberately**.
   The `pypi` environment only deploys `v*` tags, so a dispatch from `main`
   cannot use it without either weakening that rule or pushing a tag. And the
   difference is a safety property: real PyPI accepts only `pypi`, so no
   rehearsal can reach it, whatever the workflow's expressions say.
3. **Create the GitHub environment `testpypi`**: *Settings → Environments → New
   environment*. Add yourself as a **required reviewer**, so the rehearsal
   exercises the approval step too, and limit deployments to the `main` branch.
   Create it before the first rehearsal; GitHub would otherwise create it on
   first use, with no protection rules.

No variable is needed: the rehearsal is gated by the dispatch input, and only
a person with write access can dispatch.

### What the rehearsal proves, and what it cannot

**Proves**, on the commit you will tag: the build, the version check, trusted
publishing's OIDC exchange with a pending publisher for `release.yml`, the
environment approval, the upload action, and every `pypi-verify` check
(served bytes, name, version, exact repository URL, the installed command)
working end to end, as well as the `needs` chain that holds PyPI behind
`installer` and `checksum`.

**Cannot prove:**

- **That real PyPI will behave the same.** TestPyPI is a different index
  with its own CDN, so propagation times differ and are no prediction for the
  real run. A green rehearsal is evidence the mechanics work, not a guarantee
  the real run will be clean.
- **That real PyPI's publisher is right.** The rehearsal exchanges a token
  with TestPyPI's publisher (environment `testpypi`). A typo in real PyPI's
  publisher shows only on the real run, as a refused upload, which publishes
  nothing.
- **That the name is ours.** `kurukuru` on TestPyPI says nothing about
  `kurukuru` on PyPI.
- **Resolving the package by name and its dependencies from one index.** The
  rehearsal installs the TestPyPI file it just verified, with dependencies
  from real PyPI, because anyone can upload a `fastapi` to TestPyPI. The real
  run installs `kurukuru==X.Y.Z` by name from PyPI.
- **The exact real version.** The rehearsal publishes `X.Y.Z.dev<run id>`,
  so it never spends a real version and two rehearsals never collide. The
  wheel differs from the real one in its version string only.

### What the workflow expects

- A pushed tag `vX.Y.Z`, not `workflow_dispatch`, with `installer` and
  `checksum` both passed.
- `vars.PYPI_PUBLISHING == 'enabled'`.
- The `pypi` environment, matching the pending publisher's environment name.
- `id-token: write` on the job (it is in the workflow). PyPI trusts the run's
  OIDC identity: this repository, `release.yml`, environment `pypi`. A mismatch
  in any field makes PyPI refuse the upload; nothing is half-published.
- `tools/build_wheel.py` produces exactly one file,
  `kurukuru-X.Y.Z-py3-none-any.whl`, its version equal to the tag's, with the
  dashboard inside, a Project-URL that is exactly this repository, and a
  project page (`backend/PYPI_README.md`) that is Markdown with no relative
  links. It is refused **before** upload otherwise. The page used to be
  `backend/README.md`, dev notes whose `../` links would all have broken on
  PyPI.

### 14. `pypi-verify`: what `pipx install kurukuru` gives a stranger is ours

**Proves:** on a fresh runner with an empty pipx home, `pipx install
kurukuru==X.Y.Z` from PyPI installs:

- the same bytes (SHA-256) as the wheel the `pypi` job built;
- a distribution named `kurukuru`, version `X.Y.Z`;
- **a Project-URL that is exactly `https://github.com/Paul-Nwokolo/kurukuru`**,
  failing with `NOT OUR PACKAGE` otherwise. `kurukuru` is a name someone else
  could own; a package that does not name this repository is not ours,
  whatever its name and version say;
- a `kurukuru version` that reports `X.Y.Z`.

The same standard as item 11, applied to PyPI: checked from a stranger's
position, against what was built.

**Propagation, and how to tell it from a real failure.** PyPI's CDN is
eventually consistent. For a few minutes after an upload, some edges still
serve an index without the new version. The job retries only that, and only
for a bounded time:

| What the job reports | Means | Do |
|---|---|---|
| `kurukuru==X.Y.Z not on the index yet (try n/30)` | Waiting for the CDN, 10 s per try | Nothing |
| `pypi propagation`: still not served after 5 minutes | **Propagation, not a verified failure.** Nothing has been shown to be wrong with the upload | Check `pypi.org/project/kurukuru/X.Y.Z/`; re-run the job |
| PyPI serves different bytes | **Real.** PyPI never accepts the same filename twice, so a stale edge cannot serve other bytes under this name | Treat as not ours; investigate before anything else |
| `NOT OUR PACKAGE`, or a wrong name or version | **Real** | The package on PyPI is not this release |
| `pipx install … failed although the wheel is served` (after six tries, 20 s apart) | **Real**: the wheel is on the index but does not install | Read the pipx output |
| `kurukuru version` does not show `X.Y.Z` | **Real** | The installed command is not this build |

Only "not on the index" is retried. Everything after the wheel is found runs
once, because waiting longer cannot fix it.

### 15. The attestation verifies, with the command the release notes give

After `pypi-verify`, from your own machine, with no credentials:

```
pipx run pypi-attestations verify pypi --repository https://github.com/Paul-Nwokolo/kurukuru pypi:kurukuru-X.Y.Z-py3-none-any.whl
```

It must print `OK:`. The notes tell users to run this command, so it is
checked as written before anyone is told to rely on it. The certificate's
*Source Repository Ref* must be `refs/tags/vX.Y.Z` and its *Source Repository
Digest* the tagged commit (both are in the provenance at
`https://pypi.org/integrity/kurukuru/X.Y.Z/<file>/provenance`).

**Proves:** the file on PyPI was built and uploaded by this repository's
`release.yml`, from the tagged commit, in a run whose identity Sigstore
certified and logged publicly.

**Measured on the TestPyPI rehearsal (2026-10-15)**, using local files
because the `pypi:` form reads pypi.org: `OK` for our repository; refused for
another repository ("signed by repository "Paul-Nwokolo/kurukuru", expected
"someone-else/kurukuru""); refused for the same wheel with one byte appended
("subject does not match distribution digest"). The certificate named commit
`f244fbd`, `refs/heads/main`, trigger `workflow_dispatch`, and run 38801675885.
`pypi-attestations` calls its CLI experimental, which is one more reason to
run the command as written each release.

### If the upload succeeded and `pypi-verify` failed

Decide this before tagging, not after. **The state:** `kurukuru X.Y.Z` is on
PyPI permanently, the name is claimed by us, and that version number and
filename can never be uploaded again, even after deletion. The GitHub release
is still a draft, with the Windows installer and checksum attached and already
verified. INSTALL-LINUX still says not to use `pipx install kurukuru`, so no
documentation points anyone at the PyPI package.

- **Propagation** (`pypi propagation`): wait, then **Re-run failed jobs** on
  the run. That re-runs `pypi-verify` alone. GitHub reuses the successful
  `pypi` job's outputs, including the built wheel's hash, and passes the
  environment approval that already passed. Do **not** use "Re-run all
  jobs": it rebuilds everything and asks for approval again, and the upload
  then fails as a duplicate filename. Nothing is damaged, but the run reads
  as a failure. (The publish step does not set `skip-existing`, deliberately:
  a second upload of the same version should fail loudly, not pass.)
- **A real failure:** **yank** `X.Y.Z` on PyPI (*Manage project → Releases →
  Options → Yank*). pip then ignores it unless someone pins it exactly, it
  stays visible as yanked, and a yank can be undone. Prefer yanking to
  deleting, which frees nothing and loses the record. Fix the cause and cut the
  next patch version (`X.Y.Z+1`) through this same checklist. The Windows
  installer is unaffected: publishing the GitHub release for `X.Y.Z` is a
  separate decision, and its notes say the PyPI upload of that version was
  yanked and why. INSTALL-LINUX does not change.

### The gate on INSTALL-LINUX

**`docs/INSTALL-LINUX.md` may recommend `pipx install kurukuru` only after
`pypi-verify` has passed against a real upload.** Not after the account
exists, not after the pending publisher is added, not after the name is
claimed: claiming the name proves an upload happened, not that what installs
is ours. The change to INSTALL-LINUX goes in its own commit, citing the
passing run. Until then it keeps telling users not to run `pipx install
kurukuru`. If the first upload fails because someone else took the name, that
warning is already correct: their package is not this one.

**Met on 2026-10-16:** `pypi-verify` passed against the real 0.1.5 upload
in release run 38882242582, and INSTALL-LINUX was changed in its own commit
citing it. Every later release still has to pass item 14 before its notes
point anyone at the new version.
