# Releasing

The checklist for cutting a release. Each step exists because a release once
shipped without it; the decision number says which. A step that is automated
is still listed, with what to look at, because "the workflow went green" has
been wrong before (decision 68: green on a checksum file that failed for users).

## Before the tag

1. **Version** in `backend/kurukuru/product.py` (`VERSION`) — the one place it
   lives; the wheel and the installer both read it.
2. **CHANGELOG**: move `[Unreleased]` under the new version and date.
3. **Draft the GitHub release** for the tag. The release workflow uploads to an
   existing release; it never creates or publishes one.

## The release workflow (`v*` tag push)

4. **`installer`** builds on a clean Windows runner, checks both icons
   (decision 67 — every RT_ICON frame of `kurukuru.exe` and the setup `.exe`
   equal to `kurukuru.ico`'s), installs the build and runs it, then installs
   it **over the previous release** and proves the installed tree is this build
   byte for byte, with a negative control against the old one (decision 66).
5. **`checksum`** runs `sha256sum -c` on Linux, rejects any carriage return,
   and only then attaches the installer and its `.sha256` (decision 68).
6. **`pypi`** and **`pypi-verify`** — see [PyPI](#pypi). Skipped until the
   maintainer turns publishing on.

## After the workflow, before publishing the GitHub release

7. **Anonymous download-and-hash.** Logged out (or `curl` with no token),
   into an empty directory: download the installer and its `.sha256` from the
   release page, then check **both**: `sha256sum -c` prints `OK`, *and* the
   hash equals the one the `installer` job printed under "Record what was
   built". The first proves the file and its checksum agree; the second proves
   they are the bytes CI built.
8. **Release notes**: what changed for a user, every advisory fixed by its ID,
   and any advisory recorded as not reachable said so explicitly rather than
   left out.
9. Publish the release.

## PyPI

**Not yet on PyPI.** The first upload is planned for 0.1.5 (decision 74).
Until the setup below is done and the switch is on, the `pypi` job is skipped
on every tag, and `tools/test_release_publishing.py` fails if the gate or the
tokenless publishing is ever edited away.

### One-time setup, in this order

1. **Create the PyPI account** at pypi.org, verify the email, and turn on
   two-factor authentication (PyPI requires it before you can publish).
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
   reviewer** and limit deployments to tags matching `v*`. This is the
   human gate on every upload: the tag push happens while the GitHub release is
   still a draft (step 3 above), and a PyPI version can never be re-uploaded,
   even after deletion. With a reviewer, the job waits for your approval after
   the installer and checksum jobs have passed.
4. **Turn it on**: repository *Settings* → *Secrets and variables* →
   *Actions* → *Variables* → *New repository variable*:
   `PYPI_PUBLISHING` = `enabled`. Only after 1–3. Nothing else is needed: no
   token, no secret.

### What the workflow expects

- A pushed tag `vX.Y.Z` (not `workflow_dispatch`), with `installer` and
  `checksum` both passed.
- `vars.PYPI_PUBLISHING == 'enabled'`.
- The `pypi` environment, matching the pending publisher's environment name.
- `id-token: write` on the job (in the workflow) — PyPI trusts the run's OIDC
  identity: this repository, `release.yml`, environment `pypi`. A mismatch in
  any field makes PyPI refuse the upload; nothing is half-published.
- `tools/build_wheel.py` produces exactly one file,
  `kurukuru-X.Y.Z-py3-none-any.whl`, its version equal to the tag's, with the
  dashboard inside and a Project-URL naming this repository — refused
  before upload otherwise.

### After every upload — and before INSTALL-LINUX may recommend it

`pypi-verify` installs `kurukuru==X.Y.Z` with pipx into an empty pipx home on a
fresh runner and fails unless:

- PyPI serves the same bytes (SHA-256) as the wheel the `pypi` job built;
- the installed distribution's name is `kurukuru` and its version is `X.Y.Z`;
- one of its Project-URLs is `https://github.com/Paul-Nwokolo/kurukuru`;
- `kurukuru version` reports `X.Y.Z`.

That is the PyPI equivalent of the anonymous download-and-hash, and it is held
to the same standard. **`docs/INSTALL-LINUX.md` keeps telling users not to run
`pipx install kurukuru` until `pypi-verify` has passed for a published
release.** Only then does it change, in its own commit, citing that run. If
the first upload fails because the name was taken, the INSTALL-LINUX warning
is already correct: that package is someone else's.
