# Contributing

## Running the tests

Backend (from `backend/`, with the venv active):

```bash
pytest -q                          # whole suite, ~14s
pytest tests/test_qemu_engine.py   # one module
pytest -q -k capacity              # by keyword
```

The suite runs against fakes and an in-memory SQLite database. It does not
require QEMU, a network, or a running backend — with one exception:
`tests/test_images_api.py` uses real `qemu-img` to create and probe small sparse
qcow2 files, and skips itself if the binary is absent. Probing is the validity
gate for the whole image feature, and asserting it against a mock would only
prove the mock.

Frontend (from `frontend/`):

```bash
npm run verify      # typecheck, lint, colour, contrast, console, name, build
```

Or individually:

```bash
npm run typecheck      # tsc -b --force
npm run lint           # oxlint
npm run check:colour   # no hard-coded colours outside the token layer
npm run check:contrast # WCAG AA over every token pair the UI uses
npm run check:console  # the VNC console's connect ordering
npm run check:name     # the product's name is never spelled in copy
npm run build
```

All of them must pass.

> **Do not run `npx tsc --noEmit`.** The root `tsconfig.json` is
> solution-style — `"files": []` plus project references — so that command
> type-checks **nothing** and exits 0 no matter what is broken. It was quoted
> as evidence for several phases before anyone tested it against a deliberate
> error. `tsc -b` is the real check, which is what `npm run typecheck` and the
> build both run.

There is no frontend test runner; the frontend's logic is kept thin enough that
the backend suite plus live verification covers it. What guards the design
system instead is `check:colour` (no hard-coded colours outside
`src/styles/tokens.css`) and `check:contrast` (WCAG AA over every token pair the
UI actually renders).

`check:name` guards a different kind of drift. The CLI is called `kurukuru` today
and will not be forever, so the name has one definition — `CLI_NAME` in
`backend/kurukuru/product.py` — and reaches the dashboard as `cli_name` on
`GET /auth/first-run`. Copy renders it through `cliCommand()`; nothing spells
it. The strings this protects are the ones on the **login screen**, read by
someone who is locked out and following them literally: a rename that left
those naming the old command would break the one path with no workaround.
Comment lines are exempt, and so are wire identifiers (the CSRF header, the
cookie, localStorage keys), each with its reason in the script's allowlist.

`check:console` is the exception to "thin enough", and it is worth
understanding as a pattern rather than a one-off. The console panel once sat on
"Connecting…" forever because the WebSocket was opened *before* the noVNC chunk
finished loading: noVNC assigns `onmessage` when it attaches, and QEMU's RFB
greeting had already been dispatched to nobody. Typecheck, lint, the whole
backend console suite and a clean build all passed — there was nothing to see,
because the failure was an event with no listener.

So the four lines whose *order* is the behaviour live in `src/lib/console.ts`
with their collaborators injected, and the check drives the real function
against a fake channel that reproduces noVNC's attach semantics. Time in it is
counted in event-loop turns rather than milliseconds: a test that reproduces a
race by racing is a test that passes on a fast machine. Reverting the ordering
fails it every run — which is the only reason to believe it.

If you find yourself with logic that no type can describe and no build can
exercise, that shape — extract the ordering, inject the collaborators, drive it
from a script — is cheaper than a test runner and has caught more.

### Tests cannot touch your real install

Every test runs against settings and a database rooted in its own `tmp_path`.
This is applied by an autouse fixture in `tests/conftest.py` that **discovers**
which modules to redirect — it walks the imported `app.*` modules looking for
anything holding a `db_engine` or `get_settings` reference — so a module added
later is covered the moment it is imported. There is no list to maintain and
nothing to remember.

That design replaced an opt-in one, which failed twice in the way opt-in
designs do:

- **Phase 10** — `POST /keypairs/generate` runs real `ssh-keygen`. One fixture
  pinned the ISO directory and not the key directory, so ten orphaned keypairs
  accumulated in a developer's `~/.kurukuru/keys` before anyone looked.
