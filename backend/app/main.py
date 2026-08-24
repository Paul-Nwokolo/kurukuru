"""
Local IaaS Orchestrator — API entrypoint.

Run (dev):
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.config import Settings, get_settings
from app.database import get_session, init_db
from app.engines import EngineRegistry, get_engine_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("iaas")

settings = get_settings()


def _reconcile_once() -> None:
    """One full reconciliation pass, in its own DB session (blocking)."""
    from sqlmodel import Session

    from app.database import engine as db_engine
    from app.engines import get_engine_registry
    from app.routers.instances import reconcile_all

    with Session(db_engine) as session:
        reconcile_all(session, get_engine_registry())


async def _reconcile_loop(interval: int) -> None:
    """Periodically fold hypervisor state back into the database.

    Without this the DB only ever learns the truth twice: once after launch and
    whenever a human presses Refresh. Anything missed in that first sample — an
    IP not yet published, a hypervisor that was briefly unreachable — stuck
    permanently, and out-of-band changes (a VM stopped from the CLI) never
    showed up at all.

    Sleeps *first* so startup stays fast, runs the blocking pass in a worker
    thread so the event loop keeps serving, and never lets an exception kill the
    loop — an engine being down must not end reconciliation for everything else.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_reconcile_once)
        except Exception:  # noqa: BLE001 - the loop must outlive any single pass
            logger.exception("Background reconciliation pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown hooks."""
    # The resolved URL, not the configured one: "~/.local-iaas/iaas.db" and the
    # absolute path it expands to are the same setting, but only one of them is
    # a path you can go and look at.
    logger.info("Initializing database (%s)...", settings.resolved_database_url)
    init_db()

    # Seed the image catalog so it is never empty and every instance can record
    # which image it came from, including ones launched without choosing.
    from app.routers.images import ensure_builtin_image

    try:
        ensure_builtin_image(settings)
    except Exception:  # noqa: BLE001 - a catalog hiccup must not block startup
        logger.exception("Could not register the built-in image")

    # A project to file everything under. The migration seeds it too, but a
    # database built by create_all alone never runs the migration.
    from app.routers.projects import ensure_default_project

    try:
        ensure_default_project()
    except Exception:  # noqa: BLE001 - same reasoning as the image catalog
        logger.exception("Could not create the default project")

    # The network every instance is on. Same reasoning as the default project.
    from app.routers.networks import ensure_default_network

    try:
        ensure_default_network()
    except Exception:  # noqa: BLE001 - same reasoning as the image catalog
        logger.exception("Could not create the default network")

    # Adopt (never regenerate) the orchestrator's own keypair as a catalog row,
    # so it is selectable in the UI and stays the default for new launches.
    from app.routers.keypairs import ensure_orchestrator_keypair

    try:
        ensure_orchestrator_keypair(settings)
    except Exception:  # noqa: BLE001 - same reasoning as the image catalog
        logger.exception("Could not register the orchestrator keypair")

    # Trim the event log once, here. The table only grows, and a local install
    # that has been up for a year would otherwise carry every event it ever
    # recorded. See app/events.py for why this is by age alone.
    from app.events import prune_events

    prune_events(settings.event_retention_days)

    reconciler: asyncio.Task[None] | None = None
    if settings.reconcile_interval_seconds > 0:
        reconciler = asyncio.create_task(
            _reconcile_loop(settings.reconcile_interval_seconds)
        )
        logger.info(
            "Background reconciler every %ss", settings.reconcile_interval_seconds
        )

    logger.info("%s v%s ready.", settings.app_name, settings.app_version)
    yield

    if reconciler is not None:
        reconciler.cancel()
        with suppress(asyncio.CancelledError):
            await reconciler
    logger.info("Shutting down.")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# The React frontend (Vite dev server) is the only intended consumer, named by
# exact origin. `allow_credentials=True` is what makes the exactness matter: it
# permits the session cookie to travel, so every entry in this list is a page
# allowed to act as the signed-in user. There is no regex form (DECISIONS #45).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"])
def health(
    registry: EngineRegistry = Depends(get_engine_registry),
) -> dict[str, object]:
    """Liveness, plus whether the hypervisor underneath is actually usable.

    A control plane that answers "ok" while its engine is broken sends people
    debugging the wrong layer, so the engine's own view is reported here — one
    glance instead of a session with the logs.
    """
    payload: dict[str, object] = {"status": "ok", "service": settings.app_name}
    try:
        qemu = registry.get("qemu")
        engine_info = qemu.describe()
        support = engine_info.get("support")
        payload["engine"] = {
            "name": engine_info.get("name"),
            "available": engine_info.get("available"),
            "version": _qemu_version(),
            "accel": engine_info.get("acceleration"),
            "accel_available": engine_info.get("accelerated"),
            "base_image_present": engine_info.get("base_image_present"),
            # Version *and* capabilities. The version alone cannot say whether
            # this build can give a guest a TPM or boot UEFI under the
            # accelerator in use, and those are the answers people need.
            "support": support,
        }
        if not engine_info.get("available"):
            payload["status"] = "degraded"
        # An out-of-range or capability-limited build is worth saying out loud,
        # but it is not degradation: everything this backend does today still
        # works. Surfaced as its own field so the dashboard can warn without
        # the health badge crying wolf.
        if isinstance(support, dict) and support.get("warnings"):
            payload["warnings"] = support["warnings"]
    except Exception as exc:  # noqa: BLE001 - liveness must answer regardless
        logger.warning("Health: engine probe failed: %s", exc)
        payload["status"] = "degraded"
        payload["engine"] = {"available": False, "error": str(exc)}
    return payload


def _qemu_version() -> str | None:
    """First line of ``qemu-system-x86_64 --version``, or None if unreadable."""
    import subprocess

    try:
        proc = subprocess.run(
            [settings.qemu_system_binary, "--version"],
            capture_output=True, text=True, timeout=settings.cli_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").splitlines()[0].strip() or None if proc.returncode == 0 else None


@app.get("/diagnostics", tags=["system"])
def diagnostics(
    request_settings: Settings = Depends(get_settings),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> dict[str, object]:
    """Host-side facts a client cannot see for itself.

    This exists for ``iaas doctor``. Everything it reports is a property of the
    machine the *backend* runs on — whether the instance store is writable, how
    much room is left on that volume, whether the orchestrator's keypair exists
    — and a client that guessed at them by looking at its own filesystem would
    be answering about the wrong host the moment the two are not the same.

    Facts only, deliberately: no pass/fail verdicts and no remedies. What counts
    as healthy is a judgement for the caller, and baking the CLI's opinion into
    an HTTP endpoint would make every other client inherit it.

    **Scope is deliberately the minimum ``doctor`` reads.** There is no
    authentication anywhere in this API, so everything here is readable by
    anything that can reach the port. No key contents (the private key is never
    read at all, and the public half belongs to ``GET /ssh-key``, which exists
    for callers who actually want it), no configuration dump, and no path that
    is not already reported by ``/engines`` or ``/ssh-key`` — each of the three
    paths below is one ``doctor`` prints, because a remedy that cannot say
    *where* it looked is not a remedy.
    """
    import platform
    import shutil

    payload: dict[str, object] = {
        "api": {"version": settings.app_version},
        "python": platform.python_version(),
    }

    try:
        engine_info = registry.get("qemu").describe()
        payload["engine"] = {
            "available": engine_info.get("available"),
            "version": _qemu_version(),
            "accel": engine_info.get("acceleration"),
            "accel_available": engine_info.get("accelerated"),
            "base_image": engine_info.get("base_image"),
            "base_image_present": engine_info.get("base_image_present"),
            # Facts, per this endpoint's contract: which build, whether it sits
            # in the tested range, and what it can do. `doctor` decides what to
            # call healthy — the verdict is not baked in here.
            "support": engine_info.get("support"),
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must answer regardless
        logger.warning("Diagnostics: engine probe failed: %s", exc)
        payload["engine"] = {"available": False, "error": str(exc)}

    # The instance store. Reported for the *deepest existing* ancestor when the
    # directory has not been created yet, because on a fresh install the answer
    # the user needs is about the volume it will live on.
    store = Path(request_settings.qemu_dir).expanduser() / "instances"
    probe = store
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    store_info: dict[str, object] = {
        "path": str(store),
        "exists": store.exists(),
        "writable": os.access(probe, os.W_OK),
        "free_bytes": None,
    }
    try:
        store_info["free_bytes"] = shutil.disk_usage(probe).free
    except OSError as exc:
        store_info["error"] = str(exc)
    payload["instance_store"] = store_info

    # Whether a keypair exists and where — not what is in it. get_public_key is
    # what forces generation on first access, which is the right behaviour for a
    # diagnostic ("run doctor" should leave you with a keypair, not a lecture
    # about not having one), so it is called and its result discarded.
    from app.ssh_keys import SSHKeyError, get_private_key_path, get_public_key

    try:
        get_public_key(request_settings)
        payload["ssh_key"] = {
            "present": True,
            "private_key_path": str(get_private_key_path(request_settings)),
            "error": None,
        }
    except SSHKeyError as exc:
        payload["ssh_key"] = {"present": False, "private_key_path": None, "error": str(exc)}

    return payload


@app.get("/settings", tags=["system"])
def effective_settings(
    request_settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """The configuration this backend is actually running with.

    Read-only, and every entry carries the environment variable that controls
    it. That pairing is the point: a settings screen that shows a value without
    saying how to change it has told the user only half of what they came for.

    Nothing here is editable at runtime, and that is a real constraint rather
    than unfinished work. Settings are read once and cached (``get_settings``
    is ``lru_cache``d), several are consumed at startup, and the ones naming
    directories are load-bearing for VMs already on disk — moving
    ``IAAS_QEMU_DIR`` under a running instance would strand its disk. Restart
    the backend after changing any of them.

    Separate from ``/diagnostics`` deliberately. That endpoint is scoped to the
    facts ``doctor`` needs and explicitly carries no configuration dump; this
    one is the configuration dump. Merging them would put the whole config
    behind an endpoint documented as the minimum.
    """
    from app.engines.images import base_image_path

    def entry(
        key: str, label: str, value: object, env: str, kind: str = "text"
    ) -> dict[str, object]:
        """One setting. ``kind`` is a presentation hint, not a type.

        Only "path" exists, and only because a client cannot tell one from a
        string by looking: a path wants to be monospaced, truncated from the
        end and copyable, and the dashboard renders every path that way. The
        alternative was for the UI to guess from the group's display name,
        which would have made a heading into an API contract.
        """
        return {"key": key, "label": label, "value": value, "env": env, "kind": kind}

    def path_entry(key: str, label: str, value: object, env: str) -> dict[str, object]:
        return entry(key, label, value, env, kind="path")

    s = request_settings
    return {
        "groups": [
            {
                "name": "Paths",
                "description": (
                    "Where this backend keeps its state. Every directory below "
                    "defaults to a subdirectory of the state directory."
                ),
                "settings": [
                    path_entry("state_dir", "State directory", s.state_dir, "IAAS_STATE_DIR"),
                    # The resolved file, not the URL. This is the one path a
                    # user is most likely to go looking for — it used to be
                    # wherever the backend happened to be started from — so it
                    # is shown as somewhere you can actually navigate to.
                    path_entry(
                        "database", "Database",
                        str(s.database_path or s.database_url), "IAAS_DATABASE_URL",
                    ),
                    path_entry("qemu_dir", "Instance store", s.qemu_dir, "IAAS_QEMU_DIR"),
                    path_entry("iso_dir", "ISO directory", s.iso_dir, "IAAS_ISO_DIR"),
                    path_entry(
                        "ssh_key_dir", "Key directory", s.ssh_key_dir, "IAAS_SSH_KEY_DIR"
                    ),
                    path_entry(
                        "cloud_init_dir", "Cloud-init directory",
                        s.cloud_init_dir, "IAAS_CLOUD_INIT_DIR",
                    ),
                ],
            },
            {
                "name": "Capacity",
                "description": (
                    "How much of the host instances may be given. Held back "
                    "memory protects the host from being driven into swap."
                ),
                "settings": [
                    entry(
                        "host_reserve_memory_mb", "Host memory reserve (MB)",
                        s.host_reserve_memory_bytes // (1024**2),
                        "IAAS_HOST_RESERVE_MEMORY_BYTES",
                    ),
                    entry(
                        "cpu_oversubscribe_factor", "vCPU oversubscribe factor",
                        s.cpu_oversubscribe_factor, "IAAS_CPU_OVERSUBSCRIBE_FACTOR",
                    ),
                    entry(
                        "min_instance_memory_mb", "Minimum instance memory (MB)",
                        s.min_instance_memory_mb, "IAAS_MIN_INSTANCE_MEMORY_MB",
                    ),
                    entry(
                        "min_instance_disk_gb", "Minimum instance disk (GB)",
                        s.min_instance_disk_gb, "IAAS_MIN_INSTANCE_DISK_GB",
                    ),
                    # Windows will not install below these, so they are shown
                    # beside the Linux floors rather than hidden — a user
                    # wondering why a 1 GB Windows launch was refused should be
                    # able to find the number that refused it.
                    entry(
                        "windows_min_memory_mb", "Minimum Windows memory (MB)",
                        s.windows_min_memory_mb, "IAAS_WINDOWS_MIN_MEMORY_MB",
                    ),
                    entry(
                        "windows_min_disk_gb", "Minimum Windows disk (GB)",
                        s.windows_min_disk_gb, "IAAS_WINDOWS_MIN_DISK_GB",
                    ),
                ],
            },
            {
                "name": "QEMU",
                "description": "The hypervisor and the image every instance starts from.",
                "settings": [
                    entry(
                        "qemu_system_binary", "System binary",
                        s.qemu_system_binary, "IAAS_QEMU_SYSTEM_BINARY",
                    ),
                    entry("qemu_img_binary", "Image tool", s.qemu_img_binary, "IAAS_QEMU_IMG_BINARY"),
                    entry(
                        "qemu_cpu_model", "Guest CPU model",
                        s.qemu_cpu_model or "(derived from the accelerator and guest OS)",
                        "IAAS_QEMU_CPU_MODEL",
                    ),
                    entry(
                        "qemu_version_min", "Minimum QEMU version",
                        s.qemu_version_min, "IAAS_QEMU_VERSION_MIN",
                    ),
                    entry(
                        "qemu_version_max_tested", "Highest tested QEMU version",
                        s.qemu_version_max_tested, "IAAS_QEMU_VERSION_MAX_TESTED",
                    ),
                    path_entry(
                        "base_image", "Base image",
                        str(base_image_path(s)), "IAAS_QEMU_BASE_IMAGE_NAME",
                    ),
                ],
            },
            {
                "name": "Timing",
                "description": "How long the backend waits for guests and hypervisor calls.",
                "settings": [
                    entry(
                        "qemu_boot_timeout_seconds", "Boot timeout (s)",
                        s.qemu_boot_timeout_seconds, "IAAS_QEMU_BOOT_TIMEOUT_SECONDS",
                    ),
                    entry(
                        "qemu_shutdown_timeout_seconds", "Shutdown grace (s)",
                        s.qemu_shutdown_timeout_seconds, "IAAS_QEMU_SHUTDOWN_TIMEOUT_SECONDS",
                    ),
                    entry(
                        "reconcile_interval_seconds", "Reconcile interval (s)",
                        s.reconcile_interval_seconds, "IAAS_RECONCILE_INTERVAL_SECONDS",
                    ),
                    entry(
                        "degraded_after_seconds", "Degraded after (s)",
                        s.degraded_after_seconds, "IAAS_DEGRADED_AFTER_SECONDS",
                    ),
                    entry(
                        "event_retention_days", "Event retention (days)",
                        s.event_retention_days, "IAAS_EVENT_RETENTION_DAYS",
                    ),
                ],
            },
            {
                "name": "Guests",
                "description": "Defaults baked into every instance at launch.",
                "settings": [
                    entry("default_vm_user", "Default user", s.default_vm_user, "IAAS_DEFAULT_VM_USER"),
                    entry(
                        "qemu_guest_nameservers", "Guest nameservers",
                        ", ".join(s.qemu_guest_nameservers) or "(DHCP/SLIRP default)",
                        "IAAS_QEMU_GUEST_NAMESERVERS",
                    ),
                    entry(
                        "ssh_port_range", "SSH port pool",
                        f"{s.qemu_ssh_port_min}-{s.qemu_ssh_port_max}",
                        "IAAS_QEMU_SSH_PORT_MIN / _MAX",
                    ),
                    entry(
                        "vnc_port_range", "VNC port pool",
                        f"{s.qemu_vnc_port_min}-{s.qemu_vnc_port_max}",
                        "IAAS_QEMU_VNC_PORT_MIN / _MAX",
                    ),
                ],
            },
        ],
    }


@app.get("/engines", tags=["system"])
def list_engines(
    registry: EngineRegistry = Depends(get_engine_registry),
) -> list[dict[str, object]]:
    """Expose the compute-engine catalog and each driver's live status.

    The dashboard's engine selector reads this instead of hardcoding names, and
    it is where QEMU reports whether it got hardware acceleration (WHPX) or fell
    back to TCG software emulation.
    """
    catalog: list[dict[str, object]] = []
    for name in registry.names():
        try:
            catalog.append(registry.get(name).describe())
        except Exception as exc:  # noqa: BLE001 - a broken driver must not 500 the page
            logger.warning("Engine '%s' failed to describe itself: %s", name, exc)
            catalog.append({"name": name, "available": False, "error": str(exc)})
    return catalog


@app.get("/host/capacity", tags=["system"])
def host_capacity(
    session: Session = Depends(get_session),
    request_settings: Settings = Depends(get_settings),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> dict[str, object]:
    """What this host can still give a new instance.

    Reports totals, what is already committed, and what remains allocatable —
    the launch form needs all three to explain a limit rather than just enforce
    one. Cached briefly so dashboard polling doesn't re-probe psutil.
    """
    from app.host_capacity import get_capacity

    capacity = get_capacity(session, request_settings)
    payload = capacity.as_dict()

    # The accelerator probe belongs to the engine, not to psutil.
    try:
        qemu = registry.get("qemu")
        payload["accel"] = qemu.accel()  # type: ignore[attr-defined]
        # Hardware-backed, whichever platform's accelerator that is — asking
        # "== whpx" here reported every KVM host as unaccelerated.
        from app.engines.qemu import HARDWARE_ACCELS

        payload["accel_available"] = payload["accel"] in HARDWARE_ACCELS
    except Exception as exc:  # noqa: BLE001 - capacity must survive a broken engine
        logger.warning("Could not probe the accelerator: %s", exc)
        payload["accel"], payload["accel_available"] = None, False

    return payload


@app.get("/isos", tags=["system"])
def list_boot_isos(
    request_settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    """Boot media available in the configured ISO directory.

    Files are placed there by hand; this is a read-only listing so the launch
    modal can offer real choices instead of a free-text filename field.
    """
    from app.isos import list_isos

    return [
        {
            "name": iso.name,
            "size_bytes": iso.size_bytes,
            "modified_at": iso.modified_at.isoformat(),
        }
        for iso in list_isos(request_settings)
    ]


@app.get("/flavors", tags=["system"])
def list_flavors() -> dict[str, dict[str, int]]:
    """Sizing presets — starting points the launch form can fill in.

    These are defaults, not limits: a user may edit the numbers afterwards, and
    the real bound is host capacity.

    Numbers only, deliberately. This used to return each size twice — once as
    an integer and once pre-formatted for display — which was two spellings of
    one fact with nothing enforcing they agreed. Presentation belongs to the
    client, which already formats sizes elsewhere.
    """
    return {
        name: {"cpus": spec.cpus, "memory_mb": spec.memory_mb, "disk_gb": spec.disk_gb}
        for name, spec in settings.flavors.items()
    }


@app.get("/ssh-key", tags=["system"])
def ssh_key() -> dict[str, str]:
    """Return the orchestrator's public key so the UI/user can inspect it.

    The keypair is generated on first access. ``private_key_path`` is what the
    dashboard puts behind ``ssh -i`` — VMs only ever trust the orchestrator's
    key, so a command without it fails with "Permission denied (publickey)"
    unless the user happens to have that key loaded in an agent. Only the
    *public* key contents are returned.
    """
    from app.ssh_keys import SSHKeyError, get_private_key_path, get_public_key

    try:
        private_key_path = str(get_private_key_path(settings))
        return {
            "public_key": get_public_key(settings),
            "private_key_path": private_key_path,
            # Retained under its original name for pre-existing consumers.
            "key_path": private_key_path,
            "ssh_user": settings.default_vm_user,
        }
    except SSHKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


from app.routers import (  # noqa: E402
    events, images, instances, keypairs, networks, projects, snapshots, volume_snapshots,
    volumes,
)

app.include_router(projects.router)
app.include_router(instances.router)
app.include_router(images.router)
app.include_router(keypairs.router)
app.include_router(snapshots.router)
app.include_router(events.router)
app.include_router(volumes.router)
app.include_router(volume_snapshots.router)
app.include_router(networks.router)
