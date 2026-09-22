"""What a user is told when a launch fails, and what the catalog says afterwards.

Three findings from one external install, all of which are about *reporting*
rather than about the operations themselves. Each one worked correctly and then
described itself badly enough to send the user somewhere useless:

1. A launch that failed offline reported the real cause, and the background
   reconciler overwrote it a minute later with "VM no longer exists on the
   hypervisor" — a sentence about a VM that had never existed.
2. That original cause was itself a raw ``WinError 10060`` behind a URL, with
   no plain statement of what had gone wrong or what to do instead.
3. The built-in image downloaded, an instance launched from it, and the Images
   page went on showing "Importing" with no size.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, select

from kurukuru.engines.images import BaseImageError, _download_failure_message
from kurukuru.models import (
    Image,
    ImageSource,
    ImageStatus,
    Instance,
    InstanceStatus,
)
from kurukuru.routers.instances import _apply_info
from kurukuru.engines import InstanceInfo

from tests.test_instances_api import client, anon_client, iso_dir  # noqa: F401


# --------------------------------------------------------------------------- #
# 1. The reconciler must not overwrite a cause it does not know
# --------------------------------------------------------------------------- #
#: The real message, from the real report.
_OFFLINE_CAUSE = (
    "Couldn't download the Ubuntu base image — check your internet connection."
    "\n\nDetail: https://cloud-images.ubuntu.com/... — ConnectError('[WinError "
    "10060] A connection attempt failed')"
)


def test_a_row_that_failed_before_the_hypervisor_keeps_its_cause():
    """The reported bug, reproduced at the function that caused it.

    ``_apply_info`` is handed "this VM is not on the hypervisor", which is
    true and uninformative: the launch failed before anything was handed to
    the hypervisor at all.
    """
    instance = Instance(
        name="never-existed",
        flavor="small",
        status=InstanceStatus.ERROR,
        error_message=_OFFLINE_CAUSE,
    )

    _apply_info(instance, InstanceInfo(name="never-existed", status=None, ip_address=None, exists=False))

    assert instance.error_message == _OFFLINE_CAUSE, (
        "the reconciler replaced a real provisioning failure with 'VM no "
        "longer exists on the hypervisor', which is false — it never existed"
    )
    assert instance.status is InstanceStatus.ERROR


def test_a_running_vm_that_vanishes_is_still_reported():
    """The other side, so the fix above is not just a mute button.

    A VM that *was* there and is now gone is exactly the case this message was
    written for, and it has to keep working.
    """
    instance = Instance(
        name="was-running",
        flavor="small",
        status=InstanceStatus.RUNNING,
        error_message=None,
    )

    _apply_info(instance, InstanceInfo(name="was-running", status=None, ip_address=None, exists=False))

    assert instance.status is InstanceStatus.ERROR
    assert instance.error_message == "VM no longer exists on the hypervisor"


def test_an_errored_row_with_no_message_still_gets_one():
    """An Error row carrying no cause has nothing worth preserving."""
    instance = Instance(
        name="silent-failure",
        flavor="small",
        status=InstanceStatus.ERROR,
        error_message=None,
    )

    _apply_info(instance, InstanceInfo(name="silent-failure", status=None, ip_address=None, exists=False))

    assert instance.error_message == "VM no longer exists on the hypervisor"


# --------------------------------------------------------------------------- #
# 2. The message itself
# --------------------------------------------------------------------------- #
def test_the_offline_message_leads_with_the_cause_and_keeps_the_detail():
    """Plain sentence first, route out second, raw error last.

    Order matters here and is the whole finding: the raw error was always
    present, and being present is not the same as being read.
    """
    url = "https://cloud-images.ubuntu.com/noble/current/noble-amd64.img"
    exc = httpx.ConnectError("[WinError 10060] A connection attempt failed")

    message = _download_failure_message(url, exc)
    first_line = message.splitlines()[0]

    assert first_line.startswith("Couldn't download the Ubuntu base image")
    assert "check your internet connection" in first_line
    # The way out, named as a place in the product rather than as a setting.
    assert "Images -> Add image" in message
    # And the raw error survives, because a bug report needs it.
    assert "WinError 10060" in message
    assert url in message


def test_an_http_error_is_not_described_as_a_connection_problem():
    """A 404 is not the user's network, and saying so sends them nowhere good."""
    request = httpx.Request("GET", "https://example.invalid/img")
    exc = httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )

    message = _download_failure_message("https://example.invalid/img", exc)

    assert "HTTP 404" in message
    assert "check your internet connection" not in message
    assert "Images -> Add image" in message


# --------------------------------------------------------------------------- #
# 3. The built-in image row after its file arrives
# --------------------------------------------------------------------------- #
def test_the_builtin_row_is_updated_once_its_file_is_downloaded(client, tmp_path):
    """The Images page must stop saying "Importing" once the file is there.

    The download happens inside the engine during a launch, so nothing in the
    images router ever learns it finished. This drives the real launch path
    and then asks the API what the catalog says.
    """
    import kurukuru.routers.images as images_module
    import kurukuru.routers.instances as instances_module
    from kurukuru.image_store import ProbeResult
    from kurukuru.models import ImageFormat

    settings = instances_module.get_settings()

    # The row as a fresh install has it: registered, file not yet fetched.
    with Session(client.db_engine) as session:
        session.add(
            Image(
                name="Ubuntu 24.04 LTS (cloud)",
                filename="noble-server-cloudimg-amd64.img",
                source=ImageSource.BUILTIN,
                has_cloud_init=True,
                status=ImageStatus.IMPORTING,
                error_message="Base image not downloaded yet",
            )
        )
        session.commit()

    # Stand in for the engine's download: put the file where the engine would
    # have left it, and let probing report a real size.
    from kurukuru.engines.images import base_image_path

    image_file = base_image_path(settings)
    image_file.parent.mkdir(parents=True, exist_ok=True)
    image_file.write_bytes(b"not really a qcow2, but it exists")

    def fake_probe(path, _settings=None):
        return ProbeResult(
            format=ImageFormat.QCOW2,
            virtual_size_bytes=3_758_096_384,
            actual_size_bytes=image_file.stat().st_size,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(images_module, "probe_image", fake_probe)
        client.post("/instances", json={"name": "first-launch", "flavor": "small"})

    row = next(
        item
        for item in client.get("/images").json()
        if item["source"] == "builtin"
    )
    assert row["status"] == "Available", (
        "the built-in image still reports Importing after its file was "
        "downloaded and an instance launched from it"
    )
    assert row["virtual_size_bytes"] == 3_758_096_384


def test_refreshing_the_builtin_row_is_free_when_it_is_already_available(client):
    """The common case must not re-probe on every launch.

    ``qemu-img info`` per launch is not expensive, but it is a subprocess for
    an answer that cannot have changed — the same reasoning as the engine's
    availability cache.
    """
    import kurukuru.routers.images as images_module

    with Session(client.db_engine) as session:
        session.add(
            Image(
                name="Ubuntu 24.04 LTS (cloud)",
                filename="noble-server-cloudimg-amd64.img",
                source=ImageSource.BUILTIN,
                has_cloud_init=True,
                status=ImageStatus.AVAILABLE,
                virtual_size_bytes=1,
            )
        )
        session.commit()

    probes = []

    def counting_probe(path, _settings=None):  # pragma: no cover - must not run
        probes.append(path)
        raise AssertionError("probed an image that was already Available")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(images_module, "probe_image", counting_probe)
        images_module.sync_builtin_image()

    assert probes == []
