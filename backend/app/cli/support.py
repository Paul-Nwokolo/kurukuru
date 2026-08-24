"""
Shared plumbing for the commands: context, name resolution, waiting.

Kept out of the command modules so that ``launch``, ``start``, ``stop`` and
``rm`` all agree on what "wait until it settles" means and what happens when it
doesn't.
"""

from __future__ import annotations

import time

import typer

from app.cli.client import ApiClient
from app.cli.config import CliConfig
from app.cli.errors import CliError, ExitCode
from app.cli.naming import CLI_NAME
from app.cli.output import Output

#: How often ``--wait`` asks. Two seconds matches the backend's own post-launch
#: poll: faster only adds load to a host that is busy booting a VM.
POLL_SECONDS = 2.0

#: Default ``--wait`` budget. Matches ``IAAS_QEMU_BOOT_TIMEOUT_SECONDS``, which
#: is how long the backend itself is willing to wait for a guest — timing out
#: sooner would report failure for launches that were still on track.
DEFAULT_WAIT_TIMEOUT = 600


def config_of(ctx: typer.Context) -> CliConfig:
    """The resolved configuration attached by the root callback."""
    if not isinstance(ctx.obj, CliConfig):  # pragma: no cover - wiring guard
        raise CliError("CLI context was not initialised", ExitCode.FAILURE)
    return ctx.obj


def client_of(ctx: typer.Context) -> ApiClient:
    """The API client, carrying this machine's token if there is one.

    Read here rather than inside ApiClient so that a test injecting a transport
    is not silently given the developer's real credential as well. An absent
    token is not an error: the API answers 401 and the CLI turns that into a
    sentence naming `auth login`.
    """
    from app.cli import auth_store

    stored = auth_store.load()
    return ApiClient(config_of(ctx).api_url, token=stored.token if stored else None)


def resolve_instance(
    client: ApiClient, reference: str, *, include_terminated: bool = False
) -> dict:
    """Find one instance by id or by name.

    Ids win over names, and live rows win over terminated ones. Both rules
    matter because a name identifies at most one *live* instance but any number
    of terminated ones — the audit trail keeps them, and reusing a name is
    supported. Resolving to a terminated row while its live successor exists
    would point every command at the wrong thing.
    """
    rows = client.instances(include_terminated=True)

    for row in rows:
        if row["id"] == reference:
            return row

    live = [
        row
        for row in rows
        if row["name"] == reference and row["status"] != "Terminated"
    ]
    if live:
        return live[0]

    if include_terminated:
        dead = [row for row in rows if row["name"] == reference]
        if dead:
            # Newest, so "show" after a terminate describes what was just torn
            # down rather than something from last week with the same name.
            return max(dead, key=lambda row: row.get("created_at") or "")

    raise CliError(
        f"No instance named '{reference}'.",
        ExitCode.NOT_FOUND,
        hint=f"Run '{CLI_NAME} ls --all' to see every instance, including terminated ones.",
    )


def resolve_image(client: ApiClient, reference: str) -> dict:
    """Find one image by id or exact name."""
    images = client.images()
    for image in images:
        if image["id"] == reference:
            return image
    matches = [image for image in images if image["name"] == reference]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:  # names are unique in the API, so this is belt-and-braces
        raise CliError(
            f"'{reference}' matches {len(matches)} images; use the id instead.",
            ExitCode.INVALID,
        )
    raise CliError(
        f"No image named '{reference}'.",
        ExitCode.NOT_FOUND,
        hint=f"Run '{CLI_NAME} images ls' to see the catalog.",
    )


def wait_for_instance(
    client: ApiClient,
    out: Output,
    instance_id: str,
    *,
    targets: set[str],
    timeout: int,
    label: str,
) -> dict:
    """Poll until the instance reaches one of ``targets``, or fail saying why.

    Three outcomes, three exit codes. Reaching a target is success. Landing in
    ``Error`` is a failure whose message is the row's own ``error_message`` —
    the backend already wrote down what went wrong, and paraphrasing it here
    would lose the detail. Running out of budget is its own code, because
    "still provisioning" and "broken" call for different reactions in a script.
    """
    deadline = time.monotonic() + timeout
    with out.spinner(f"{label}…") as progress:
        while True:
            instance = client.instance(instance_id)
            status = instance["status"]
            progress.update(f"{label}: {status}")

            if status in targets:
                return instance
            if status == "Error":
                raise CliError(
                    instance.get("error_message") or f"'{instance['name']}' is in the Error state.",
                    ExitCode.FAILURE,
                    hint=f"Run '{CLI_NAME} show {instance['name']}' for the full record.",
                )
            if status == "Terminated" and "Terminated" not in targets:
                raise CliError(
                    f"'{instance['name']}' was terminated while waiting.",
                    ExitCode.FAILURE,
                )

            if time.monotonic() >= deadline:
                raise CliError(
                    f"Timed out after {timeout}s waiting for "
                    f"'{instance['name']}' to reach {' or '.join(sorted(targets))} "
                    f"(last seen: {status}).",
                    ExitCode.TIMEOUT,
                    hint=(
                        "The instance may still be starting — this gave up, it did "
                        f"not stop anything. Check '{CLI_NAME} show {instance['name']}'."
                    ),
                )
            time.sleep(POLL_SECONDS)


def source_label(instance: dict, images: dict[str, str] | None = None) -> str:
    """What this instance booted from, as one short cell.

    ISO instances name their media; image-backed ones name the image when the
    catalog is to hand, because "image" alone tells the reader nothing they
    couldn't have guessed.
    """
    if instance.get("boot_source") == "iso":
        return instance.get("iso") or "iso"
    image_id = instance.get("image_id")
    if image_id and images:
        return images.get(image_id, "image")
    return "image"


def image_names(client: ApiClient) -> dict[str, str]:
    """``{image_id: name}``, or an empty map if the catalog can't be read.

    A failure here must not take down ``ls``: the image name is decoration on a
    listing whose real subject is instances.
    """
    try:
        return {image["id"]: image["name"] for image in client.images()}
    except CliError:
        return {}