- **Phase 11** — the new event writer held its own engine reference. Three of
  four test modules were updated; the fourth was not, and 130 rows landed in
  the real `kurukuru.db`. It surfaced as test instances named `web-one` and
  `doomed` appearing in the live dashboard's activity feed.

Neither was a bug in the code under test. Both passed CI.

Two guards back the isolation up, because isolation that silently stops working
is worse than none:

- **The database is prevented.** Opening the real `kurukuru.db` raises immediately,
  from a patch on `sqlite3.connect` *and* `sqlite3.dbapi2.connect` — they are
  separate module objects and SQLAlchemy imports the second one. You get a
  traceback at the offending line.
- **The state directories are detected.** `~/.kurukuru` and its `keys` and
  `cloud-init` subdirectories are fingerprinted before and after every test; a
  change fails that test by name. Detection rather than prevention here, so the
  stray file does get written — the run tells you which test to blame and you
  delete it.

The database is deliberately *not* watched by mtime. Your own backend is
probably running while you run the tests, and its reconciler writes every 30
seconds, so a timestamp check blamed whichever test straddled a reconcile pass.
A guard that cries wolf gets deleted.

If a test genuinely needs the real paths, mark it `@pytest.mark.real_state` and
say why in the same commit. Nothing does today.

`tests/test_isolation.py` tests the harness itself, including running a
deliberately leaking test in a subprocess and asserting the run fails — a guard
nobody has watched fire is a guard nobody knows works.

## Live verification is expected

**Anything touching the hypervisor must be verified against a real VM before it
is called done.** The test suite cannot catch a wrong QEMU flag, a broken boot,
or a console that connects but renders nothing — all three have happened here,
and all three passed the unit tests first.

What that means in practice: launch a real instance through the running backend,
watch it reach `Running`, and exercise the thing you changed — SSH in, open the
console, stop and start it. Include what you observed in the change description.

Two techniques that have repeatedly paid off:

- **Ask the hypervisor, not the abstraction.** `qemu-img info` on an overlay
  tells you the real backing file. QMP `screendump` plus a pixel histogram tells
  you whether a framebuffer actually contains anything — counting distinct
  colours distinguishes "renders" from "black rectangle" far more reliably than
  looking at a screenshot.
- **Test the whole path.** A console that connects proves the socket works, not
  that pixels arrive. Type into the guest and check the screen changed.

## Verification integrity

**A check you have never watched fail is not evidence.**

Every green result in this project is a claim about something you cannot see
directly, and three times that claim has been false — not because a test was
wrong, but because the *thing reporting* was not measuring anything. All three
were found by accident, weeks after they started lying.

| What was reported | What was actually happening |
|---|---|
| `npx tsc --noEmit` → exit 0, quoted as "typecheck clean" for several phases | The root `tsconfig.json` is solution-style (`"files": []` plus references), so the command type-checked **zero files** and could not fail |
| `npx tsc \| head && echo CLEAN`, and later `kurukuru launch x \| tail; echo $?` | `$?` is the *last* command in a pipeline — `head`'s status, `tail`'s status. A failing command with a successful pager reads as success |
| `npm run verify` → exit 0 across typecheck, lint, colour, contrast and build | The dev server was serving a stale module graph. The app rendered a blank page. The build output was accurate and irrelevant |

Three rules follow, and they are cheap:

**1. Prove a new guard fires. Break something on purpose.** When you add a
check — a lint rule, a contrast script, a test-isolation fixture — make it fail
once before you trust it. `tests/test_isolation.py` does this deliberately: it
runs a leaking test in a subprocess and asserts the run goes red, because a
guard nobody has watched fire is a guard nobody knows works. The
`npx tsc --noEmit` gap survived for phases and took ten seconds to expose: add a
bogus prop, watch it pass, and you know.

**2. Read exit codes directly.** Never through a pipe.

```bash
npm run typecheck; echo "TSC=$?"                      # right
npm run build 2>&1 | tail -3; echo "${PIPESTATUS[0]}" # right, if you must pipe
kurukuru launch x >/dev/null 2>&1; echo "exit: $?"        # right

npm run typecheck | head && echo CLEAN                # WRONG: head's status
kurukuru launch x --wait | tail; echo $?                  # WRONG: tail's status
```

