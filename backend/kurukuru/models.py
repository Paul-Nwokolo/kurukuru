"""
Domain models and API schemas.

SQLModel lets one class hierarchy serve both the ORM layer and the
Pydantic validation layer, so the DB schema and API contract can't drift.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import computed_field, field_validator
from sqlmodel import Field, SQLModel

# Multipass instance-name constraint (also a sane hostname rule):
# lowercase alphanumerics and hyphens, must start with a letter.
INSTANCE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

# Compute engine catalog. Declared here rather than in kurukuru.engines because the
# schema layer must not import the driver layer (kurukuru.engines imports this module
# for InstanceStatus); kurukuru/engines/__init__.py asserts the two stay in sync.
KNOWN_ENGINES = ("qemu",)
DEFAULT_ENGINE = "qemu"

# Engines that once existed and whose rows are still in the database. The column
# keeps their name for audit purposes, but nothing can be launched on them and
# no driver is loaded — so requests naming one get an explanation rather than a
# generic "unknown engine". See ROADMAP_v2.md for why Multipass was retired.
RETIRED_ENGINES = ("multipass",)

# Accelerator choices callers may request. "auto" lets the driver decide, which
# is the right default and the one a portable client should send: the hardware
# accelerator's *name* is a property of the host OS (whpx on Windows, kvm on
# Linux, hvf on macOS), so a caller naming one has to know which platform the
# backend runs on. The names are all accepted here rather than gated on the
# running platform — the driver resolves an unavailable request down to what
# the host actually offers, which keeps a request that was valid yesterday from
# 422-ing after the backend moved to another machine.
KNOWN_ACCELS = ("auto", "whpx", "kvm", "hvf", "tcg")
DEFAULT_ACCEL = "auto"

# Guest display adapters. "std" is the default on purpose: it needs no driver in
# the guest, so *something* appears for any OS — which is the whole point of
# arbitrary-guest ISO boot. "virtio" needs a guest driver but is the only device
# that renders a VGA-text-mode guest under hardware acceleration.
KNOWN_DISPLAYS = ("std", "virtio")
DEFAULT_DISPLAY = "std"


class BootSource(str, Enum):
    """Where an instance's disk came from — decides its whole access story.

    IMAGE  -> copy-on-write overlay on a cloud image; cloud-init injects our SSH
              key, so SSH is the primary way in.
    ISO    -> blank disk plus boot media; no key injection and no promised
              network service, so the console is the only way in.
    """

    IMAGE = "image"
    ISO = "iso"


class GuestOS(str, Enum):
    """The operating system family a guest runs.

    This exists so the UI can stop guessing. Graphics, storage initialisation
    and the whole access story differ by OS, and until now every one of them
    was written as though Linux were the only possibility.

    Both families provision as of Phase 13. What differs is the virtual
    hardware, and it differs completely: Windows Setup carries inbox drivers
    for a SATA disk and an Intel e1000e NIC and for neither virtio-blk nor
    virtio-net, so a Windows guest gets an AHCI root disk, an e1000e NIC and
    standard VGA, while Linux keeps the paravirtualised set. The mapping lives
    in ``kurukuru.engines.qemu.GUEST_PROFILES``.

    Two consequences worth stating where the enum is read rather than leaving
    to be discovered:

    * **Windows installs from an ISO the user supplies.** Microsoft's images
      cannot be redistributed or fetched automatically, and there is no Windows
      equivalent of the Ubuntu cloud image, so there is nothing to overlay.
    * **Windows gets no NoCloud seed and no SSH.** ``cloud_init.build_config``
      emits a shell, a sudoers line and apt packages; Windows consumes none of
      them. Access is the console until the user enables RDP inside the guest,
      at which point the port-forward preset covers 3389.

    **Windows 11 is not supported**, and cannot be until the host side changes:
    it requires TPM 2.0, and QEMU disables TPM emulation entirely on Windows
    hosts (``meson.build``: "TPM emulation only available on POSIX systems"),
    so no QEMU version can offer it here. Windows Server and Windows 10 have no
    such requirement and are what this targets. See docs/DECISIONS.md.
    """

    LINUX = "linux"
    WINDOWS = "windows"


#: Guest families a launch may actually request today.
PROVISIONABLE_GUEST_OS = (GuestOS.LINUX, GuestOS.WINDOWS)


class InstanceStatus(str, Enum):
    """VM lifecycle states.

    PENDING       -> accepted, not yet handed to the hypervisor
    PROVISIONING  -> launch in progress (disk creation / boot)
    RUNNING       -> booted, IP assigned
    STOPPED       -> exists on hypervisor but powered off
    TERMINATED    -> deleted + purged (terminal)
    ERROR         -> provisioning or reconciliation failed (terminal-ish,
                     retained for observability rather than silently vanishing)
    """

    PENDING = "Pending"
    PROVISIONING = "Provisioning"
    RUNNING = "Running"
    STOPPED = "Stopped"
    TERMINATED = "Terminated"
    ERROR = "Error"


#: Label stored on rows sized by hand rather than from a preset.
CUSTOM_PRESET = "custom"


class Flavor(str, Enum):
    """Built-in sizing presets. Retained as an enum for the preset *catalog*;
    a row's label is a plain string because it may also be ``custom``."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


#: Minimum password length. Deliberately a length floor and nothing else: no
#: character-class rules, which push people towards `Password1!` and are worse
#: than length. Long enough that argon2id makes an offline guess impractical.
MIN_PASSWORD_LENGTH = 12


def validate_password(value: str) -> str:
    """The one password rule, shared by the API and the CLI so they cannot differ."""
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Name of the project every pre-Phase-11 resource is migrated into, and the
#: one new resources land in when the caller does not choose.
DEFAULT_PROJECT_NAME = "default"


