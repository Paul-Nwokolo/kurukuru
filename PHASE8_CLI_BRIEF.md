# PHASE 8 BRIEF — Command-Line Interface

Read docs/ARCHITECTURE.md and docs/API.md first — the CLI is a second
client of the existing HTTP API and must not reach into the database,
engines, or filesystem directly. Anything it needs that the API cannot
express is a missing endpoint, not a reason to bypass the API.

Strategic context: this is the first "widening" phase. The audience is
platform/infra engineers who live in terminals and scripts, and for whom
the CLI — not the dashboard — is the primary interface.

## Naming
The product name is not final. Define the command name ONCE as a constant
(e.g. `CLI_NAME` in a single module, plus the console_scripts entry point)
so renaming later is a one-line change. Use `iaas` as the working name
throughout until told otherwise. Do not scatter the literal string.

## Architecture
- Thin HTTP client over the existing API. No direct DB/engine access.
- Ships from the backend package: a `console_scripts` entry point in
  pyproject/setup so `pip install -e backend` puts the command on PATH.
  (Add pyproject.toml if the backend doesn't have one — keep
  requirements.txt working.)
- Library: Typer (Click underneath) for commands and help; rich for
  tables/spinners. Add both to requirements.
- Config resolution order, documented in `--help`:
  1. `--api-url` flag  2. `IAAS_API_URL` env  3. `~/.local-iaas/cli.toml`
  4. default `http://127.0.0.1:8000`
- When the API is unreachable: a clear, actionable error naming the URL
  tried and suggesting `<cli> serve` — never a raw traceback or a stack
  of httpx internals. Exit code 3 (see below).

## Commands

### Instances
- `<cli> launch NAME` — options: `--preset small|medium|large`,
  `--cpus`, `--memory` (accepts `2G`/`2048M`/plain MB), `--disk` (`20G`),
  `--mode quick|iso|image` (maps to the Phase 7 intent modes),
  `--iso NAME`, `--image NAME_OR_ID`, `--accel`, `--display`,
  `--wait` (poll until Running or Error), `--timeout`, `--json`.
  Without `--wait`, print the id and status and exit immediately —
  mirroring the API's 202 semantics. With `--wait`, show a progress
  spinner with the current status, and exit non-zero if it lands in Error
  with the error_message printed.
- `<cli> ls` — table: name, status, address, size, image/boot source, age.
  Flags: `--all` (include terminated), `--json`, `--watch` (re-render on
  an interval; Ctrl-C exits cleanly).
- `<cli> show NAME_OR_ID` — full detail including ports, accel, display,
  console_caveat, error_message. `--json`.
- `<cli> start|stop NAME_OR_ID` — `--wait` optional. Surface the API's
  409s as readable messages ("cannot start: instance is Running").
- `<cli> rm NAME_OR_ID` — terminate. `--force` maps to force-terminate,
  `--yes` skips the confirmation prompt. Confirm interactively by default;
  never prompt when stdout is not a TTY (scripts must not hang) — require
  `--yes` in that case and say so.
- `<cli> ssh NAME_OR_ID [-- ARGS...]` — resolve the instance, build the
  same command lib/ssh.ts builds, and exec it (replacing the process on
  POSIX; subprocess on Windows). Pass trailing args through to ssh.
  Refuse with a clear message when the instance has no SSH access
  (ISO/no-cloud-init instances), pointing at `console`.
- `<cli> console NAME_OR_ID` — open the dashboard's console page for that
  instance in the default browser. If console_caveat is set, print it
  first. (A terminal-native VNC client is out of scope.)

### Images and ISOs
- `<cli> images ls|import|rm` mirroring the API. `import` takes a path and
  `--name`, `--cloud-init/--no-cloud-init`, `--wait`.
- `<cli> isos ls`.

### System
- `<cli> serve` — run the backend (uvicorn) in the foreground. Flags:
  `--host`, `--port`, `--reload`. This is a convenience wrapper, not a
  process manager: no daemonizing, no PID files. Document that service
  installation comes later.
- `<cli> capacity` — the host capacity view: totals, committed,
  allocatable, accelerator. `--json`.
- `<cli> doctor` — diagnostics, each line PASS/WARN/FAIL with a remedy:
  API reachable; QEMU binary found + version; accelerator available;
  base image present; instance store path writable and free space;
  SSH keypair present; Python/CLI version. Exits non-zero on any FAIL.
  This is the first thing a confused new user should run — write the
  remedies as if to someone who has never seen the project.
- `<cli> version` — CLI version, API version, QEMU version.
- `<cli> completion [bash|zsh|fish|powershell]` — shell completion.

## Scripting contract (treat as a hard requirement)
- `--json` on every read command emits parseable JSON to stdout and
  nothing else. Human output goes to stdout only when not `--json`;
  progress, spinners, and warnings go to stderr so pipes stay clean.
- Exit codes: 0 success; 1 generic failure; 2 usage error (Typer default);
  3 API unreachable; 4 not found; 5 conflict/invalid state (API 409);
  6 validation/capacity refusal (API 422); 7 operation timed out
  (`--wait`). Document the table in `--help` and in docs.
- Colour and spinners auto-disable when not a TTY or when `NO_COLOR` is
  set.
- `<cli> launch web-01 --wait && <cli> ssh web-01` must work as one line.

## Errors
The API already returns explanatory messages (the capacity 422s spell out
the arithmetic; 409s name the blocking state). Print the API's `detail`
verbatim rather than substituting a generic message — that work is done
and the CLI should not throw it away.

## Testing
- Unit: use FastAPI's TestClient as the transport so command tests run
  against the real app without a live server (inject the client). Cover
  every command's happy path, the exit-code table, `--json` shape, and
  the not-a-TTY behaviours (no prompt, no colour).
- A test asserting `--json` output contains no non-JSON contamination.
- Live: with the backend running, exercise the headline flow end to end —
  `launch --wait`, `ls`, `show`, `ssh` running a remote command, `stop`,
  `start`, `rm` — plus `doctor` and `capacity`. Report the transcript.
- Existing suites stay green.

## Docs
- New `docs/CLI.md`: install, config, every command with examples, the
  exit-code table, and a scripting section showing a real pipeline
  (launch several instances from a loop, wait, collect IPs as JSON).
- README: add a CLI section near the top — for this audience it is a
  headline feature, not an appendix.

## Out of scope
Remote/multi-host support, auth (no auth exists yet), a terminal VNC
client, service/daemon installation, packaging beyond the entry point.