`set -o pipefail` is not on in `sh`, so it will not save you. The same trap
applies to any `cmd | grep` used as a pass/fail test — `grep`'s "not found"
silently becomes the verdict.

**3. Confirm UI changes in a browser, not in build output.** A clean build
proves the code compiles and bundles. It says nothing about whether the app
mounts, whether a token resolves, or whether a module resolved to the version
you just wrote. Open the page. Assert against the live DOM — computed styles,
rendered text, whether the root element has any children at all:

```js
getComputedStyle(document.documentElement).getPropertyValue('--surface')
document.getElementById('root').innerText.length > 0
```

And read the browser console for errors, which is where the stale-module
failure was actually visible the whole time.

**4. Never match a process by a pattern your own command line contains.**
`pkill -f qemu-system-x86_64` over SSH matches the *remote shell running that
very command*, so it can kill the session — and if the pattern is broader, the
things around it. The same trap catches `pgrep -f`, `ps | grep`, and any
`kill $(...)` built from them; it has bitten this project twice, once taking
out an SSH session mid-run.

Match on the process name rather than the full command line, and kill by pid:

```bash
for p in $(ps -eo pid,comm | awk '$2 ~ /^qemu-system/ {print $1}'); do
  kill -TERM "$p"
done                                          # right

pkill -f qemu-system-x86_64                   # WRONG: matches this shell too
```

`ps -eo pid,comm` compares against the executable name, which your `ssh`,
`bash` and `awk` do not share. Note that `comm` is truncated to 15 characters
on Linux — `pgrep` will even warn you about this — so anchor the pattern at the
start and keep it short rather than spelling out a long binary name that will
never match.

The general shape: **prefer a check that can distinguish "working" from
"not running at all".** Most false greens in this project were not wrong
answers — they were no answer, formatted like a good one.

## Conventions

**Typed Python.** `from __future__ import annotations` at the top,
`X | None` unions, dataclasses for value objects, SQLModel for anything
persisted. Public functions carry docstrings that say *why*, not what.

**Subprocess calls** are always an argument **list**, never `shell=True`, always
with an explicit `timeout`, and always with the three failure modes translated
into the project's exception hierarchy: missing binary →
`HypervisorUnavailableError`, timeout → `ComputeTimeoutError`, non-zero exit →
`ComputeEngineError` carrying stderr. Put the tool's own stderr in the message —
`qemu-img`'s explanation is more useful than ours.

**Migrations are additive**, applied by `init_db()` on every startup and
idempotent. Add a column to `_ADDED_COLUMNS` in `kurukuru/database.py`; redefine an
index through `_REDEFINED_INDEXES`. Data backfills go in
`_backfill_instance_sizing` or a sibling. SQLite cannot drop a table constraint
in place, so prefer designs that do not require it — an index can be dropped and
recreated, a `NOT NULL` cannot. Add a case to `tests/test_migrations.py`, which
builds a genuine pre-migration database and asserts convergence without data
loss.

**Tests accompany behaviour changes.** Not coverage for its own sake — a test
should describe a behaviour someone could plausibly break. Prefer real objects
over mocks where the real thing is cheap: a loopback TCP server instead of a
mocked socket, a real qcow2 file instead of a stubbed probe. Name tests after
the behaviour (`test_stopped_instances_release_their_memory`), and use the
docstring to record *why* the behaviour matters, especially for regressions.

**Comments explain reasoning.** Assume the reader can see what the code does.
Where a line encodes a hard-won fact — a WHPX quirk, a Windows API trap, an
ordering that prevents a race — say so, so it survives the next refactor.

## Restoring the database from a backup

The backend takes a backup of `kurukuru.db` **automatically, immediately before an
additive migration changes the schema** — and at no other time. An ordinary
startup against an up-to-date database writes nothing, because a copy on every
restart would fill the retention window with identical files and age the one
useful restore point out of it.

