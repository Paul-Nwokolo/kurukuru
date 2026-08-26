"""
The CLI's only route to the system: the HTTP API.

Nothing in ``kurukuru.cli`` imports a router, a model, a database session or an
engine. The CLI is a second client of the same API the dashboard uses, which is
what keeps the two honest — a capability the CLI needs and the API cannot
express is a missing endpoint, not an excuse to open the SQLite file.

Two decisions worth stating:

**The transport is injectable.** ``use_client`` swaps in any ``httpx.Client``,
and FastAPI's ``TestClient`` *is* one. Command tests therefore run against the
real application, routes, validators and all, with no server to start and no
port to race on.

**API messages are passed through verbatim.** The capacity refusals spell out
the arithmetic and the 409s name the blocking state; substituting a tidier
sentence of our own would throw away the most useful text in the system.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.naming import CLI_NAME, CSRF_HEADER, env_var

#: Reads and quick mutations. Generous enough for a loaded host, short enough
#: that a wedged backend doesn't look like a hang.
DEFAULT_TIMEOUT = 30.0

#: Operations that legitimately block for minutes: ``POST /{id}/start`` waits
#: for the guest's SSH port, and terminate waits for an ACPI shutdown. The
#: backend's own budget for these is ~600s, so the client must outlast it or it
#: would report a timeout for something that was about to succeed.
BLOCKING_TIMEOUT = 900.0

#: Status codes whose meaning the exit-code table pins down.
_STATUS_EXIT_CODES = {
    401: ExitCode.UNAUTHENTICATED,
    404: ExitCode.NOT_FOUND,
    409: ExitCode.CONFLICT,
    422: ExitCode.INVALID,
}

_injected: httpx.Client | None = None


@contextmanager
def use_client(client: httpx.Client | None) -> Iterator[None]:
    """Run the block with ``client`` as the transport for every ApiClient.

    Used by the test suite to point the CLI at an in-process FastAPI app.
    """
    global _injected
    previous = _injected
    _injected = client
    try:
        yield
    finally:
        _injected = previous


def _detail(response: httpx.Response) -> str:
    """The API's own explanation, in the shape it actually sent it.

    FastAPI reports request-validation failures as a *list* of field errors and
    application-level refusals as a plain sentence. Both arrive as ``detail``,
    so both are rendered here rather than at every call site.
    """
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip() or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if not isinstance(item, dict):
                    parts.append(str(item))
                    continue
                location = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                message = item.get("msg", "invalid value")
                parts.append(f"{location}: {message}" if location else str(message))
            if parts:
                return "; ".join(parts)
    return f"HTTP {response.status_code}"


class ApiClient:
    """Thin, typed-enough wrapper over the orchestrator's HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.Client | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or _injected
        self._owned: httpx.Client | None = None
        # Resolved once, here, rather than read from disk per call. None means
        # "send nothing" — the API answers 401 and the CLI turns that into a
        # sentence telling the user to sign in.
        self._token = token

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    def _client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        if self._owned is None:
            self._owned = httpx.Client(base_url=self.base_url, timeout=DEFAULT_TIMEOUT)
        return self._owned

    def close(self) -> None:
        if self._owned is not None:
            self._owned.close()
            self._owned = None

    def _unreachable(self, exc: Exception) -> CliError:
        return CliError(
            f"Cannot reach the API at {self.base_url} ({exc.__class__.__name__}).",
            ExitCode.UNREACHABLE,
            hint=(
                f"Is the backend running? Start it with '{CLI_NAME} serve', or point "
                f"the CLI at another host with --api-url or ${env_var('API_URL')}."
            ),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
        """One HTTP call, with transport and status failures already translated."""
        client = self._client()
        if self._token:
            # Bearer rather than a cookie: the CLI is not a browser, and a token
            # request is exempt from the CSRF check because a browser cannot be
            # tricked into attaching this header cross-origin.
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Authorization", f"Bearer {self._token}")
            kwargs["headers"] = headers
        if self._http is None:
            # Per-call budget, but only for a transport we own. An injected one
            # (FastAPI's TestClient) runs in-process with nothing to wait for,
            # and rejects the argument outright.
            kwargs["timeout"] = timeout
        try:
            response = client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.ConnectTimeout as exc:
            raise self._unreachable(exc) from exc
        except httpx.TimeoutException as exc:
            raise CliError(
                f"The API did not answer within {timeout:g}s ({method} {path}).",
                ExitCode.TIMEOUT,
                hint="The backend may be busy provisioning. Try again in a moment.",
            ) from exc
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from exc

        if response.status_code == 401:
            raise CliError(
                "Not authenticated.",
                ExitCode.UNAUTHENTICATED,
                hint=(
                    f"Run '{CLI_NAME} auth login'. If this install has no account "
                    f"yet, run '{CLI_NAME} auth init' on the machine hosting it."
                ),
            )
        if response.status_code >= 400:
            raise CliError(
                _detail(response),
                _STATUS_EXIT_CODES.get(response.status_code, ExitCode.FAILURE),
            )

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:  # a proxy or a wrong URL, not our API
            raise CliError(
                f"{method} {path} returned {response.status_code} but not JSON — "
                f"is {self.base_url} really the orchestrator?",
                ExitCode.FAILURE,
            ) from exc

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    # The CLI signs in only to mint a token, then throws the session away. It
    # keeps the token, never the password. These three calls are the only place
    # a cookie is used, so they handle the CSRF header themselves rather than
    # complicating every other request with it.
    def login(self, username: str, password: str) -> dict:
        client = self._client()
        response = client.request(
            "POST", f"{self.base_url}/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code == 401:
            raise CliError(
                "Incorrect username or password.", ExitCode.UNAUTHENTICATED,
                hint=f"Reset it on the host with '{CLI_NAME} auth reset-password'.",
            )
        if response.status_code == 429:
            raise CliError(_detail(response), ExitCode.CONFLICT,
                           hint="Too many attempts; wait and try again.")
        if response.status_code >= 400:
            raise CliError(_detail(response), ExitCode.FAILURE)
        return {"user": response.json(), "csrf": response.headers.get(CSRF_HEADER, "")}

    def create_token_with_csrf(self, csrf: str, name: str) -> dict:
        return self.request(
            "POST", "/auth/tokens", json={"name": name}, headers={CSRF_HEADER: csrf}
        )

    def logout_with_csrf(self, csrf: str) -> None:
        try:
            self.request("POST", "/auth/logout", headers={CSRF_HEADER: csrf})
        except CliError:
            # The token is already minted and stored; a session left to expire
            # is not worth failing the command over.
            pass

    # ------------------------------------------------------------------ #
    # System
    # ------------------------------------------------------------------ #
    def health(self) -> dict:
        return self.request("GET", "/health")

    def diagnostics(self) -> dict:
        return self.request("GET", "/diagnostics", timeout=BLOCKING_TIMEOUT)

    def capacity(self) -> dict:
        return self.request("GET", "/host/capacity")

    def flavors(self) -> dict:
        return self.request("GET", "/flavors")

    def isos(self) -> list[dict]:
        return self.request("GET", "/isos")

    def ssh_key(self) -> dict:
        return self.request("GET", "/ssh-key")

    # ------------------------------------------------------------------ #
    # Instances
    # ------------------------------------------------------------------ #
    def instances(
        self, *, include_terminated: bool = False, project_id: str | None = None
    ) -> list[dict]:
        params: dict[str, object] = {"include_terminated": include_terminated}
        if project_id is not None:
            params["project_id"] = project_id
        return self.request("GET", "/instances", params=params)

    # ------------------------------------------------------------------ #
    # Projects
    # ------------------------------------------------------------------ #
    def projects(self) -> list[dict]:
        return self.request("GET", "/projects")

    def create_project(self, name: str, description: str | None = None) -> dict:
        return self.request(
            "POST", "/projects", json={"name": name, "description": description}
        )

    def rename_project(self, project_id: str, name: str) -> dict:
        return self.request("PATCH", f"/projects/{project_id}", json={"name": name})

    def delete_project(self, project_id: str) -> None:
        self.request("DELETE", f"/projects/{project_id}")

    def instance(self, instance_id: str) -> dict:
        return self.request("GET", f"/instances/{instance_id}")

    def create_instance(self, payload: dict) -> dict:
        return self.request("POST", "/instances", json=payload)

    def start_instance(self, instance_id: str) -> dict:
        return self.request(
            "POST", f"/instances/{instance_id}/start", timeout=BLOCKING_TIMEOUT
        )

    def stop_instance(self, instance_id: str) -> dict:
        return self.request(
            "POST", f"/instances/{instance_id}/stop", timeout=BLOCKING_TIMEOUT
        )

    def delete_instance(self, instance_id: str, *, force: bool = False) -> dict:
        return self.request(
            "DELETE",
            f"/instances/{instance_id}",
            params={"force": force},
            timeout=BLOCKING_TIMEOUT,
        )

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def events(
        self,
        *,
        instance_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """One page of the event log, newest first.

        The per-instance route is used when an instance is named, rather than
        the global feed with a filter, so an unknown id is a 404 the CLI can
        turn into exit 4 instead of an empty table.
        """
        params: dict[str, object] = {"limit": limit}
        if kind is not None:
            params["kind"] = kind
        path = f"/instances/{instance_id}/events" if instance_id else "/events"
        return self.request("GET", path, params=params)

    # ------------------------------------------------------------------ #
    # Networks and port forwards
    # ------------------------------------------------------------------ #
    def networks(self) -> list[dict]:
        return self.request("GET", "/networks")

    def network_modes(self) -> dict:
        return self.request("GET", "/networks/modes")

    def forwards(self, instance_id: str) -> list[dict]:
        return self.request("GET", f"/instances/{instance_id}/forwards")

    def add_forward(
        self,
        instance_id: str,
        host_port: int,
        guest_port: int,
        *,
        protocol: str = "tcp",
        description: str | None = None,
    ) -> dict:
        return self.request(
            "POST",
            f"/instances/{instance_id}/forwards",
            json={
                "host_port": host_port,
                "guest_port": guest_port,
                "protocol": protocol,
                "description": description,
            },
        )

    def remove_forward(self, instance_id: str, forward_id: str) -> None:
        self.request("DELETE", f"/instances/{instance_id}/forwards/{forward_id}")

    # ------------------------------------------------------------------ #
    # Volumes
    # ------------------------------------------------------------------ #
    def volumes(self, *, project_id: str | None = None) -> list[dict]:
        params = {"project_id": project_id} if project_id else None
        return self.request("GET", "/volumes", params=params)

    def volume(self, volume_id: str) -> dict:
        return self.request("GET", f"/volumes/{volume_id}")

    def create_volume(
        self, name: str, size_gb: int, *, project_id: str | None = None
    ) -> dict:
        return self.request(
            "POST", "/volumes",
            json={"name": name, "size_gb": size_gb, "project_id": project_id},
        )

    def attach_volume(self, volume_id: str, instance_id: str) -> dict:
        return self.request(
            "POST", f"/volumes/{volume_id}/attach", json={"instance_id": instance_id}
        )

    def detach_volume(self, volume_id: str) -> dict:
        return self.request("POST", f"/volumes/{volume_id}/detach")

    def delete_volume(self, volume_id: str) -> None:
        self.request("DELETE", f"/volumes/{volume_id}")

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    def snapshots(self, instance_id: str) -> list[dict]:
        return self.request("GET", f"/instances/{instance_id}/snapshots")

    def create_snapshot(
        self, instance_id: str, name: str, description: str | None = None
    ) -> dict:
        return self.request(
            "POST",
            f"/instances/{instance_id}/snapshots",
            json={"name": name, "description": description},
        )

    def restore_snapshot(self, instance_id: str, snapshot_id: str) -> dict:
        # Fast (a qcow2 restore rewrites metadata, not data) but given the
        # blocking budget anyway: the disk may be large and the host busy.
        return self.request(
            "POST",
            f"/instances/{instance_id}/snapshots/{snapshot_id}/restore",
            timeout=BLOCKING_TIMEOUT,
        )

    def delete_snapshot(self, instance_id: str, snapshot_id: str) -> dict:
        return self.request(
            "DELETE", f"/instances/{instance_id}/snapshots/{snapshot_id}"
        )

    # ------------------------------------------------------------------ #
    # Volume snapshots
    # ------------------------------------------------------------------ #
    # Separate from the instance snapshot calls above, and not a parameterised
    # version of them: they address different resources, and collapsing the two
    # into one method taking a "kind" would make every call site say which kind
    # anyway, less legibly.
    def volume_snapshots(self, volume_id: str) -> list[dict]:
        return self.request("GET", f"/volumes/{volume_id}/snapshots")

    def create_volume_snapshot(
        self, volume_id: str, name: str, description: str | None = None
    ) -> dict:
        return self.request(
            "POST",
            f"/volumes/{volume_id}/snapshots",
            json={"name": name, "description": description},
        )

    def restore_volume_snapshot(self, volume_id: str, snapshot_id: str) -> dict:
        return self.request(
            "POST",
            f"/volumes/{volume_id}/snapshots/{snapshot_id}/restore",
            timeout=BLOCKING_TIMEOUT,
        )

    def delete_volume_snapshot(self, volume_id: str, snapshot_id: str) -> dict:
        return self.request("DELETE", f"/volumes/{volume_id}/snapshots/{snapshot_id}")

    # ------------------------------------------------------------------ #
    # Images
    # ------------------------------------------------------------------ #
    def images(self) -> list[dict]:
        return self.request("GET", "/images")

    def image(self, image_id: str) -> dict:
        return self.request("GET", f"/images/{image_id}")

    def import_image(self, name: str, path: str, *, has_cloud_init: bool) -> dict:
        return self.request(
            "POST",
            "/images/import",
            json={"name": name, "path": path, "has_cloud_init": has_cloud_init},
        )

    def delete_image(self, image_id: str) -> None:
        self.request("DELETE", f"/images/{image_id}", timeout=BLOCKING_TIMEOUT)
