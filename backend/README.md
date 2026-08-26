# Backend — dev notes

Setup, requirements and the first-launch walkthrough live in the
[root README](../README.md). This file is backend-specific notes only.

## Run

```bash
uvicorn kurukuru.main:app --reload --port 8000
```

Or, with the package installed (`pip install -e .` from this directory, which is
also what puts the `kurukuru` command on `PATH`):

```bash
kurukuru serve --reload
```

`kurukuru serve` changes to this directory first, so the SQLite URL and `.env`
resolve the same way wherever it is launched from.

`--reload` is safe with running VMs: they are spawned detached, so they survive
a backend restart and are picked up again by the reconciler.

- Swagger UI: <http://localhost:8000/docs>
- OpenAPI schema: <http://localhost:8000/openapi.json>

## Tests

```bash
pytest -q
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for conventions and the live
verification expectation.

## Layout

```
kurukuru/
  product.py         the names: product, command, env prefix, state dir, database
                     leaf, and what each was before Phase 16
  main.py            app wiring, lifespan, system endpoints, reconcile loop
  config.py          pydantic-settings; every KURUKURU_ knob
  database.py        engine, WAL pragma, additive migrations, pre-migration backups
  state_migration.py one-time move of a pre-Phase-16 ~/.local-iaas
  models.py          SQLModel tables + API schemas
  host_capacity.py   psutil probes and the allocatable arithmetic
  console.py         VNC <-> WebSocket byte pump
  cloud_init.py      #cloud-config rendering
  ssh_keys.py        orchestrator keypair
  image_store.py     qemu-img probing, import copy
  isos.py            boot-media catalog (traversal-safe)
  engines/
    base.py          ComputeEngine ABC, InstanceInfo, LaunchOptions, exceptions
    qemu.py          the driver: overlays, seed ISO, QMP, lifecycle
    qmp.py           minimal blocking QMP client
    seed.py          NoCloud CIDATA seed ISO (pycdlib)
    images.py        base image download
    ports.py         host port allocation
    process.py       detached spawn + Windows-safe pid liveness
  routers/
    instances.py     lifecycle, sizing, reconciler, console route
    images.py        image catalog
  cli/               the `kurukuru` command — an API client, nothing more
    main.py          root Typer app, global options, error boundary
    client.py        the only route to the system (injectable transport)
    naming.py        CLI_NAME and friends: the command's name, in one place
    config.py        --api-url / KURUKURU_API_URL / cli.toml / default
    output.py        stdout vs stderr, --json purity, TTY and NO_COLOR rules
    formats.py       size parsing (2G/2048M), table formatting
    support.py       name resolution and the --wait loop
    commands_*.py    instances, images/isos, system
tests/
```

`kurukuru/cli/` imports nothing from the layers above — no router, model, session or
engine. It is a second client of the HTTP API, and a capability it needs that
the API lacks is a missing endpoint. See [docs/CLI.md](../docs/CLI.md).

Everything hypervisor-related is imported from `app.engines`, which re-exports
the ABC, the value objects and the registry.

## Runtime state on disk

Under `KURUKURU_QEMU_DIR` (default `~/.kurukuru/qemu/`). The per-instance
directory is the engine's real state — see
[ARCHITECTURE.md](../docs/ARCHITECTURE.md#per-instance-directory).

The SQLite database is `KURUKURU_STATE_DIR/kurukuru.db` (default
`~/.kurukuru/kurukuru.db`), created on first run and migrated in place on every
startup. Deleting it loses the instance records but not the VMs; the reconciler
will not re-adopt them, so terminate anything you care about first.

It used to live at `./kurukuru.db`, relative to wherever the backend was started
from. If you have a database from that era in `backend/`, the first start after
this change moves it — with its `-wal` and `-shm` sidecars — into the state
directory, and says so in the log. It never writes over a database that is
already there: if both locations hold data you get a warning naming the two
paths and nothing is touched.