class Project(SQLModel, table=True):
    """A label for grouping resources. **Not** an isolation boundary.

    This is organisation, not tenancy: there is no authentication, so there is
    nothing to isolate *from*. Moving an instance between projects changes a
    column and nothing else — not its disk, not its ports, not who can reach
    it. Anything that can reach the API can see and act on every project.

    That is stated here, in the UI, in the API docs and in the delete
    confirmation, because a boundary users believe in but the system does not
    enforce is worse than no boundary at all.
    """

    __tablename__ = "projects"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    #: Unique among all projects, enforced in the router — the same story as
    #: instance, image and keypair names.
    name: str = Field(index=True, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    #: The project resources fall back to. Exactly one row has this set; it
    #: cannot be deleted, because deleting it would leave nowhere to put the
    #: resources of any other project that is deleted later.
    is_default: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class ProjectCreate(SQLModel):
    """Body for POST /projects."""

    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Project name cannot be blank")
        return v


class ProjectUpdate(SQLModel):
    """Body for PATCH /projects/{id}. Omitted fields are left alone."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("Project name cannot be blank")
        return v


class ProjectRead(SQLModel):
    """Response schema for the projects API."""

    id: str
    name: str
    description: str | None
    is_default: bool
    created_at: datetime
    #: How much is in it, so the UI can warn before a delete without a second
    #: round trip. Counts live instances only — terminated rows are audit
    #: history and never block anything.
    instance_count: int = 0
    image_count: int = 0
    keypair_count: int = 0


class ImageFormat(str, Enum):
    """Disk formats qemu-img can use as a backing file."""

    QCOW2 = "qcow2"
    RAW = "raw"
    VMDK = "vmdk"
    VDI = "vdi"


class ImageSource(str, Enum):
    BUILTIN = "builtin"    # shipped by the orchestrator; not deletable
    IMPORTED = "imported"  # registered by the user from a local file


class ImageStatus(str, Enum):
    """AVAILABLE is the only state an instance can be launched from."""

    IMPORTING = "Importing"
    AVAILABLE = "Available"
    ERROR = "Error"


class Image(SQLModel, table=True):
    """A disk image instances can be backed by.

    Names are unique among *existing* rows and enforced in the router rather
    than the schema — the same reasoning as instance names, kept consistent so
    there is one story about uniqueness in this codebase.
    """

    __tablename__ = "images"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str = Field(index=True, min_length=1, max_length=64)
    #: Filename within the base-images directory (never a caller-supplied path).
    filename: str
    format: ImageFormat = Field(default=ImageFormat.QCOW2)
    source: ImageSource = Field(default=ImageSource.IMPORTED)
    #: Sizes as qemu-img reports them: virtual is what the guest sees, actual is
    #: what the file occupies — they differ wildly for sparse qcow2 files.
    virtual_size_bytes: int | None = Field(default=None)
    actual_size_bytes: int | None = Field(default=None)
    #: Whether the guest will consume a NoCloud seed. False means no SSH key can
    #: be injected, so instances from it are console-access only.
    has_cloud_init: bool = Field(default=False)
    status: ImageStatus = Field(default=ImageStatus.IMPORTING, index=True)
    error_message: str | None = Field(default=None)
    #: Organisational grouping only — see :class:`Project`. Nullable because
    #: the migration backfills existing rows and a null is readable as "the
    #: default project" rather than as a broken row.
    project_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class ImageImportRequest(SQLModel):
    """Body for POST /images/import.

    ``path`` is a path on the *backend's* filesystem, not an upload. Installer
    and disk images run to gigabytes; for a local-first tool where the backend
    and the user share a machine, pointing at a file beats streaming it through
    a browser.
    """

    name: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1)
    has_cloud_init: bool = False
    project_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Image name cannot be blank")
        return v


class ImageFetchRequest(SQLModel):
    """Body for POST /images/fetch — download an image from a URL.

    This is how people actually obtain cloud images: every distribution
    publishes a qcow2 at a stable URL alongside a checksum. It is also the only
    import route that works identically no matter what OS the *customer* is
    running, because nothing about it involves their filesystem.
    """

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1)
    #: Expected SHA256, lowercase hex. Optional but strongly encouraged — every
    #: image provider publishes one, and it is the only way to know the bytes
    #: that arrived are the bytes that were published.
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    has_cloud_init: bool = True
    project_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Image name cannot be blank")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("SHA256 must be 64 hexadecimal characters")
        return v


class ImageRead(SQLModel):
    """Response schema for the images API."""

    id: str
    name: str
    filename: str
    format: ImageFormat
    source: ImageSource
    virtual_size_bytes: int | None
    actual_size_bytes: int | None
    has_cloud_init: bool
    status: ImageStatus
    error_message: str | None
    project_id: str | None = None
    created_at: datetime


class KeyPairSource(str, Enum):
    """Where a keypair came from, which decides what we may do with it.

    ORCHESTRATOR is the single key generated in Phase 4 and baked into every
    instance launched since. It is adopted as a row rather than recreated, and
    it cannot be deleted — instances in the field trust it.
    """

    ORCHESTRATOR = "orchestrator"
    IMPORTED = "imported"
    GENERATED = "generated"


class KeyPair(SQLModel, table=True):
    """An SSH public key that can be installed on new instances.

    Only the *public* half is a first-class value here. A private key exists on
    disk for orchestrator and generated keys, and this row records where — but
    the contents never cross the API. See :class:`KeyPairRead`.
    """

    __tablename__ = "keypairs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    #: Unique among all keypairs, enforced in the router (consistent with how
    #: instance and image names are handled — one story about uniqueness).
    name: str = Field(index=True, min_length=1, max_length=64)
    public_key: str
    #: OpenSSH's ``SHA256:...`` spelling, computed on import. Indexed because
    #: "do I already have this key?" is the question users actually ask.
    fingerprint: str = Field(index=True)
    #: Wire type label for display: ed25519, rsa, ecdsa-256...
    key_type: str = Field(default="", max_length=32)
    source: KeyPairSource = Field(default=KeyPairSource.IMPORTED, index=True)
    has_private_key: bool = Field(default=False)
    #: Where the private half lives on the backend's filesystem, for `ssh -i`.
    #: Null for imported keys, whose private half we never see.
    private_key_path: str | None = Field(default=None)
    #: Organisational grouping only — see :class:`Project`.
    project_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class InstanceKeyPair(SQLModel, table=True):
    """Which keypairs an instance was launched with.

    A join table rather than a JSON column, because the detail view lists these
    and the association outlives the keypair row: deleting a keypair does not
    remove it from a guest it is already baked into, so the link is kept and
    rendered as a name we may no longer be able to resolve.
    """

    __tablename__ = "instance_keypairs"

    instance_id: str = Field(foreign_key="instances.id", primary_key=True)
    keypair_id: str = Field(primary_key=True, index=True)
    #: Denormalised so the detail view can still say which key was installed
    #: after the keypair row is gone. History, not a cache.
    keypair_name: str = Field(default="")
    fingerprint: str = Field(default="")


class KeyPairRead(SQLModel):
    """Response schema. Deliberately has no private-key field of any kind."""

    id: str
    name: str
    public_key: str
    fingerprint: str
    key_type: str
    source: KeyPairSource
    has_private_key: bool
    #: The *path*, never the contents. There is no endpoint that returns a
    #: private key: it is written once at generation and read only by `ssh -i`.
    #: Download-once semantics would need somewhere to hold the secret until
    #: collection, which is a decision for a phase that has authentication.
    private_key_path: str | None
    project_id: str | None = None
    created_at: datetime


def validate_snapshot_tag(v: str) -> str:
    """Keep a snapshot name usable as a qcow2 tag.

    ``qemu-img`` takes the tag as a positional argument, so a leading dash would
    be read as a flag, and the listing is parsed back out of a column-formatted
    table where a newline would corrupt the row.

    Shared by instance and volume snapshots deliberately: both end up as tags
    inside a qcow2 through the same tool, so a rule that held for one and not
    the other would be a difference with no cause.
    """
    v = v.strip()
    if not v:
        raise ValueError("Snapshot name cannot be blank")
    if v.startswith("-"):
        raise ValueError("Snapshot name cannot start with '-'")
    if any(c in v for c in "\r\n\t"):
        raise ValueError("Snapshot name cannot contain line breaks or tabs")
    return v


class SnapshotStatus(str, Enum):
    """Lifecycle of a snapshot row.

    CREATING and DELETING exist because both operations are backgrounded: a
    snapshot of a diverged overlay copies real data and takes as long as it
    takes, and an HTTP request must not hold a worker for it.
    """

    CREATING = "Creating"
    AVAILABLE = "Available"
    ERROR = "Error"
    DELETING = "Deleting"


class Snapshot(SQLModel, table=True):
    """A point-in-time state of one instance's disk.

    Lives inside the instance's qcow2 overlay as an internal snapshot, which is
    why destroying the instance destroys these with it — there is no separate
    file to keep.
    """

    __tablename__ = "snapshots"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    instance_id: str = Field(foreign_key="instances.id", index=True)
    #: Unique per instance, enforced in the router. This is also the qcow2 tag,
    #: so it must survive a round trip through qemu-img.
    name: str = Field(index=True, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    #: VM state size as the hypervisor reports it — zero for a stopped-instance
    #: snapshot, whose cost shows up as the overlay growing instead.
    size_bytes: int | None = Field(default=None)
    status: SnapshotStatus = Field(default=SnapshotStatus.CREATING, index=True)
    error_message: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class SnapshotCreate(SQLModel):
    """Body for POST /instances/{id}/snapshots."""

    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_snapshot_tag(v)


class SnapshotRead(SQLModel):
    """Response schema for the snapshots API."""

    id: str
    instance_id: str
    name: str
    description: str | None
    size_bytes: int | None
    status: SnapshotStatus
    error_message: str | None
    created_at: datetime


class NetworkMode(str, Enum):
    """How an instance reaches the world.

    Only USER ships. The other two are modelled so instances have somewhere to
    land when they arrive, and so the UI can explain what each would require
    rather than showing a disabled button with no reason attached — see
    :data:`DEFERRED_NETWORK_MODES` and DECISIONS #24.
    """

    USER = "user"
    HOST_ONLY = "host_only"
    BRIDGED = "bridged"


#: Modes that exist in the model but cannot be selected, and precisely what
#: each would need from the operator. This is the single source for the API,
#: the dashboard and the docs, so the three cannot drift into disagreeing about
#: what is required.
#:
#: Every claim here was measured on this build; the transcripts are in
#: DECISIONS #24.
DEFERRED_NETWORK_MODES: dict[str, dict[str, str]] = {
    NetworkMode.HOST_ONLY.value: {
        "label": "Host-only / internal",
        "summary": "VMs on a private segment that can talk to each other but not the LAN.",
        "blocker": (
            "QEMU's portable shared-segment backend (a multicast socket) fails "
            "on Windows: 'can't bind ip=230.0.0.1 to socket'. The working "
            "alternative makes one VM the switch for the others, so the network "
            "dies whenever that instance is stopped."
        ),
        "requires": (
            "A switch process this project does not have. Nothing the operator "
            "can install fixes it."
        ),
    },
    NetworkMode.BRIDGED.value: {
        "label": "Bridged",
        "summary": "The VM appears on your LAN with its own IP from your router.",
        "blocker": (
            "Needs a host tap device, which is an elevated operation on every "
            "platform this runs on."
        ),
        "requires": (
            "Windows: Administrator, to install the tap-windows6 kernel driver "
            "(QEMU has the tap backend compiled in but there is no adapter for "
            "it to open). "
            "Linux: root or CAP_NET_ADMIN — /dev/net/tun is world-writable but "
            "'ip tuntap add' returns 'Operation not permitted' without it — "
            "plus a pre-made bridge and a setuid qemu-bridge-helper with an "
            "/etc/qemu/bridge.conf ACL."
        ),
    },
}

#: Name of the network every instance is on today.
DEFAULT_NETWORK_NAME = "default"


class Network(SQLModel, table=True):
    """A network an instance can be attached to.

    Exactly one ships: ``user``, QEMU's built-in NAT, which is what every
    instance has always been on. It is modelled explicitly so existing rows map
    onto the model rather than being described as having "no network", and so
    the deferred modes have a shape to arrive in.
    """

    __tablename__ = "networks"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str = Field(index=True, min_length=1, max_length=64)
    mode: NetworkMode = Field(default=NetworkMode.USER, index=True)
    #: The guest-visible subnet. Fixed by QEMU's SLIRP for user mode; it is
    #: recorded rather than derived so a future mode can carry a real one.
    cidr: str | None = Field(default="10.0.2.0/24")
    is_default: bool = Field(default=False, index=True)
    project_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class NetworkRead(SQLModel):
    """Response schema for the networks API."""

    id: str
    name: str
    mode: NetworkMode
    cidr: str | None
    is_default: bool
    project_id: str | None
    created_at: datetime
    instance_count: int = 0


class ForwardProtocol(str, Enum):
    TCP = "tcp"
    UDP = "udp"


class PortForward(SQLModel, table=True):
    """A host port that reaches a port inside one guest.

    User-mode networking gives a guest no address of its own, so a forward is
    the only way in. **The SSH forward is deliberately not in this table** — it
    lives on ``Instance.ssh_port`` and is emitted by the engine from the
    instance's runtime file, exactly as it has since Phase 5.

    That is a decision, not an omission (DECISIONS #25). Making this table
    authoritative for SSH would put the one forward every instance depends on
    behind a migration, and a single row that failed to backfill would produce
    an instance nobody can reach. The API *shows* the SSH forward alongside
    these, marked derived and read-only, so the listing tells the whole truth
    while the schema carries none of the risk.
    """

    __tablename__ = "port_forwards"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    instance_id: str = Field(foreign_key="instances.id", index=True)
    host_port: int = Field(index=True)
    guest_port: int
    protocol: ForwardProtocol = Field(default=ForwardProtocol.TCP)
    description: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=_utcnow)

    @property
    def spec(self) -> str:
        """QEMU's hostfwd spelling, used both at launch and over QMP."""
        from kurukuru.engines.qemu import HOST_IP

        return f"{self.protocol.value}:{HOST_IP}:{self.host_port}-:{self.guest_port}"


class ForwardPreset(SQLModel):
    """A named guest port worth offering as one click instead of two numbers.

    Deliberately thin. A preset fills the form in; it does not create anything,
    reserve anything, or imply the guest is listening. That last part matters
    for RDP especially: Remote Desktop is **off** by default on every Windows
    edition, so a forward created before the user enables it inside the guest
    is a correct forward to a closed port. The description says so, and the UI
    repeats it, because "the forward exists therefore it should work" is the
    obvious wrong inference.
    """

    key: str
    label: str
    guest_port: int
    protocol: ForwardProtocol = ForwardProtocol.TCP
    #: What this reaches, and what the user must do inside the guest first.
    description: str
    #: Guest family this is offered for; None means any.
    guest_os: GuestOS | None = None


#: Offered presets. RDP is the whole list, and on purpose — it is the access
#: path for a Windows guest once the console has got the OS installed, and the
#: forwards feature already does everything else. Adding "the ports someone
#: might want" is how a focused feature becomes a directory.
FORWARD_PRESETS: tuple[ForwardPreset, ...] = (
    ForwardPreset(
        key="rdp",
        label="Remote Desktop (RDP)",
        guest_port=3389,
        protocol=ForwardProtocol.TCP,
        description=(
            "Reaches Remote Desktop inside the guest. Turn it on in Windows "
            "first — Settings > System > Remote Desktop — because it is off by "
            "default and the forward alone does not enable it."
        ),
        guest_os=GuestOS.WINDOWS,
    ),
)


class PortForwardCreate(SQLModel):
    """Body for POST /instances/{id}/forwards."""

    host_port: int = Field(gt=0, lt=65536)
    guest_port: int = Field(gt=0, lt=65536)
    protocol: ForwardProtocol = ForwardProtocol.TCP
    description: str | None = Field(default=None, max_length=200)


class PortForwardRead(SQLModel):
    """Response schema. Covers both real rows and the synthetic SSH entry."""

    id: str
    instance_id: str
    host_port: int
    guest_port: int
    protocol: ForwardProtocol
    description: str | None
    created_at: datetime | None = None
    #: True for the SSH forward, which is derived from the instance's pinned
    #: port rather than stored here. Clients must render it as read-only:
    #: deleting it is refused, and a control that appears to work and then
    #: brings the row back is worse than no control.
    derived: bool = False


class VolumeStatus(str, Enum):
    """Lifecycle of an additional disk.

    CREATING exists because ``qemu-img create`` on a large volume is not
    instant, and ATTACHED is a *state*, not a flag, so a volume can never be
    described as both available and in use.
    """

    CREATING = "Creating"
    AVAILABLE = "Available"
    ATTACHED = "Attached"
    ERROR = "Error"


class Volume(SQLModel, table=True):
    """A blank disk that can be attached to an instance.

    The EBS story, minus the network. A volume is a qcow2 file that outlives
    the instances it is attached to — which is the entire point, and the reason
    terminating an instance detaches its volumes rather than deleting them.

    Unlike an instance name, a volume name is **not** the hypervisor's identity:
    the file is named by uuid and nothing outside this database refers to it.
    That is what lets volume names be unique per project while instance names
    must be global (DECISIONS #20 and #22).
    """

    __tablename__ = "volumes"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    #: Unique within a project, enforced in the router.
    name: str = Field(index=True, min_length=1, max_length=64)
    size_gb: int
    format: ImageFormat = Field(default=ImageFormat.QCOW2)
    #: Absolute path to the backing file. Recorded rather than derived so a
    #: volume created under one configuration survives a change to qemu_dir.
    path: str = Field(default="")
    status: VolumeStatus = Field(default=VolumeStatus.CREATING, index=True)
    error_message: str | None = Field(default=None)
    attached_instance_id: str | None = Field(default=None, index=True)
    #: Position in the instance's drive order, 0-based. This is what makes
    #: guest device names stable: the engine emits ``-drive`` arguments in this
    #: order on every launch, and QEMU enumerates virtio-blk devices in
    #: argument order. Null while detached.
    attach_order: int | None = Field(default=None)
    project_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class VolumeSnapshot(SQLModel, table=True):
    """A point-in-time state of one volume's qcow2.

    A separate table from :class:`Snapshot` rather than a shared one with a
    discriminator, for three reasons (DECISIONS #43). ``Snapshot.instance_id``
    is NOT NULL with a real foreign key, so sharing would need it nullable
    alongside a nullable ``volume_id`` and a mutual-exclusion rule enforced in
    application code. The semantics are opposite: an instance snapshot dies with
    its instance, while a volume outlives every instance it is attached to and
    its snapshots must outlive them too. And making a NOT NULL column nullable
    in SQLite is a table rebuild — the create-copy-swap this project's additive
    migrations deliberately never do — on the table holding restore points.

    Like an instance snapshot, this lives *inside* the volume's qcow2, which is
    why deleting the volume takes its snapshots with it and no hypervisor work
    is needed to clean them up.
    """

    __tablename__ = "volume_snapshots"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    volume_id: str = Field(foreign_key="volumes.id", index=True)
    #: Unique per volume, enforced in the router. Also the qcow2 tag.
    name: str = Field(index=True, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    #: VM state size as qemu-img reports it. Always zero here — a volume has no
    #: VM state — but carried so the shape matches an instance snapshot rather
    #: than inventing a difference the reader has to explain to themselves.
    size_bytes: int | None = Field(default=None)
    status: SnapshotStatus = Field(default=SnapshotStatus.CREATING, index=True)
    error_message: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class VolumeSnapshotCreate(SQLModel):
    """Body for POST /volumes/{id}/snapshots."""

    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return validate_snapshot_tag(v)


class VolumeSnapshotRead(SQLModel):
    """Response schema for the volume snapshots API."""

    id: str
    volume_id: str
    name: str
    description: str | None
    size_bytes: int | None
    status: SnapshotStatus
    error_message: str | None
    created_at: datetime


class VolumeCreate(SQLModel):
    """Body for POST /volumes."""

    name: str = Field(min_length=1, max_length=64)
    size_gb: int = Field(gt=0)
    project_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Volume name cannot be blank")
        return v


class VolumeAttach(SQLModel):
    """Body for POST /volumes/{id}/attach."""

    instance_id: str


class VolumeRead(SQLModel):
    """Response schema for the volumes API."""

    id: str
    name: str
    size_gb: int
    format: ImageFormat
    path: str
    status: VolumeStatus
    error_message: str | None
    attached_instance_id: str | None
    attach_order: int | None
    project_id: str | None
    created_at: datetime
    #: Name of the instance it is attached to, for a listing that would
    #: otherwise show a uuid. Null when detached.
    attached_instance_name: str | None = None
    #: Guest OS of the instance it is attached to. Null when detached — and
    #: carried at all because ``device_hint`` is a Linux device name, and a
    #: client that rendered it for a Windows guest would be telling that user
    #: to look for something their operating system has never heard of.
    attached_instance_guest_os: GuestOS | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def device_hint(self) -> str | None:
        """Where the guest will most likely see it — ``vdb``, ``vdc``, …

        A *hint*, and named one. The root disk is the first virtio drive, so an
        attach at order 0 becomes the second and lands on ``vdb``. That holds
        because the engine emits drives in ``attach_order`` and QEMU enumerates
        virtio-blk in argument order — but the guest kernel names its own
        devices, and a guest with other block devices could still surprise you.
        ``lsblk`` in the guest is the authority; this is the starting point.

        **Linux naming, deliberately unconditional.** This is the name the
        *Linux kernel* gives a virtio-blk device, and it stays that regardless
        of what is attached, because it is derived from the drive order — a
        property of the QEMU command line, not of the guest. Windows guests see
        the same disk with no such name at all, which is what
        :attr:`windows_disk_hint` is for. Presentation picks the one that
        matches the guest; inventing a cross-platform spelling here would just
        be a third name for one disk.
        """
        if self.attach_order is None:
            return None
        return f"vd{chr(ord('b') + self.attach_order)}" if self.attach_order < 25 else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def windows_disk_hint(self) -> str | None:
        """The same disk as Disk Management numbers it — ``Disk 1``, ``Disk 2``…

        Same reasoning and the same arithmetic as :attr:`device_hint`: the root
        disk is Disk 0, so an attach at order 0 is Disk 1. Windows has no
        ``/dev`` path to offer, and printing one at a Windows user is worse
        than printing nothing — they will go looking for it.
        """
        if self.attach_order is None:
            return None
        return f"Disk {self.attach_order + 1}"


class EventKind(str, Enum):
    """What happened. Closed on purpose: the UI renders an icon and a phrasing
    per kind, so an unrecognised value would arrive as a blank row.

    Adding one is a three-line change (here, the writer, the UI's icon map) and
    that friction is the point — it keeps the feed a vocabulary rather than a
    log-line dump.
    """

    CREATED = "created"
    PROVISIONING_STARTED = "provisioning_started"
    PROVISIONING_SUCCEEDED = "provisioning_succeeded"
    PROVISIONING_FAILED = "provisioning_failed"
    STARTED = "started"
    STOPPED = "stopped"
    TERMINATED = "terminated"
    FORCE_TERMINATED = "force_terminated"
    ERRORED = "errored"
    SNAPSHOT_CREATED = "snapshot_created"
    SNAPSHOT_RESTORED = "snapshot_restored"
    SNAPSHOT_DELETED = "snapshot_deleted"
    VOLUME_ATTACHED = "volume_attached"
    VOLUME_DETACHED = "volume_detached"
    RESTARTED = "restarted"
    CLONED = "cloned"
    PORT_FORWARD_ADDED = "port_forward_added"
    PORT_FORWARD_REMOVED = "port_forward_removed"
    RECONCILED = "reconciled"
    IMAGE_IMPORT = "image_import"
    #: A Windows guest looked stuck after an internal reboot and was restarted
    #: automatically — the watchdog workaround in kurukuru/reboot_watchdog.py
    #: for an upstream QEMU/WHPX defect, not a routine correction. Deliberately
    #: its own kind rather than reusing RESTARTED or RECONCILED: a user must be
    #: able to tell "the backend did something to your VM without being asked"
    #: apart from both "you asked for this" and "a field was silently corrected".
    AUTO_RESTARTED = "auto_restarted"


class EventActor(str, Enum):
    """Who did it — the only attribution possible without authentication.

    API means a request came in and something acted on it. RECONCILER means
    nobody asked: the background pass found the hypervisor disagreeing with the
    database and corrected the record. SYSTEM is startup and maintenance work.

    This is deliberately not a *user* field. There are no users, and a column
    that could only ever say "admin" would imply an accountability this system
    does not have.
    """

    API = "api"
    RECONCILER = "reconciler"
    SYSTEM = "system"


class InstanceEvent(SQLModel, table=True):
    """One thing that happened, recorded once and never updated.

    Append-only by convention rather than by constraint — SQLite has no such
    thing — but nothing in this codebase reads a row back to modify it, and
    that is the property the whole table exists for. ``updated_at`` on an
    instance holds exactly one transition; this holds all of them.
    """

    __tablename__ = "instance_events"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    #: Null for events that are not about one instance — an image import is the
    #: current example. Those appear in the global feed only.
    #:
    #: Deliberately NOT a foreign key: an event must be able to outlive nothing
    #: in particular, but more importantly a FK with cascade would delete the
    #: history of an instance at exactly the moment the history became most
    #: interesting. Terminated rows are retained, and so are their events.
    instance_id: str | None = Field(default=None, index=True)
    #: Denormalised so the global feed can name an instance without a join, and
    #: still name it after the row is gone.
    instance_name: str = Field(default="", max_length=64)
    occurred_at: datetime = Field(default_factory=_utcnow, index=True)
    kind: EventKind = Field(index=True)
    actor: EventActor = Field(default=EventActor.API)
    #: One line, already phrased for a human. The UI renders this verbatim.
    summary: str = Field(default="", max_length=500)
    #: Longer text or a JSON string — an error's full message, or what the
    #: reconciler observed versus what it changed. Null when there is no more
    #: to say; the UI only offers to expand a row that has one.
    detail: str | None = Field(default=None)


class InstanceEventRead(SQLModel):
    """Response schema for the events API."""

    id: str
    instance_id: str | None
    instance_name: str
    occurred_at: datetime
    kind: EventKind
    actor: EventActor
    summary: str
    detail: str | None


class InstanceKeyPairRead(SQLModel):
    """One key installed on an instance, as recorded when it launched."""

    keypair_id: str
    name: str
    fingerprint: str
    #: True when the catalog row has since been deleted. The key remains in the
    #: guest's authorized_keys — deleting the record never touched the VM.
    deleted: bool = False


class KeyPairImportRequest(SQLModel):
    """Body for POST /keypairs/import."""

    name: str = Field(min_length=1, max_length=64)
    public_key: str = Field(min_length=1)
    project_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Key pair name cannot be blank")
        return v


class KeyPairGenerateRequest(SQLModel):
    """Body for POST /keypairs/generate."""

    name: str = Field(min_length=1, max_length=64)
    project_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Key pair name cannot be blank")
        return v


class InstanceBase(SQLModel):
    """Fields shared between the table model and API schemas."""

    # Indexed but deliberately NOT unique. Terminated rows are retained as an
    # audit trail, so a schema-level unique index would make every name
    # permanently unusable after its first instance was destroyed. Uniqueness is
    # enforced in the create route against *live* rows only.
    name: str = Field(index=True, min_length=2, max_length=31)
    # The preset this instance started from, or "custom". A label for history,
    # not a constraint — the numbers below are what actually get launched, and
    # editing them is the point of custom sizing.
    flavor: str = Field(default=Flavor.SMALL.value, max_length=16)
    # Which driver owns this VM. Retained after the Multipass retirement so
    # historical rows keep saying what actually ran them — rewriting them would
    # destroy the audit trail and claim VMs were QEMU when they never were.
    engine: str = Field(default=DEFAULT_ENGINE, index=True, max_length=16)
    boot_source: BootSource = Field(default=BootSource.IMAGE)


class Instance(InstanceBase, table=True):
    """Persisted desired-state record for a VM."""

    __tablename__ = "instances"

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    status: InstanceStatus = Field(default=InstanceStatus.PENDING, index=True)
    # Resolved hardware. Nullable only so the additive migration can backfill
    # pre-Phase-7 rows from their preset; every new row sets all three.
    cpus: int | None = Field(default=None)
    memory_mb: int | None = Field(default=None)
    disk_gb: int | None = Field(default=None)
    ip_address: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    # Populated when status == ERROR so operators can see *why*.
    error_message: str | None = Field(default=None)

    # --- Engine runtime detail (QEMU only; null for Multipass rows) --------- #
    # The engine owns these on disk; they are mirrored here by the reconciler so
    # the API can render an SSH command without asking the hypervisor.
    ssh_port: int | None = Field(default=None)
    vnc_port: int | None = Field(default=None)
    qmp_port: int | None = Field(default=None)
    pid: int | None = Field(default=None)
    #: Accelerator the VM actually runs under — the UI needs it to explain why
    #: a console may show nothing (see QemuEngine.ACCELS_WITHOUT_DISPLAY).
    accel: str | None = Field(default=None)
    #: Guest display adapter; see KNOWN_DISPLAYS. Null on pre-Phase-7 rows,
    #: which all ran the "std" default.
    display: str | None = Field(default=None)
    #: Whether the orchestrator's SSH key reached this guest. Null until the
    #: engine reports it; false for ISO installs and images without cloud-init.
    ssh_enabled: bool | None = Field(default=None)
    #: Set when this instance was created from an ISO (Phase 6, Part B).
    iso: str | None = Field(default=None)
    #: Image this instance's disk is backed by; null for ISO and legacy rows.
    image_id: str | None = Field(default=None, index=True)
    #: The raw cloud-config the user supplied at launch, stored so the detail
    #: view can show what this instance was actually built with. Not the merged
    #: document — that is derived, and re-deriving it keeps one renderer.
    user_data: str | None = Field(default=None)
    #: Operating system family. Drives the guest-facing advice the UI gives —
    #: how a volume is initialised, which display renders — none of which is
    #: the same on Windows. Defaults to Linux, which is what every instance
    #: created before this field existed is.
    guest_os: GuestOS = Field(default=GuestOS.LINUX, index=True)
    #: The hypervisor build that last launched this instance, e.g. "10.0.94".
    #: Recorded because a VM that worked last month and does not today, on a
    #: host somebody upgraded in between, is otherwise undiagnosable — nothing
    #: else on the row says which QEMU built it. Written at launch and refreshed
    #: on each start, so it describes the process actually running rather than
    #: the one that first created the disk. Null for rows that predate this.
    qemu_version: str | None = Field(default=None)
    #: False when the VM process is alive but its QMP monitor did not answer.
    #: Persisted because it is a live engine observation and `degraded` is
    #: computed from the row. Null means "no monitor concept, or not running".
    monitor_reachable: bool | None = Field(default=None)
    #: Network this instance is attached to. Null on rows that predate the
    #: model; the API reads that as the default user network, which is what
    #: they have always been on.
    network_id: str | None = Field(default=None, index=True)
    #: Organisational grouping only — see :class:`Project`. Changing it moves
    #: nothing on disk: the instance keeps its name, its directory, its ports
    #: and its reachability. Names stay globally unique regardless of project
    #: (DECISIONS #20).
    project_id: str | None = Field(default=None, index=True)


class InstanceCreate(InstanceBase):
    """Request body for POST /instances.

    Sizing is explicit: ``cpus``/``memory_mb``/``disk_gb`` are what gets
    launched. ``preset`` (and its older alias ``flavor``) only *fill in* those
    numbers when they are omitted, so a preset is a starting point rather than
    a menu of the only allowed shapes.
    """

    #: Named starting point. ``flavor`` is accepted as an alias so existing
    #: clients keep working.
    preset: str | None = None
    cpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not INSTANCE_NAME_PATTERN.match(v):
            raise ValueError(
                "Name must start with a letter and contain only lowercase "
                "letters, digits, and hyphens (2-31 chars)."
            )
        return v

    @field_validator("cpus", "memory_mb", "disk_gb")
    @classmethod
    def validate_positive(cls, v: int | None) -> int | None:
        """Catch nonsense before it reaches the capacity check.

        Real floors are enforced server-side against host capacity; this only
        rejects values that can't be a size at all, so the capacity message
        never has to explain a negative number.
        """
        if v is not None and v <= 0:
            raise ValueError("must be a positive whole number")
        return v

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, v: str) -> str:
        v = v.strip().lower()
        if v in RETIRED_ENGINES:
            raise ValueError(
                f"The '{v}' engine is no longer supported. Existing {v} instances "
                f"remain visible and can be terminated, but new ones must use: "
                f"{', '.join(KNOWN_ENGINES)}."
            )
        if v not in KNOWN_ENGINES:
            raise ValueError(
                f"Unknown engine '{v}'. Known engines: {', '.join(KNOWN_ENGINES)}."
            )
        return v

    #: Keypairs whose public halves are installed on the guest. Omitted means
    #: the orchestrator key alone, which is exactly what every instance got
    #: before keypairs existed — the default has to keep working unchanged.
    #: An explicit empty list is honoured as "no keys", which produces a
    #: console-only instance.
    keypair_ids: list[str] | None = None
    #: Raw cloud-config, merged onto the generated one rather than replacing it
    #: (see app/user_data.py). Refused for ISO instances, which have no
    #: cloud-init to read it.
    user_data: str | None = None
    #: Boot media (filename within iso_dir). Setting this makes an ISO instance.
    iso: str | None = None
    #: Image to back the disk with; None means the built-in Ubuntu cloud image.
    image_id: str | None = None
    #: Operating system family. Only ``linux`` can be provisioned today; see
    #: :class:`GuestOS` for exactly what Windows still needs.
    guest_os: GuestOS = GuestOS.LINUX
    #: Network to attach to. Omitted means the default (user-mode NAT), which
    #: is the only mode that ships.
    network_id: str | None = None
    #: Project to file this instance under. Omitted means the default project.
    #: Purely organisational — it does not affect the name, which is unique
    #: across every project (DECISIONS #20).
    project_id: str | None = None
    #: Accelerator preference; see KNOWN_ACCELS.
    accel: str = DEFAULT_ACCEL
    #: Guest display adapter; see KNOWN_DISPLAYS.
    display: str = DEFAULT_DISPLAY

    @field_validator("accel")
    @classmethod
    def validate_accel(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_ACCELS:
            raise ValueError(
                f"Unknown accelerator '{v}'. Known: {', '.join(KNOWN_ACCELS)}."
            )
        return v

    @field_validator("display")
    @classmethod
    def validate_display(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_DISPLAYS:
            raise ValueError(
                f"Unknown display '{v}'. Known: {', '.join(KNOWN_DISPLAYS)}."
            )
        return v

    @field_validator("iso")
    @classmethod
    def validate_iso(cls, v: str | None) -> str | None:
        """Reject anything that isn't a plain filename.

        Path traversal is checked again server-side against the resolved
        directory; this is the cheap first gate that keeps ``../`` and absolute
        paths out of the request entirely.
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if "/" in v or "\\" in v or v in (".", "..") or ":" in v:
            raise ValueError("ISO must be a plain filename inside the ISO directory")
        return v


class InstanceClone(SQLModel):
    """Body for POST /instances/{id}/clone."""

    name: str = Field(min_length=2, max_length=31)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not INSTANCE_NAME_PATTERN.match(v):
            raise ValueError(
                "Name must start with a letter and contain only lowercase "
                "letters, digits, and hyphens (2-31 chars)."
            )
        return v


class InstanceRead(InstanceBase):
    """Response schema — everything the dashboard needs."""

    id: str
    status: InstanceStatus
    #: Resolved hardware. Null only on pre-Phase-7 rows the migration could not
    #: expand (an unknown preset label).
    cpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    ip_address: str | None
    created_at: datetime
    updated_at: datetime
    error_message: str | None
    ssh_port: int | None = None
    vnc_port: int | None = None
    qmp_port: int | None = None
    pid: int | None = None
    accel: str | None = None
    display: str | None = None
    ssh_enabled: bool | None = None
    iso: str | None = None
    image_id: str | None = None
    #: The user's own cloud-config, as supplied. Null when none was given.
    user_data: str | None = None
    project_id: str | None = None
    network_id: str | None = None
    guest_os: GuestOS = GuestOS.LINUX
    #: Hypervisor build that last launched this instance. Null until it has been
    #: launched once, and on rows that predate the field.
    qemu_version: str | None = None
    #: False when the process is alive but its QMP monitor did not answer.
    #: Read by `degraded` below, so it has to travel on the response schema
    #: as well as the table.
    monitor_reachable: bool | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def console_supported(self) -> bool:
        """Whether a console can be *opened* for this instance.

        Named for what it actually promises. It does not say a picture will
        appear: every accelerator/display combination *can* render — an ISO
        guest that does a KMS modeset draws fine on std VGA under WHPX — but
        the one combination that may come up blank (a guest that stays in VGA
        text mode under WHPX) cannot be predicted from here, because it depends
        on what the guest does after boot. :attr:`console_caveat` warns about
        that instead of hiding the action.
        """
        if self.engine != "qemu":
            return False
        # An unresolved accelerator means the VM hasn't launched yet, so there
        # is no VNC socket to connect to.
        return self.accel is not None and self.accel != "auto"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def degraded(self) -> bool:
        """Running, but not actually reachable the way it promised to be.

        A green "Running" on an instance you cannot SSH into is the dashboard
        lying. This catches the case where a VM booted but never published an
        address — deliberately *not* applied to console-only instances, which
        are working exactly as designed with no address at all.
        """
        if self.status is not InstanceStatus.RUNNING:
            return False
        # A live process whose monitor will not answer is degraded immediately,
        # with no grace period: unlike a missing address this is not a state a
        # healthy VM passes through on the way up. The console needs that
        # socket, and so does a graceful stop.
        if self.monitor_reachable is False:
            return True
        if not self.ssh_enabled or self.ip_address:
            return False

        from kurukuru.config import get_settings

        # updated_at is the right clock: a stuck row stops being written, so it
        # holds the moment the instance went Running.
        age = (_utcnow() - self.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
        return age > get_settings().degraded_after_seconds

    @computed_field  # type: ignore[prop-decorator]
    @property
    def degraded_reason(self) -> str | None:
        if not self.degraded:
            return None
        if self.monitor_reachable is False:
            return (
                "Running, but its QMP control socket is not answering. The VM "
                "process is alive — this is not a stopped instance — but the "
                "console and a graceful stop both need that socket. QEMU serves "
                "one QMP client at a time, so the usual cause is something else "
                "already connected to it."
            )
        return (
            "Running but no address has appeared. cloud-init may have failed, or "
            "the guest's network did not come up — check the console."
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def console_caveat(self) -> str | None:
        """Advisory for the combination that can legitimately render blank."""
        if self.engine != "qemu":
            return None
        from kurukuru.engines.qemu import QemuEngine

        return QemuEngine.console_caveat(self.accel, self.display)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ssh_user(self) -> str:
        """Default VM login user (from settings) — derived, never stored per-row."""
        # Imported lazily to avoid a config <-> models import cycle at load time.
        from kurukuru.config import get_settings

        return get_settings().default_vm_user

# --------------------------------------------------------------------------- #
# Authentication (Phase 15)
# --------------------------------------------------------------------------- #
#: Every credential carries the ``credential_version`` of the user it was issued
#: for. Changing a password bumps that number, which invalidates every session
#: and every API token in one write — no sweep, no chance of missing one, and no
#: window where a stolen cookie outlives the password it was obtained with.
#: Comparing it is the whole mechanism; see :func:`kurukuru.auth.resolve_principal`.


class User(SQLModel, table=True):
    """One account. Every account is equal — this phase has no roles.

    ``is_owner`` marks the account created by the first-run flow. It confers no
    extra permission today and exists so a later authorization phase has
    something to attach one to, and so the reset procedure can name an account
    that is guaranteed to exist.
    """

    __tablename__ = "users"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    username: str = Field(index=True, unique=True, min_length=1, max_length=64)
    #: argon2id. Never a bare hash — a password is low-entropy by nature and the
    #: KDF is what makes an offline guess expensive.
    password_hash: str
    #: Bumped on every password change. See the note above this class.
    credential_version: int = Field(default=1)
    is_owner: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Session(SQLModel, table=True):
    """A browser session, stored server-side rather than signed into a cookie.

    A signed stateless cookie would avoid this table, and would also make
    "log out" and "revoke everything" impossible to honour before expiry. The
    cookie carries an opaque secret; this row is what it means.
    """

    __tablename__ = "sessions"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    #: SHA-256 of the cookie secret. Not argon2: the secret is 32 random bytes,
    #: so there is no dictionary to slow down, and this is verified on *every*
    #: request — a KDF here would be a self-inflicted rate limit.
    token_hash: str = Field(index=True, unique=True)
    #: Paired with the session and required on state-changing requests. Stored
    #: rather than derived so that revoking the session revokes it too.
    csrf_token: str
    credential_version: int = Field(default=1)
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    last_seen_at: datetime = Field(default_factory=_utcnow)


class ApiToken(SQLModel, table=True):
    """A long-lived credential for the CLI and scripts.

    Shown once at creation and stored hashed, so the database is not a list of
    working credentials. ``prefix`` exists purely so a human can tell two tokens
    apart in ``auth token ls`` without the product having to keep the secret.
    """

    __tablename__ = "api_tokens"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    name: str = Field(min_length=1, max_length=64)
    token_hash: str = Field(index=True, unique=True)
    #: First few characters of the secret, for display only.
    prefix: str = Field(max_length=16)
    credential_version: int = Field(default=1)
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime | None = Field(default=None)
    #: Set rather than deleted, so a revoked token stays visible in the list
    #: long enough for a human to confirm they revoked the right one.
    revoked_at: datetime | None = Field(default=None)


class ConsoleTicket(SQLModel, table=True):
    """A single-use, short-lived permit to open one instance's console.

    Browsers cannot set an Authorization header on a WebSocket handshake, and
    the alternatives are all worse: a cookie alone would make the console
    reachable by any same-site page (every localhost port is same-site), and a
    token in the query string writes a credential into logs and history.

    So the console is authorised out of band: mint over authenticated HTTP,
    redeem once on connect. The ticket is bound to **both** the instance and the
    session that minted it, so a ticket for one VM cannot open another, and a
    ticket outlives neither its single use nor its session.
    """

    __tablename__ = "console_tickets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    instance_id: str = Field(index=True)
    #: The session that minted it, when a browser did. None when a token did —
    #: a token has no session row to die with.
    session_id: str | None = Field(default=None, index=True)
    #: Snapshot of the user's credential version, so a password change kills
    #: outstanding tickets too. Carried on the ticket rather than looked up
    #: through the session, because a token-minted ticket has no session.
    credential_version: int = Field(default=1)
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    #: Stamped on redemption. Presence is what makes it single-use.
    used_at: datetime | None = Field(default=None)


class LoginRequest(SQLModel):
    """Body for POST /auth/login."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class FirstRunRequest(SQLModel):
    """Body for POST /auth/first-run — creating the owner account.

    Validates the password the same way every other path does, through
    :func:`validate_password`, so the web setup form and ``auth init`` cannot
    end up enforcing different rules. A minimum that applies in a terminal but
    not in a browser would be no minimum.
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password(value)


class PasswordChange(SQLModel):
    """Body for POST /auth/password."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return validate_password(v)


class UserRead(SQLModel):
    """Response schema for the signed-in account. Never carries the hash."""

    id: str
    username: str
    is_owner: bool
    created_at: datetime


class ApiTokenCreate(SQLModel):
    name: str = Field(min_length=1, max_length=64)


class ApiTokenRead(SQLModel):
    """A token as listed. The secret is absent by construction."""

    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiTokenCreated(ApiTokenRead):
    """The one response that carries the secret, returned once at creation."""

    token: str


class ConsoleTicketRead(SQLModel):
    ticket: str
    expires_at: datetime