Backups live in `~/.kurukuru/backups/` (`KURUKURU_DB_BACKUP_DIR`), named
`kurukuru-<timestamp>-pre-migration.db` (and `iaas-…` for backups taken
before the Phase 16 rename — retention still counts them as its own, and
orders both by the timestamp inside the name rather than by the prefix). The
newest `KURUKURU_DB_BACKUP_RETENTION`
(default 5) are kept and older ones pruned; anything in that directory *not*
matching that pattern — a hand-made copy, a notes file — is never touched.

**Restore is manual and deliberately not an API.** Putting "replace the
database" behind an HTTP route means a mis-click can discard live state, and
the situations that call for a restore are exactly the ones where you want a
human reading the filenames.

### The procedure

1. **Stop the backend.** SQLite will let you overwrite a file that an open
   connection is using, and the result is a process holding a page cache for a
   database that no longer exists underneath it.

2. **Pick the backup.** They sort chronologically by name; the one you want is
   normally the newest whose timestamp is *before* the upgrade that went wrong.

   ```
   ls ~/.kurukuru/backups/
   ```

3. **Move the current database aside rather than deleting it** — including its
   sidecars. It may still be the better copy, and you cannot tell until after
   you have looked at the other one.

   ```
   cd ~/.kurukuru
   mv kurukuru.db kurukuru.db.broken
   mv kurukuru.db-wal kurukuru.db-wal.broken 2>/dev/null
   mv kurukuru.db-shm kurukuru.db-shm.broken 2>/dev/null
   ```

4. **Copy the backup into place.** One file, and only one:

   ```
   cp backups/kurukuru-20260821-132229-pre-migration.db kurukuru.db
   ```

   There is no `-wal` or `-shm` to bring with it, and that is the point. The
   backup is taken through SQLite's **online backup API**, not by copying
   files: with WAL journalling the committed state is spread across `kurukuru.db`
   and `kurukuru.db-wal`, so copying them one at a time captures two different
   moments and can produce a pair that do not agree. The online backup reads a
   consistent snapshot through SQLite itself — safe against a backend that is
   mid-write — and folds the WAL contents into a single self-contained file.
   A backup with a sidecar next to it is not one of ours.

5. **Verify before starting the backend**, while it is still cheap to change
   your mind. Compare row counts against the database you set aside:

   ```
   for db in kurukuru.db kurukuru.db.broken; do
     echo "== $db"
     sqlite3 "$db" "SELECT 'instances', COUNT(*) FROM instances
                    UNION ALL SELECT 'volumes', COUNT(*) FROM volumes
                    UNION ALL SELECT 'projects', COUNT(*) FROM projects
                    UNION ALL SELECT 'events', COUNT(*) FROM instance_events;"
   done
   ```

   A restored database is expected to have **fewer** rows than the one it
   replaces — it predates whatever happened since. What it must not have is
   *zero* where the other has many, or a missing table; either means you picked
   the wrong file or the copy was truncated.

6. **Start the backend.** It will run the additive migrations against the
   restored database on the way up, which is the same path that produced the
   backup in the first place — and it will take a fresh backup first, because
   the restored file is once again a schema behind.

7. **Reconcile.** The database is desired state, not truth. Instances created
   after the backup was taken exist on the hypervisor but not in the restored
   rows; the reconciler will report them as out-of-band. Read
   `~/.kurukuru/qemu/instances/` to see what is actually there before
   deciding what to do about the difference.

Once you are satisfied, delete the `.broken` files. Not before.

## Making a change

1. Read the surrounding code first. Where the code and the phase briefs in
   `docs/history/` disagree, the code is correct; the briefs are historical
   intent.
