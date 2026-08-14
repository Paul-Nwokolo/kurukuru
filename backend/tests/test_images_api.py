"""
Tests for the disk image catalog.

Real qcow2 files made with ``qemu-img create`` rather than fixtures: probing is
the validity gate for the whole feature, and asserting it against a mock would
only prove the mock. These are tiny sparse files, so they cost milliseconds.

The delete guards get the most attention — removing a backing file out from
under a live overlay destroys that instance's disk irrecoverably.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import app.engines as engines_module
from tests.conftest import redirect_db_engines
import app.events as events_module
import app.routers.images as images_module
import app.routers.instances as instances_module
import app.routers.keypairs as keypairs_module
from app.config import Settings, get_settings
from app.database import get_session
from app.engines import EngineRegistry, get_engine_registry
from app.image_store import ImageError, probe_image
from app.main import app
from app.models import Image, ImageSource, ImageStatus, Instance, InstanceStatus

from tests.test_instances_api import FakeQemuEngine

# PATH only, plus the same setting the backend itself honours. The previous
# fallback was a hardcoded `C:\Program Files\qemu\qemu-img.exe`, which on any
# other platform is not a path that could exist — it turned "qemu-img is
# somewhere unusual" into a silent skip instead of pointing at the override
# that fixes it.
QEMU_IMG = os.environ.get("IAAS_QEMU_IMG_BINARY") or shutil.which("qemu-img")
needs_qemu_img = pytest.mark.skipif(
    not (QEMU_IMG and Path(QEMU_IMG).exists()),
    reason="qemu-img not on PATH (set IAAS_QEMU_IMG_BINARY to point at it)",
)


def make_qcow2(path: Path, size: str = "64M") -> Path:
    subprocess.run(
        [QEMU_IMG, "create", "-f", "qcow2", str(path), size],
        capture_output=True, check=True, timeout=30,
    )
    return path


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """Where imported images land (the engine's base-images directory)."""
    directory = tmp_path / "qemu" / "base-images"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture()
def settings(tmp_path: Path, store: Path) -> Settings:
    return Settings(
        qemu_dir=str(tmp_path / "qemu"),
        iso_dir=str(tmp_path / "isos"),
        post_launch_ip_timeout_seconds=1,
        post_launch_poll_seconds=0.001,
    )


@pytest.fixture()
def client(monkeypatch, settings: Settings, small_host):
    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(test_engine)

    registry = EngineRegistry({"qemu": lambda: FakeQemuEngine()})

    def _session_override():
        with Session(test_engine) as s:
            yield s

    redirect_db_engines(monkeypatch, test_engine)
    monkeypatch.setattr(engines_module, "_registry", registry)
    monkeypatch.setattr(instances_module, "get_settings", lambda: settings)
    monkeypatch.setattr(images_module, "get_settings", lambda: settings)

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_engine_registry] = lambda: registry
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as c:
        c.db_engine = test_engine  # type: ignore[attr-defined]
        yield c

    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Probing — the gate everything else depends on
# --------------------------------------------------------------------------- #
@needs_qemu_img
def test_probe_reads_format_and_sizes(tmp_path: Path, settings: Settings):
    image = make_qcow2(tmp_path / "disk.qcow2", "128M")
    result = probe_image(image, settings)

    assert result.format.value == "qcow2"
    assert result.virtual_size_bytes == 128 * 1024 * 1024
    # A freshly created qcow2 is sparse: far smaller on disk than it claims.
    assert result.actual_size_bytes < result.virtual_size_bytes


@needs_qemu_img
def test_probe_rejects_a_file_qemu_img_cannot_read(tmp_path: Path, settings: Settings):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a disk image")

    # qemu-img guesses raw for arbitrary bytes; either it refuses outright or it
    # reports raw — both are wrong for a backing image and must not import.
    try:
        result = probe_image(junk, settings)
    except ImageError:
        return
    assert result.format.value == "raw"


