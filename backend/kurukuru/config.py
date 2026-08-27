"""
Application configuration.

All values can be overridden via environment variables (prefix: KURUKURU_)
or a local `.env` file, e.g.:

    KURUKURU_DATABASE_URL=sqlite:////var/lib/kurukuru/kurukuru.db
    KURUKURU_CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]

The prefix was ``IAAS_`` before Phase 16. Those names still work for one
release — see :func:`apply_legacy_env`, which is where the deprecation lives.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kurukuru.product import (
    DATABASE_LEAF,
    VERSION,
    ENV_PREFIX,
    PRODUCT_NAME,
    STATE_DIR,
    apply_legacy_env,
)

logger = logging.getLogger("kurukuru.config")

#: Root of everything this tool keeps on disk. One dotfile directory in $HOME,
#: which is the convention on Windows and macOS and *a* convention on Linux —
#: where the XDG Base Directory spec would instead put state under
#: ``$XDG_DATA_HOME`` (``~/.local/share/kurukuru``) and configuration under
#: ``$XDG_CONFIG_HOME``. Changing the default would move every existing
#: install's VMs and keys out from under it, so the default stays and
#: ``KURUKURU_STATE_DIR`` is the supported way to relocate the lot:
#:
#:     KURUKURU_STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kurukuru"
#:
#: Whether Linux should *default* to that is a policy question for the Linux
#: validation phase; the mechanism is here either way.
#:
#: This moved from ``~/.local-iaas`` in Phase 16. It is the one default whose
#: change *cannot* be made invisible, so it is not made invisible: an existing
#: tree is detected and moved once, loudly, by :mod:`kurukuru.state_migration`.
DEFAULT_STATE_DIR = STATE_DIR


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
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8-sig",  # tolerates BOM; plain UTF-8 still works
        extra="ignore",                 # unknown .env keys warn-by-omission, never crash the server
    )

    # --- General ---
    app_name: str = PRODUCT_NAME
    app_version: str = VERSION
    debug: bool = False

    # --- Database ---
    # Rooted in the state directory like every other path, and for the same
    # reason. This used to be ``sqlite:///./kurukuru.db`` — relative to the
    # *current working directory*, so the backend opened a different database
    # depending on where it was started from. Started from `backend/` you got
    # your VMs; started from the repo root you got an empty dashboard and a
    # freshly created second database, with the real rows still sitting in the
    # first one. Nothing was lost, but nothing said so either, and "my
    # instances disappeared" is indistinguishable from data loss while it is
    # happening.
    #
    # Left at this default it follows ``state_dir`` (see ``_apply_state_dir``).
    # Set KURUKURU_DATABASE_URL to put it anywhere, including a CWD-relative path
    # if that is genuinely what you want.
    database_url: str = f"sqlite:///{DEFAULT_STATE_DIR}/{DATABASE_LEAF}"

    # --- Serving ---
    # Loopback, and changing it is a deliberate act with a warning attached.
    # Everything this tool exposes is unauthenticated at the network layer or
    # protected only by a session cookie over plain HTTP: VM consoles, the SSH
    # forwards, the API. Binding a wildcard puts all of it on the LAN.
    host: str = "127.0.0.1"
    # Not 8000. That port is contended enough to be taken on a developer's
    # machine most of the time, and the failure it produces — a backend that
    # exits, or worse, a dashboard talking to somebody else's server — is not
    # worth inheriting for familiarity. 7842 is unassigned by IANA and sits
    # below the ranges Windows and Hyper-V reserve for themselves.
    #
    # A fixed default cannot be guaranteed bindable on Windows in any case: the
    # dynamic port range starts at 1024 on a default install and Hyper-V
    # reserves blocks that move across reboots, so a bind can fail with a
    # *permission* error for a port nothing is listening on. That is why the
    # message on failure matters more than the number — see
    # ``kurukuru.cli.commands_system.serve``.
    port: int = 7842
    # Where the built dashboard is. Empty means "look in the two places it
    # normally is" — beside the package in a frozen build, or frontend/dist in
    # a checkout. Serving no dashboard is a supported state: it is what a
    # developer running against the Vite dev server wants.
    dashboard_dir: str = ""

    # --- CORS ---
    # Empty, and that is the shipped configuration. The dashboard and the API
    # are the same origin now, so there is no cross-origin request to permit
    # and the correct list is the empty one.
    #
    # Development is the exception and is stated explicitly rather than left as
    # a default that production then inherits: running the Vite dev server on
    # 5173 against the backend on another port *is* cross-origin, and needs
    #
    #   KURUKURU_CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]
    #
    # which backend/.env.example carries, commented, for exactly that.
    #
    # There is deliberately no origin *regex* escape hatch. One existed for a
    # single session's convenience — Vite takes the next free port when 5173 is
    # busy, and the documented example matched any port on localhost. With no
    # authentication that cost little: the API was open, so CORS was not the
    # thing protecting it. A session cookie changes that completely. Every
    # localhost port is the *same site*, so the cookie is sent to whatever is
    # listening there, and a permissive origin regex plus allow_credentials
    # turns any page served from any local port into a fully authenticated API
    # client. That is precisely the "malicious page on the same machine" Phase
    # 15 exists to shut out, so the hatch is gone rather than narrowed. The fix
    # for a port collision is still to free the port (DECISIONS #45).
    cors_origins: list[str] = []

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
    # Everything below defaults to a subdirectory of this. Set KURUKURU_STATE_DIR to
    # move all of it at once (an XDG layout on Linux, or a different volume);
    # set any individual directory to place just that one. An explicit
    # directory always wins over the root — see _root_unset_dirs.
    state_dir: str = DEFAULT_STATE_DIR

    # --- Authentication (Phase 15) ---
    # Where the CLI keeps its API token. Under state_dir with everything else,
    # and locked to the owning OS user at write time — see kurukuru.fs_permissions
    # for what that is actually worth on each platform.
    #
    # This is a convenience, not a trust boundary: the token in it is an
    # ordinary API token, revocable like any other. Anyone who can read it could
    # also read the SSH private key and every VM disk sitting beside it.
    auth_token_file: str = f"{DEFAULT_STATE_DIR}/cli-token"

    # --- Database backups (Phase 14) ---
    # A copy of the database is taken automatically immediately before an
    # additive migration changes its shape, and never otherwise — an ordinary
    # startup on a converged database writes nothing, or every restart would
    # leave an identical copy behind.
    #
    # Under state_dir rather than beside the database, so that "the database and
    # everything that protects it" is not one `rm kurukuru.db*` away from being
    # gone, and so a backup is never mistaken for a live sidecar.
    db_backup_dir: str = f"{DEFAULT_STATE_DIR}/backups"
    # How many automatic backups to keep. Older ones are pruned oldest-first
    # after a successful new one. 0 disables pruning and keeps everything.
    db_backup_retention: int = 5

    # --- Cloud-init & SSH access (Phase 4) ---
    # The non-root sudo user created inside every cloud-init guest.
    #
    # **Deliberately not renamed in Phase 16.** This is not a product string:
    # it is an identity written into a guest's ``/etc/passwd`` at provision
    # time, and the row does not record which name it got — ``Instance.ssh_user``
    # returns *this setting*, live. Changing it would therefore rewrite the
    # "Copy SSH" command of every instance that already exists into a username
    # its guest has never heard of, which fails as "Permission denied
    # (publickey)" and looks like a broken key rather than a wrong user.
    #
    # Renaming it is a two-step change: persist the user on the instance row,
    # backfill existing rows with "iaas" (which is correct for all of them),
    # and only then move the default. See docs/DECISIONS.md.
    default_vm_user: str = "iaas"
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

    # --- Known-good QEMU range (Phase 13) ---
    # Version *awareness*, not version chasing. Nothing here downloads,
    # upgrades or replaces a binary: for packaging we pin and bundle a tested
    # QEMU, and an application that rewrites system binaries is a security and
    # support problem we do not want. These two values only decide whether the
    # dashboard says "tested" or "outside the tested range".
    #
    # The minimum is a real floor: q35 defaults, `-accel` syntax and the device
    # names used here all predate it comfortably, but older builds start
    # missing things in ways that surface as confusing device errors.
    qemu_version_min: str = "8.0.0"
    # The newest version actually exercised against this code. Bump it when a
    # release has been through the suite and a live launch — not when one ships.
    #
    # 11.1.0 here is the development snapshot `v11.1.0-12130-ge470268ff4`, which
    # has been through the suite and a live launch on the Windows host. It is
    # deliberately recorded as a version anyway: `status` reports "prerelease"
    # from the build string rather than this number, so the range stays a
    # statement about released versions while the snapshot still gets flagged
    # as uninstallable by anyone else. See DECISIONS "QEMU version awareness".
    qemu_version_max_tested: str = "11.1.0"
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
        # A Windows guest sized to actually finish installing and still have
        # room afterwards. Every Linux preset is below the Windows floor, so
        # without this a Windows launch has nothing sensible to start from.
        "windows": FlavorSpec(cpus=2, memory_mb=4096, disk_gb=40),
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

    # --- Windows guests (Phase 13) ---
    # Windows will not install below these, so a request under them is not a
    # slow VM, it is a failed install discovered forty minutes in. Set to admit
    # the *smallest* supported target rather than the comfortable one: Windows
    # Server Core and Windows 10 both fit here, and the recommended starting
    # point is the "windows" preset below, which is roomier.
    #
    # Windows 11 asks for more again (4 GB / 64 GB) — and cannot be installed
    # here at all, because TPM 2.0 emulation does not exist on a Windows host.
    # See docs/DECISIONS.md.
    windows_min_cpus: int = 2
    windows_min_memory_mb: int = 2048
    windows_min_disk_gb: int = 32

    #: Directory settings that follow ``state_dir``, and the leaf each takes
    #: under it. Kept as data so adding a directory cannot forget to re-root it.
    _ROOTED_DIRS = {
        "qemu_dir": "qemu",
        "ssh_key_dir": "keys",
        "cloud_init_dir": "cloud-init",
        "iso_dir": "isos",
        "db_backup_dir": "backups",
    }

    #: The database's leaf under ``state_dir``. Separate from ``_ROOTED_DIRS``
    #: because it is a URL rather than a bare path, so it needs its own
    #: spelling on both sides of the comparison.
    _DATABASE_LEAF = DATABASE_LEAF

    #: Same treatment for the CLI's token file: a path, not a directory, so it
    #: needs its own line in the re-rooting below.
    _AUTH_TOKEN_LEAF = "cli-token"

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
        if self.auth_token_file == f"{DEFAULT_STATE_DIR}/{self._AUTH_TOKEN_LEAF}":
            object.__setattr__(
                self, "auth_token_file", f"{root}/{self._AUTH_TOKEN_LEAF}"
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
        ``KURUKURU_DATABASE_URL=sqlite:///./kurukuru.db`` has asked for the
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
        ``kurukuru.database``. Comparing against this rather than against a flag
        keeps the rule the same one ``_apply_state_dir`` uses: explicit wins.
        """
        root = self.state_dir.rstrip("/\\")
        return Path(f"{root}/{self._DATABASE_LEAF}").expanduser()


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor — safe to use as a FastAPI dependency.

    The legacy-prefix shim runs here rather than at import time so that it runs
    exactly once, on the same cache boundary as the settings it feeds, and so a
    test that manipulates the environment and clears this cache gets the shim
    applied to what it just set.
    """
    for old, new in apply_legacy_env():
        logger.warning(
            "%s is deprecated and will stop being read in a future release. "
            "Rename it to %s.", old, new,
        )
    return Settings()
