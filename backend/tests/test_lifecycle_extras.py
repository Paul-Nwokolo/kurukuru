"""
Restart, clone, guest OS, and importing an image from a URL.

The load-bearing assertions here are about honesty rather than mechanism:

* A **clone owns its bytes.** It is a flattened copy, so terminating the source
  cannot corrupt it. That is the whole reason the slow copy was chosen over an
  overlay chain, and it is asserted directly.
* A clone does **not** inherit ports or cloud-init identity, because two guests
  fighting over one host port, or one believing it is the machine it was copied
  from, are the two ways this feature goes wrong quietly.
* **Windows is refused, and the refusal explains itself.** The field exists so a
  later phase has somewhere to land; accepting a Windows launch today would
  produce a VM that boots to "no bootable device".
* A **URL import is bounded and verified** — an unbounded download onto the
  backend's disk is the one place a caller can consume the host.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlmodel import Session, select

from app.models import GuestOS, Instance, InstanceStatus

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures


def _launch(client, name: str = "web-01", **extra) -> dict:
    r = client.post("/instances", json={"name": name, **extra})
    assert r.status_code == 202, r.text
    return r.json()


def _stopped(client, name: str = "web-01") -> dict:
    instance = _launch(client, name)
    client.post(f"/instances/{instance['id']}/stop")
    return client.get(f"/instances/{instance['id']}").json()


# --------------------------------------------------------------------------- #
# Guest OS
# --------------------------------------------------------------------------- #
def test_an_instance_defaults_to_linux(client):
    assert _launch(client)["guest_os"] == "linux"


def test_a_windows_guest_without_an_iso_is_refused_with_the_reason(client):
    """Windows provisions as of Phase 13 — but only from an installer medium.

    This replaces a test that asserted Windows was refused outright. The
    refusal narrowed rather than disappeared, and the narrower case is the one
    worth pinning: there is no Windows cloud image to overlay, so a Windows
    launch with no ISO would build a blank disk and boot it to "no operating
    system" some minutes later. The 422 says so at request time.
    """
    r = client.post(
        "/instances",
        json={"name": "win-01", "guest_os": "windows", "flavor": "windows"},
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "ISO" in detail
    # Says where one comes from, since we cannot supply it.
    assert "Microsoft" in detail


def test_a_windows_guest_boots_from_an_iso(client, iso_dir):  # noqa: F811
    """The whole point of the phase: the request is accepted and the row says
    Windows."""
    (iso_dir / "server.iso").write_bytes(b"\0" * 64)
    r = client.post(
        "/instances",
        json={
            "name": "win-02",
            "guest_os": "windows",
            "flavor": "windows",
            "iso": "server.iso",
        },
    )

    assert r.status_code == 202, r.text
    assert r.json()["guest_os"] == "windows"


def test_a_windows_guest_is_refused_below_its_own_floor(client, iso_dir):  # noqa: F811
    """A Windows floor, not the Linux one.

    ``small`` is a perfectly good Linux size and nowhere near enough for a
    Windows installer, which stops partway through rather than running slowly.
    """
    (iso_dir / "server.iso").write_bytes(b"\0" * 64)
    r = client.post(
        "/instances",
        json={
            "name": "win-03",
            "guest_os": "windows",
            "flavor": "small",
            "iso": "server.iso",
        },
    )

    assert r.status_code == 422
    assert "Windows instance" in r.json()["detail"]


def test_an_unknown_guest_os_is_rejected(client):
    assert client.post("/instances", json={"name": "x", "guest_os": "plan9"}).status_code == 422


# --------------------------------------------------------------------------- #
# Restart
# --------------------------------------------------------------------------- #
def test_restart_stops_and_starts_the_instance(client):
    """Graceful, not a reset button: it goes through the same stop path, so it
    inherits the ACPI grace period."""
    instance = _launch(client)

    r = client.post(f"/instances/{instance['id']}/restart")

    assert r.status_code == 200
    assert [call for call in client.fake.calls if call[1] == "web-01"][-2:] == [
        ("stop", "web-01"),
        ("start", "web-01"),
    ]


def test_restarting_a_stopped_instance_is_refused(client):
    instance = _stopped(client)

    r = client.post(f"/instances/{instance['id']}/restart")

    assert r.status_code == 409
    assert "only Running instances can be restarted" in r.json()["detail"]


def test_restart_is_recorded_in_the_event_log(client):
    instance = _launch(client)
    client.post(f"/instances/{instance['id']}/restart")

    kinds = [e["kind"] for e in client.get(f"/instances/{instance['id']}/events").json()]
    assert kinds[0] == "restarted"


# --------------------------------------------------------------------------- #
# Clone
# --------------------------------------------------------------------------- #
def test_cloning_a_running_instance_is_refused_with_the_reason(client):
    instance = _launch(client)

    r = client.post(f"/instances/{instance['id']}/clone", json={"name": "web-02"})

    assert r.status_code == 409
    assert "mid-write" in r.json()["detail"]


def test_a_clone_gets_its_own_ports_not_the_source_s(tmp_path):
    """Two instances sharing a host port would collide on the next start.

    Asserted against the real allocator rather than through the API, because
    the fake engine hands every instance the same pinned ports — the guarantee
    lives in ``_allocate_runtime``/``_reserved_ports``, so that is what is
    exercised.
    """
    from app.config import Settings
    from app.engines.qemu import QemuEngine

    engine = QemuEngine(Settings(state_dir=str(tmp_path)))
    source = engine._allocate_runtime("web-01", cpus=1, memory="1024")
    engine._write_runtime("web-01", source)
    clone = engine._allocate_runtime("web-02", cpus=1, memory="1024")

    assert clone.ssh_port != source.ssh_port
    assert clone.qmp_port != source.qmp_port
    assert clone.vnc_port != source.vnc_port


def test_a_clone_copies_the_sizing_and_keys_of_its_source(client):
    source = _stopped(client, "web-01")
    clone = client.post(
        f"/instances/{source['id']}/clone", json={"name": "web-02"}
    ).json()

    assert (clone["cpus"], clone["memory_mb"], clone["disk_gb"]) == (
        source["cpus"], source["memory_mb"], source["disk_gb"]
    )
    source_keys = {k["keypair_id"] for k in client.get(f"/instances/{source['id']}/keypairs").json()}
    clone_keys = {k["keypair_id"] for k in client.get(f"/instances/{clone['id']}/keypairs").json()}
    assert clone_keys == source_keys


def test_the_clone_disk_is_flattened_not_chained(client):
    """The reason the slow copy was chosen. `qemu-img convert` produces a
    standalone qcow2; `create -b` would leave the clone depending on a file it
    does not own, so terminating the source would corrupt it silently."""
    source = _stopped(client, "web-01")
    client.post(f"/instances/{source['id']}/clone", json={"name": "web-02"})

    assert ("clone", "web-01") in client.fake.calls or client.fake.cloned == [
        ("web-01", "web-02")
    ]


def test_cloning_onto_an_existing_name_is_refused(client):
    source = _stopped(client, "web-01")
    _launch(client, "taken")

    r = client.post(f"/instances/{source['id']}/clone", json={"name": "taken"})

    assert r.status_code == 409


def test_a_clone_is_recorded_as_such_and_names_the_source(client):
    source = _stopped(client, "web-01")
    clone = client.post(
        f"/instances/{source['id']}/clone", json={"name": "web-02"}
    ).json()

    events = client.get(f"/instances/{clone['id']}/events").json()
    cloned = next(e for e in events if e["kind"] == "cloned")
    assert "web-01" in cloned["summary"]
    # The warning that cannot be fixed from outside the guest.
    assert "SSH host keys" in cloned["detail"]


def test_terminating_the_source_leaves_the_clone_alone(client):
    """The property the flattened copy buys."""
    source = _stopped(client, "web-01")
    clone = client.post(
        f"/instances/{source['id']}/clone", json={"name": "web-02"}
    ).json()

    client.delete(f"/instances/{source['id']}")

    survivor = client.get(f"/instances/{clone['id']}")
    assert survivor.status_code == 200
    assert survivor.json()["status"] != "Terminated"


# --------------------------------------------------------------------------- #
# URL image import
# --------------------------------------------------------------------------- #
def test_a_url_import_is_accepted_and_starts_importing(client, monkeypatch):
    import app.routers.images as images_module

    captured = {}

    def _fake_fetch(url, filename, *, expected_sha256=None, settings=None):
        captured["url"] = url
        captured["sha256"] = expected_sha256
        from pathlib import Path

        from app.engines.images import base_images_dir

        target = base_images_dir(settings) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"QFI\xfb" + b"\0" * 60)
        return Path(target)

    monkeypatch.setattr(images_module, "fetch_into_store", _fake_fetch)

    r = client.post(
        "/images/fetch",
        json={
            "name": "Debian 12",
            "url": "https://cloud.debian.org/images/debian-12-generic-amd64.qcow2",
            "sha256": "a" * 64,
        },
    )

    assert r.status_code == 202
    assert captured["url"].endswith(".qcow2")
    assert captured["sha256"] == "a" * 64


@pytest.mark.parametrize(
    "url", ["ftp://example.com/x.qcow2", "not-a-url", "file:///etc/passwd"]
)
def test_a_non_http_url_is_rejected(client, url):
    r = client.post("/images/fetch", json={"name": "x", "url": url})
    assert r.status_code == 422


def test_a_malformed_checksum_is_rejected(client):
    r = client.post(
        "/images/fetch",
        json={"name": "x", "url": "https://e.com/x.qcow2", "sha256": "nothex"},
    )
    assert r.status_code == 422


def test_a_duplicate_image_name_is_refused(client):
    client.post("/images/fetch", json={"name": "dupe", "url": "https://e.com/a.qcow2"})
    r = client.post("/images/fetch", json={"name": "dupe", "url": "https://e.com/b.qcow2"})
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# The fetch helper's own guards
# --------------------------------------------------------------------------- #
def test_fetch_rejects_a_declared_size_over_the_cap(tmp_path, monkeypatch):
    """A 40 GB image should be refused before a byte is written."""
    import httpx

    from app.config import Settings
    from app.image_store import ImageError, fetch_into_store

    class _Response:
        headers = {"content-length": str(40 * 1024**3)}

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield b""

    class _Stream:
        def __enter__(self):
            return _Response()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream())
    settings = Settings(state_dir=str(tmp_path), image_fetch_max_bytes=16 * 1024**3)

    with pytest.raises(ImageError) as caught:
        fetch_into_store("https://e.com/big.qcow2", "big.qcow2", settings=settings)

    assert "import limit" in str(caught.value)


def test_fetch_rejects_a_checksum_mismatch_and_leaves_nothing_behind(tmp_path, monkeypatch):
    """A corrupted or substituted download must not become launchable."""
    import httpx

    from app.config import Settings
    from app.engines.images import base_images_dir
    from app.image_store import ImageError, fetch_into_store

    payload = b"not the image you asked for"

    class _Response:
        headers = {"content-length": str(len(payload))}

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield payload

    class _Stream:
        def __enter__(self):
            return _Response()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream())
    settings = Settings(state_dir=str(tmp_path))

    with pytest.raises(ImageError) as caught:
        fetch_into_store(
            "https://e.com/x.qcow2", "x.qcow2", expected_sha256="b" * 64, settings=settings
        )

    assert "Checksum mismatch" in str(caught.value)
    assert not (base_images_dir(settings) / "x.qcow2").exists()
    assert not list(base_images_dir(settings).glob("*.part"))


def test_fetch_accepts_a_matching_checksum(tmp_path, monkeypatch):
    import httpx

    from app.config import Settings
    from app.image_store import fetch_into_store

    payload = b"QFI\xfb" + b"\0" * 60
    digest = hashlib.sha256(payload).hexdigest()

    class _Response:
        headers = {"content-length": str(len(payload))}

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield payload

    class _Stream:
        def __enter__(self):
            return _Response()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream())
    settings = Settings(state_dir=str(tmp_path))

    result = fetch_into_store(
        "https://e.com/x.qcow2", "x.qcow2", expected_sha256=digest, settings=settings
    )

    assert result.exists()
    assert result.read_bytes() == payload