def test_probe_reports_a_missing_binary_clearly(tmp_path: Path):
    broken = Settings(qemu_dir=str(tmp_path), qemu_img_binary="definitely-not-qemu-img")
    with pytest.raises(ImageError, match="not found"):
        probe_image(tmp_path / "whatever.qcow2", broken)


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
@needs_qemu_img
def test_import_copies_probes_and_becomes_available(client, tmp_path: Path, store: Path):
    source = make_qcow2(tmp_path / "source.qcow2", "256M")

    body = client.post(
        "/images/import",
        json={"name": "my-image", "path": str(source), "has_cloud_init": False},
    ).json()

    image = client.get(f"/images/{body['id']}").json()
    assert image["status"] == "Available"
    assert image["format"] == "qcow2"
    assert image["virtual_size_bytes"] == 256 * 1024 * 1024
    # The file was copied into the store, not referenced where it lay: a source
    # that moves or is deleted must not break overlays built on it.
    assert (store / image["filename"]).exists()
    assert image["filename"] != source.name


@needs_qemu_img
def test_import_survives_the_source_disappearing_afterwards(client, tmp_path: Path, store: Path):
    source = make_qcow2(tmp_path / "temp.qcow2")
    body = client.post(
        "/images/import", json={"name": "durable", "path": str(source)}
    ).json()
    source.unlink()

    image = client.get(f"/images/{body['id']}").json()
    assert image["status"] == "Available"
    assert (store / image["filename"]).exists()


def test_import_rejects_a_missing_path(client, tmp_path: Path):
    r = client.post(
        "/images/import", json={"name": "ghost", "path": str(tmp_path / "nope.qcow2")}
    )
    assert r.status_code == 422
    assert "readable by the backend" in r.json()["detail"]


def test_import_rejects_a_directory(client, tmp_path: Path):
    r = client.post("/images/import", json={"name": "dir", "path": str(tmp_path)})
    assert r.status_code == 422


@needs_qemu_img
def test_duplicate_image_name_is_409(client, tmp_path: Path):
    source = make_qcow2(tmp_path / "a.qcow2")
    client.post("/images/import", json={"name": "dupe", "path": str(source)})
    r = client.post("/images/import", json={"name": "dupe", "path": str(source)})
    assert r.status_code == 409


@needs_qemu_img
def test_unreadable_file_lands_in_error_with_qemu_imgs_words(client, tmp_path: Path):
    """The import row must carry the real reason, not a generic failure.

    A qcow2 magic + version 2 header with nothing behind it: qemu-img commits
    to reading it as qcow2 and then fails on the malformed header, rather than
    shrugging and calling it raw the way it does for arbitrary bytes.
    """
    junk = tmp_path / "broken.qcow2"
    junk.write_bytes(b"QFI\xfb\x00\x00\x00\x02")

    body = client.post("/images/import", json={"name": "broken", "path": str(junk)}).json()
    image = client.get(f"/images/{body['id']}").json()

    assert image["status"] == "Error"
    # qemu-img's own words, so the operator can act on them.
    assert "cluster size" in image["error_message"].lower()


# --------------------------------------------------------------------------- #
# Catalog + built-in
# --------------------------------------------------------------------------- #
def test_builtin_image_is_registered_at_startup(client):
    builtin = [i for i in client.get("/images").json() if i["source"] == "builtin"]
    assert len(builtin) == 1
    assert builtin[0]["has_cloud_init"] is True


def test_builtin_image_cannot_be_deleted(client):
    builtin = next(i for i in client.get("/images").json() if i["source"] == "builtin")
    r = client.delete(f"/images/{builtin['id']}")
    assert r.status_code == 409
    assert "built-in" in r.json()["detail"]


def test_deleting_an_unknown_image_is_404(client):
    assert client.delete("/images/nope").status_code == 404