2. Add or update tests alongside the change.
3. Run both suites, reading exit codes directly — see
   [Verification integrity](#verification-integrity).
4. Verify live if the hypervisor is involved; in a browser if the UI changed.
5. Update the docs when behaviour changes — particularly
   [`docs/API.md`](docs/API.md) for route changes and
   [`docs/DECISIONS.md`](docs/DECISIONS.md) when you make a call worth
   remembering.

## Version control

Git was initialised late — phases 1 through 12 and the post-review fixes landed
as a single first commit, because there was no repository to put them in while
they were being written. That is the one thing about this history that cannot be
fixed, and it is the reason for the rule below.

**Commit at the end of each phase.** Not at the end of the project. A phase is
the natural unit here: it has a brief, it ends with both suites green and a live
verification, and that is exactly the state worth being able to return to. A
phase that is not committed is a phase that exists only on one disk.

**Never commit a `.env` or a database file.** Both are ignored, and the ignore
rules are not the interesting part — the reasoning is:

- **`.env` is per-machine, and grows credentials.** Today `backend/.env` holds a
  binary path. The moment this grows a registry token or a remote host password,
  a tracked `.env` publishes it, and a secret that has been committed stays in
  the history after you delete it. `.env.example` is the tracked half: it
  documents every variable and holds no values.
- **`kurukuru.db` is state, not source.** It is the desired state of one machine's
  VMs — meaningless on any other machine, conflicting on every pull, and a
  record of what you have been running. Its `-wal` and `-shm` sidecars are
  ignored by name for a separate reason: a `-wal` holds committed transactions
  the `.db` has not absorbed yet, so a repo carrying one without the other is
  worse than one carrying neither.

Also ignored, and for the ordinary reasons: `.venv/`, `node_modules/`, `dist/`,
`__pycache__/`, `*.egg-info/`, disk images (`*.qcow2`, `*.img`, `*.iso`), and
`.claude/settings.local.json` — see below for why that last one matters.

### Credentials never go in a tool config file, ignored or not

Not in `.claude/settings.local.json`, not in a `.vscode/tasks.json`, not in a
shell history file a tool keeps for you. "It is gitignored" is not the argument
people think it is.

This is not hypothetical here. An audit of this repo's
`.claude/settings.local.json` — 252 approved-command rules, accumulated one
prompt at a time — found the Linux test host's password in cleartext four
times, alongside the username and the public IP needed to use it:

| What | Occurrences |
|---|---|
| The host password in cleartext — as a `-pw` argument and piped into `sudo -S` | 4 |
| `claude@<public-ip>` / `ubuntu@<public-ip>` — the accounts it opens | 9 |
| The host's SSH host-key fingerprint | 2 |
| Paths to a personal private key (paths only, no key material) | 7 |
| The developer's local username, in absolute paths | 47 |

No key material and no environment variables were in the file, which is the one
piece of good news. But a password, a username and a routable address together
are not three findings — they are one working login, sitting in a plain JSON
file in a project directory.

The mechanism is worth understanding, because nobody typed that file. It is
written by *approving commands*: every one-off `plink -pw … ` or
`echo <password> | sudo -S …` that gets approved is recorded verbatim so it can
be auto-approved next time. The credential is a side effect of getting on with
the work, which is exactly why it needs a rule rather than good intentions.

So:

- **Never put a secret in a command you approve.** Use key authentication, an
  SSH agent, a password manager's CLI, or an environment variable read at the
  point of use — anything that keeps the secret out of the command line. It also
  keeps it out of `ps` output and your shell history, which have the same
  problem.
- **`.gitignore` is the last line, not the first.** It stops a commit; it does
  nothing about backups, sync clients, screen shares, crash reports, or the
  support bundle you paste into an issue. A secret in an ignored file is still a
  secret on disk in cleartext.
- **A credential that has been written down is a credential to rotate.** Not
  delete-the-file — rotate. You cannot know what copied it in the meantime.

Before the first commit on any new clone, check what is actually staged rather
than trusting the ignore file:

```bash
git diff --cached --name-only | grep -Ei '\.env$|\.db|\.venv/|node_modules/|settings\.local' ; echo "matches: $?"
```

Exit 1 from `grep` means no matches, which is the answer you want — and is
exactly the inverted-exit-code trap that
[Verification integrity](#verification-integrity) warns about, so read it
deliberately rather than at a glance.

## Layout

```
backend/kurukuru/            control plane
backend/kurukuru/engines/    ComputeEngine ABC and drivers
backend/tests/          pytest suite
frontend/src/           React dashboard
docs/                   architecture, API, decisions
docs/history/           phase briefs (historical)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map and
[the "adding an engine" section](docs/ARCHITECTURE.md#adding-an-engine) if you
are writing a driver.
