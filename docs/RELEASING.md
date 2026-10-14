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

## Before the tag

1. **Version** in `backend/kurukuru/product.py` (`VERSION`). The one place it
   lives; the wheel and the installer both read it, so they cannot disagree.
2. **CHANGELOG**: move `[Unreleased]` under the new version and date.
3. **Draft the GitHub release** for the tag. The workflow uploads to an existing
   release and never creates or publishes one: publishing stays a human act.

## The release workflow (`v*` tag push)

### 4. Installed and run on a clean Windows runner — `installer`

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

### 5. The icon check — `installer`, `build_installer.verify_icon`

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

### 6. Upgrade over the previous release — `installer`, `tools/verify_upgrade.py`

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

### 7. The checksum file, checked on Linux before it is attached — `checksum`

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

### 8. PyPI — `pypi`, then `pypi-verify`

See [PyPI](#pypi). Skipped on every tag until the maintainer turns it on.

## After the workflow, before publishing the GitHub release

### 9. Anonymous download-and-hash

Logged out (or `curl` with no token), into an **empty** directory: download the
installer and its `.sha256` from the release page, then check **both**:

- `sha256sum -c` prints `OK`, which proves the file and its checksum agree; and
- the hash equals the one the `installer` job printed under "Record what was
  built", which proves these are the bytes CI built and tested, not a file
  from your machine or a stale asset.

**Proves:** what a stranger downloads is what was tested. This is item 4's
lesson applied to the published file: 0.1.0 worked for the person who built it
and for nobody else, and only a check made from a stranger's position would
have shown it.

**Caught:** it is how the CRLF checksum (item 7) was found. The 0.1.4 hashes
matched, and then `sha256sum -c` on the published file failed.

### 10. Release notes

What changed for a user. Every advisory fixed is named by its ID, and every
advisory recorded as not reachable is listed as such, never quietly left out.
A reader deciding whether to upgrade needs both.

### 11. Publish the release.

## PyPI

**Not yet on PyPI.** The first upload is planned for 0.1.5 (DECISIONS #74).
Until the setup below is done and the switch is on, the `pypi` job is skipped
on every tag. `tools/test_release_publishing.py` fails if the gate, the job
ordering, the tokenless publishing, the repository check or the propagation
handling is edited away.

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
  dashboard inside and a Project-URL that is exactly this repository. It is
  refused **before** upload otherwise.

### 12. `pypi-verify`: what `pipx install kurukuru` gives a stranger is ours

**Proves:** on a fresh runner with an empty pipx home, `pipx install
kurukuru==X.Y.Z` from PyPI installs:

- the same bytes (SHA-256) as the wheel the `pypi` job built;
- a distribution named `kurukuru`, version `X.Y.Z`;
- **a Project-URL that is exactly `https://github.com/Paul-Nwokolo/kurukuru`**,
  failing with `NOT OUR PACKAGE` otherwise. `kurukuru` is a name someone else
  could own; a package that does not name this repository is not ours,
  whatever its name and version say;
- a `kurukuru version` that reports `X.Y.Z`.

The same standard as item 9, applied to PyPI: checked from a stranger's
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