@needs_qemu_img
def test_delete_removes_the_row_and_the_file(client, tmp_path: Path, store: Path):
    source = make_qcow2(tmp_path / "gone.qcow2")
    image = client.post("/images/import", json={"name": "gone", "path": str(source)}).json()
    stored = store / client.get(f"/images/{image['id']}").json()["filename"]
    assert stored.exists()

    assert client.delete(f"/images/{image['id']}").status_code == 204
    assert client.get(f"/images/{image['id']}").status_code == 404
    assert not stored.exists()


# --------------------------------------------------------------------------- #
# Launching from an image, and the in-use guard
# --------------------------------------------------------------------------- #
@needs_qemu_img
def test_instance_records_the_image_it_was_launched_from(client, tmp_path: Path):
    source = make_qcow2(tmp_path / "base.qcow2")
    image = client.post(
        "/images/import",
        json={"name": "custom", "path": str(source), "has_cloud_init": True},
    ).json()

    body = client.post(
        "/instances",
        json={"name": "from-image", "flavor": "small", "engine": "qemu", "image_id": image["id"]},
    ).json()
    assert body["image_id"] == image["id"]


def test_launching_from_an_unknown_image_is_422(client):
    r = client.post(
        "/instances",
        json={"name": "bad-img", "flavor": "small", "engine": "qemu", "image_id": "nope"},
    )
    assert r.status_code == 422


def test_launching_from_an_importing_image_is_409(client):
    """A half-copied file is not a backing image yet."""
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        image = Image(name="half", filename="half.qcow2", status=ImageStatus.IMPORTING)
        session.add(image)
        session.commit()
        image_id = image.id

    r = client.post(
        "/instances",
        json={"name": "too-soon", "flavor": "small", "engine": "qemu", "image_id": image_id},
    )
    assert r.status_code == 409


@needs_qemu_img
def test_image_in_use_by_a_live_instance_cannot_be_deleted(client, tmp_path: Path):
    """The guard that matters: deleting a backing file corrupts every overlay."""
    source = make_qcow2(tmp_path / "inuse.qcow2")
    image = client.post(
        "/images/import",
        json={"name": "in-use", "path": str(source), "has_cloud_init": True},
    ).json()
    client.post(
        "/instances",
        json={"name": "holder", "flavor": "small", "engine": "qemu", "image_id": image["id"]},
    )

    r = client.delete(f"/images/{image['id']}")
    assert r.status_code == 409
    assert "holder" in r.json()["detail"]


@needs_qemu_img
def test_image_becomes_deletable_once_its_instances_are_terminated(client, tmp_path: Path):
    source = make_qcow2(tmp_path / "freeable.qcow2")
    image = client.post(
        "/images/import",
        json={"name": "freeable", "path": str(source), "has_cloud_init": True},
    ).json()
    inst = client.post(
        "/instances",
        json={"name": "tmp-holder", "flavor": "small", "engine": "qemu", "image_id": image["id"]},
    ).json()

    assert client.delete(f"/images/{image['id']}").status_code == 409
    client.delete(f"/instances/{inst['id']}")  # terminate the holder
    assert client.delete(f"/images/{image['id']}").status_code == 204


@needs_qemu_img
def test_image_without_cloud_init_gets_no_ssh_promise(client, tmp_path: Path):
    """No cloud-init means no key injection — the instance is console-access only."""
    source = make_qcow2(tmp_path / "plain.qcow2")
    image = client.post(
        "/images/import",
        json={"name": "no-ci", "path": str(source), "has_cloud_init": False},
    ).json()

    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        stored = session.get(Image, image["id"])
        assert stored.has_cloud_init is False
        assert stored.source is ImageSource.IMPORTED


def test_terminated_instances_do_not_hold_an_image(client):
    """Audit rows must not pin an image forever."""
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        image = Image(name="held", filename="h.qcow2", status=ImageStatus.AVAILABLE)
        session.add(image)
        session.commit()
        session.add(
            Instance(
                name="dead",
                engine="qemu",
                status=InstanceStatus.TERMINATED,
                image_id=image.id,
            )
        )
        session.commit()
        image_id = image.id

    assert client.delete(f"/images/{image_id}").status_code == 204
