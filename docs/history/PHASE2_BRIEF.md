# PHASE 2 BRIEF — Compute Engine + Instance API

## Context
This repo is a "Local IaaS Orchestrator": a FastAPI backend that turns
Canonical Multipass into a self-service, EC2-style local cloud.
Phase 1 (scaffolding, DB, config) is DONE and working. Read these files
before writing any code — do not change their public interfaces:

- `backend/app/config.py`   — settings incl. `flavors` catalog,
  `multipass_binary`, `cli_timeout_seconds` (30s), `launch_timeout_seconds` (600s)
- `backend/app/models.py`   — `Instance` table, `InstanceCreate`/`InstanceRead`
  schemas, `InstanceStatus` enum (Pending, Provisioning, Running, Stopped,
  Terminated, Error), `Flavor` enum
- `backend/app/database.py` — engine, `get_session` dependency
- `backend/app/main.py`     — app entrypoint; has a commented include point
  for the instances router

Environment: Windows, Python 3.13, venv at `backend/.venv`, Multipass
installed and verified working. Run the server with:
`cd backend; .venv\Scripts\activate; uvicorn app.main:app --reload --port 8000`

## Architectural rules (non-negotiable)
1. **Abstract the hypervisor.** Create `backend/app/compute_engine.py` with an
   abstract base class `ComputeEngine` (methods: `provision_instance`,
   `start_instance`, `stop_instance`, `destroy_instance`, `get_instance_info`,
   `is_available`) and a concrete `MultipassEngine` implementing it. Routers
   must depend only on the abstract interface. No Multipass-specific strings
   outside `MultipassEngine`.
2. **DB is desired state.** Routes update the DB first, then drive the
   hypervisor. A reconciliation function syncs actual Multipass state
   (via `multipass list --format json` / `multipass info <name> --format json`)
   back into the DB: fills in `ip_address`, corrects `status`, marks rows
   `Error` with `error_message` when a VM vanished or launch failed.
3. **Non-blocking provisioning.** `POST /instances` must return **202** with
   the `Pending` record immediately, then run the launch in a FastAPI
   `BackgroundTasks` job that transitions Pending -> Provisioning -> Running
   (or Error). Never block a request for the up-to-600s launch.
4. **Robust subprocess handling.** All CLI calls via `subprocess.run` with:
   - `timeout=` from settings (30s fast ops, 600s launch)
   - `capture_output=True, text=True`
   - explicit handling for: `TimeoutExpired`, `FileNotFoundError` (Multipass
     not installed -> raise a clean `HypervisorUnavailableError`), non-zero
     exit codes (wrap stderr into a `ComputeEngineError`)
   - never `shell=True`; always pass args as a list
5. **Typed, enterprise-grade Python.** Full type hints, custom exception
   hierarchy, module-level logger (`logging.getLogger("iaas.compute")`),
   docstrings.

## API surface to build (`backend/app/routers/instances.py`)
- `POST   /instances`              body `InstanceCreate` -> 202 `InstanceRead`;
  reject duplicate names with 409; map flavor -> cpus/mem/disk via
  `settings.flavors`
- `GET    /instances`              list all (exclude Terminated by default;
  `?include_terminated=true` to include)
- `GET    /instances/{id}`         404 if missing
- `POST   /instances/{id}/start`   only valid from Stopped -> 409 otherwise
- `POST   /instances/{id}/stop`    only valid from Running -> 409 otherwise
- `DELETE /instances/{id}`         `multipass delete <name> --purge`, set
  status Terminated (keep the row for audit)
- `POST   /instances/refresh`      trigger reconciliation manually
- Register the router in `main.py` at the commented include point.

## State machine (enforce in routes)
Pending -> Provisioning -> Running <-> Stopped -> Terminated
Any state may go to Error (with error_message). Error and Terminated are
terminal for start/stop; DELETE is allowed from any state.

## Multipass command reference
- launch: `multipass launch --name <n> --cpus <c> --memory <m> --disk <d>`
  (use `--memory`; newer Multipass deprecated `--mem`. Detect and fall back
  to `--mem` if `--memory` errors with "unknown option".)
- info:   `multipass info <n> --format json` -> JSON with
  `info.<name>.state` ("Running"/"Stopped"/"Deleted") and `info.<name>.ipv4[0]`
- list:   `multipass list --format json`
- stop / start / delete as usual; delete always with `--purge`.
- Map Multipass states -> our enum: Running->Running, Stopped->Stopped,
  Deleted/missing->Terminated (or Error if DB says it should exist).

## Testing requirements (do these yourself before finishing)
1. `pytest`-able unit tests for `MultipassEngine` with `subprocess.run`
   mocked (success, timeout, missing binary, bad JSON).
2. Live smoke test: start the server, `POST /instances` with
   `{"name": "phase2-test", "flavor": "small"}`, poll `GET /instances` until
   Running with an IP, then stop, start, and DELETE it. Confirm
   `multipass list` shows it gone. Clean up everything you create.
3. Show me the final route list and test output when done.

## Out of scope for Phase 2
No frontend, no cloud-init (that is Phase 4 — but leave a
`cloud_init_path: str | None = None` parameter on `provision_instance` so
Phase 4 slots in without changing the interface).
