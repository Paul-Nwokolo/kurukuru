"""
Application configuration.

All values can be overridden via environment variables (prefix: IAAS_)
or a local `.env` file, e.g.:

    IAAS_DATABASE_URL=sqlite:////var/lib/local-iaas/iaas.db
    IAAS_CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Root of everything this tool keeps on disk. One dotfile directory in $HOME,
#: which is the convention on Windows and macOS and *a* convention on Linux —
#: where the XDG Base Directory spec would instead put state under
#: ``$XDG_DATA_HOME`` (``~/.local/share/local-iaas``) and configuration under
#: ``$XDG_CONFIG_HOME``. Changing the default would move every existing
#: install's VMs and keys out from under it, so the default stays and
#: ``IAAS_STATE_DIR`` is the supported way to relocate the lot:
#:
#:     IAAS_STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/local-iaas"
#:
#: Whether Linux should *default* to that is a policy question for the Linux
#: validation phase; the mechanism is here either way.
DEFAULT_STATE_DIR = "~/.local-iaas"


class FlavorSpec(BaseModel):
    """A named starting point for instance sizing.

    Numbers only. These are defaults a user can edit, arithmetic against host
    capacity needs integers, and every consumer formats them for its own
    purpose — the engine for a QEMU command line, the dashboard for a label.
    Carrying pre-formatted strings here once meant two spellings of the same
    size that nothing kept in agreement.
    """

    cpus: int
    memory_mb: int
    disk_gb: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IAAS_",
        env_file=".env",
        env_file_encoding="utf-8-sig",  # tolerates BOM; plain UTF-8 still works
        extra="ignore",                 # unknown .env keys warn-by-omission, never crash the server
    )

    # --- General ---
    app_name: str = "Local IaaS Orchestrator"
    app_version: str = "0.1.0"
    debug: bool = False

    # --- Database ---
    # Rooted in the state directory like every other path, and for the same
    # reason. This used to be ``sqlite:///./iaas.db`` — relative to the
    # *current working directory*, so the backend opened a different database
    # depending on where it was started from. Started from `backend/` you got
    # your VMs; started from the repo root you got an empty dashboard and a
    # freshly created second database, with the real rows still sitting in the
    # first one. Nothing was lost, but nothing said so either, and "my
    # instances disappeared" is indistinguishable from data loss while it is
    # happening.
    #
    # Left at this default it follows ``state_dir`` (see ``_apply_state_dir``).
    # Set IAAS_DATABASE_URL to put it anywhere, including a CWD-relative path
    # if that is genuinely what you want.
    database_url: str = f"sqlite:///{DEFAULT_STATE_DIR}/iaas.db"

    # --- CORS (Vite dev server defaults) ---
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    # Optional regex of additional allowed origins. **Off by default, and
    # development-only.** Vite takes the next free port when 5173 is busy, and
    # the dashboard then fails every request with a CORS error that names none
    # of that; this exists so a developer can opt out of the annoyance for a
    # session, e.g.
    #
    #     IAAS_CORS_ORIGIN_REGEX='http://(localhost|127\.0\.0\.1)(:\d+)?'
    #
    # It is deliberately not the default. CORS is a security control, and the
    # right fix for a port collision is to free the port — widening a default
    # for the convenience of the machine that hit the problem is how a
    # development shortcut becomes a shipped posture.
    cors_origin_regex: str = ""

    # --- Compute engine ---
    # Timeout for short-lived hypervisor tool calls (qemu-img, --version probes).
    # Boot waits use qemu_boot_timeout_seconds instead; these commands should
    # return in milliseconds and a hang means something is wrong.
    #
    # The Multipass settings that used to live here (IAAS_MULTIPASS_BINARY,
    # IAAS_LAUNCH_TIMEOUT_SECONDS) were removed with that engine. Leaving them
    # in a .env is harmless — model_config sets extra="ignore".
    cli_timeout_seconds: int = 30

    # --- Reconciliation ---
    # A hypervisor often reports a VM Running a few seconds before it publishes
    # the guest's address, so the post-launch sync keeps sampling until an IP
    # appears rather than pinning "Running with no IP" onto the row.
    post_launch_ip_timeout_seconds: int = 60
    post_launch_poll_seconds: float = 2.0
    # Background reconcile cadence. The DB is desired state and the hypervisor
    # is the truth; without a recurring pass, any transient miss (or any change
    # made outside the API) persists until someone clicks Refresh. 0 disables.
    reconcile_interval_seconds: int = 30
    # A VM that should have an address but still hasn't published one after
    # this long is not healthy, and saying so beats a green dot that lies.
    degraded_after_seconds: int = 90

    # --- Event log (Phase 11) ---
    # How long an instance's history is kept. Pruned once at startup, by age
    # only: a Terminated instance keeps its events until they age out, because
    # "what happened to the VM that is now gone" is the question the log exists
    # to answer. 0 disables pruning and keeps everything.
    event_retention_days: int = 90

    # --- On-disk layout ---
    # Everything below defaults to a subdirectory of this. Set IAAS_STATE_DIR to
    # move all of it at once (an XDG layout on Linux, or a different volume);
    # set any individual directory to place just that one. An explicit
    # directory always wins over the root — see _root_unset_dirs.
    state_dir: str = DEFAULT_STATE_DIR

    # --- Cloud-init & SSH access (Phase 4) ---
    default_vm_user: str = "iaas"       # non-root sudo user created on every VM
    # Orchestrator keypair location. "~" is expanded at use-time in ssh_keys.py.
    ssh_key_dir: str = f"{DEFAULT_STATE_DIR}/keys"
    # Per-instance cloud-init YAML is rendered here, then deleted after launch.
    cloud_init_dir: str = f"{DEFAULT_STATE_DIR}/cloud-init"
    ssh_keygen_binary: str = "ssh-keygen"  # Windows 10+ ships OpenSSH on PATH

    # --- QEMU engine (Phase 5) ---
    # Working tree: <qemu_dir>/base-images/<image>  and  <qemu_dir>/instances/<name>/
    qemu_dir: str = f"{DEFAULT_STATE_DIR}/qemu"
    qemu_system_binary: str = "qemu-system-x86_64"
    qemu_img_binary: str = "qemu-img"
    # Official Ubuntu 24.04 LTS cloud image (qcow2, cloud-init preinstalled).
    qemu_base_image_url: str = (
        "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
    )
    qemu_base_image_name: str = "noble-server-cloudimg-amd64.img"
    # Guest CPU model. Empty means "pick one that works for the accelerator in
    # use" — see QemuEngine.cpu_model(); set this to override.
    qemu_cpu_model: str = ""
    qemu_download_timeout_seconds: int = 1800  # ~600 MB over a slow link
    # Resolvers handed to QEMU guests via the NoCloud network-config. QEMU's
    # user-mode DNS proxy (10.0.2.3) is unreliable on Windows — it NXDOMAINs
    # every lookup while NAT itself works — so guests are pointed at real
    # resolvers instead. Empty list = keep whatever DHCP/SLIRP supplies.
    qemu_guest_nameservers: list[str] = ["1.1.1.1", "8.8.8.8"]
    # How long provision/start wait for the guest's SSH port to answer.
    qemu_boot_timeout_seconds: int = 600
    # How long a snapshot create/restore may take. Independent of the boot
    # timeout: these are disk operations whose cost scales with how much the
    # overlay has diverged, not with how long a guest takes to start.
    qemu_snapshot_timeout_seconds: int = 900
    # Grace period for system_powerdown before the process is killed.
    qemu_shutdown_timeout_seconds: int = 90
    # Host port pools. Ports are bind-probed and then pinned to the instance for
    # its whole life so "Copy SSH" keeps working across stop/start.
    qemu_ssh_port_min: int = 2200
    qemu_ssh_port_max: int = 2299
    qemu_qmp_port_min: int = 4400
    qemu_qmp_port_max: int = 4499
    # VNC display N listens on 5900+N; the pool is expressed in real port numbers.
    qemu_vnc_port_min: int = 5900
    qemu_vnc_port_max: int = 5999

    # --- Image import from a URL (Phase 13) ---
    # A hard ceiling on a fetched image. Downloading an arbitrary URL onto the
    # backend's disk is the one place a caller can consume unbounded space, and
    # a cloud image that is genuinely larger than this is unusual enough to be
    # worth an explicit override.
    image_fetch_max_bytes: int = 16 * 1024**3  # 16 GB
    image_fetch_timeout_seconds: int = 3600

    # --- Boot media & images (Phase 6) ---
    # Drop .iso files here by hand; there is no upload endpoint (a local-first
    # tool shouldn't push multi-GB installers through the browser).
    iso_dir: str = f"{DEFAULT_STATE_DIR}/isos"

    # --- Sizing presets (one-click starting points, not limits) ---
    flavors: dict[str, FlavorSpec] = {
        "small": FlavorSpec(cpus=1, memory_mb=1024, disk_gb=5),
        "medium": FlavorSpec(cpus=2, memory_mb=2048, disk_gb=10),
        "large": FlavorSpec(cpus=4, memory_mb=4096, disk_gb=20),
    }

    # --- Host capacity ---
    # Held back for the host OS so a launch can't drive the machine into swap.
    host_reserve_memory_bytes: int = 2 * 1024**3
    # vCPUs timeshare, so handing out more than the host has is normal and safe;
    # a single VM is still capped at the real core count.
    cpu_oversubscribe_factor: float = 2.0
    # Dashboard polling would otherwise call psutil several times a second.
    capacity_cache_seconds: float = 3.0
    # Floors below which a guest is not worth booting.
    min_instance_cpus: int = 1
    min_instance_memory_mb: int = 512
    min_instance_disk_gb: int = 1

    #: Directory settings that follow ``state_dir``, and the leaf each takes
    #: under it. Kept as data so adding a directory cannot forget to re-root it.
    _ROOTED_DIRS = {
        "qemu_dir": "qemu",
        "ssh_key_dir": "keys",
        "cloud_init_dir": "cloud-init",
        "iso_dir": "isos",
    }

    #: The database's leaf under ``state_dir``. Separate from ``_ROOTED_DIRS``
    #: because it is a URL rather than a bare path, so it needs its own
    #: spelling on both sides of the comparison.
    _DATABASE_LEAF = "iaas.db"

    @model_validator(mode="after")
    def _apply_state_dir(self) -> Settings:
        """Re-root the paths that were left at their defaults.

        Only those. A path someone set explicitly is a decision, and moving it
        because ``state_dir`` also changed would quietly relocate the one path
        they cared enough to name — so an explicit setting always wins, and the
        two can be combined (state elsewhere, ISOs on a big disk) without
        either surprising the other.

        With ``state_dir`` at its default this rewrites every value to exactly
        what it already was, which is what makes the change invisible to
        existing installs.
        """
        root = self.state_dir.rstrip("/\\")
        for field, leaf in self._ROOTED_DIRS.items():
            if getattr(self, field) == f"{DEFAULT_STATE_DIR}/{leaf}":
                object.__setattr__(self, field, f"{root}/{leaf}")
        if self.database_url == f"sqlite:///{DEFAULT_STATE_DIR}/{self._DATABASE_LEAF}":
            object.__setattr__(
                self, "database_url", f"sqlite:///{root}/{self._DATABASE_LEAF}"
            )
        return self

    @property
    def database_path(self) -> Path | None:
        """The file behind ``database_url``, with ``~`` expanded.

        None for anything that is not a SQLite file — an in-memory database, or
        a real server URL if this ever grows one. SQLAlchemy does not expand
        ``~`` (it would open a directory literally named ``~``), so every
        consumer of the URL has to go through here or through
        :attr:`resolved_database_url`.
        """
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        raw = self.database_url[len(prefix):]
        if not raw or raw == ":memory:":
            return None
        return Path(raw).expanduser()

    @property
    def resolved_database_url(self) -> str:
        """``database_url`` with ``~`` expanded — what actually opens the file.

        A relative path is left relative. Anyone who sets
        ``IAAS_DATABASE_URL=sqlite:///./iaas.db`` has asked for the
        CWD-relative behaviour explicitly, and honouring that is the difference
        between a default that was wrong and a setting that is not overridable.
        """
        path = self.database_path
        return f"sqlite:///{path.as_posix()}" if path is not None else self.database_url

    @property
    def default_database_path(self) -> Path | None:
        """Where the database lands when ``database_url`` was left alone.

        Used to tell "this install is on the default layout" from "someone
        named a path", which is what gates the one-time relocation in
        ``app.database``. Comparing against this rather than against a flag
        keeps the rule the same one ``_apply_state_dir`` uses: explicit wins.
        """
        root = self.state_dir.rstrip("/\\")
        return Path(f"{root}/{self._DATABASE_LEAF}").expanduser()


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor — safe to use as a FastAPI dependency."""
    return Settings()
