"""
Serving the dashboard and the API from one origin.

The thing being protected here is not "static files work". It is that the
dashboard's URLs and the API's URLs stopped being the same URLs, and stay that
way. Before Phase 16 the dashboard had a client-side route per page and the API
had a route with the identical path and method for each one — eight exact
collisions. On two origins that was invisible. On one it is a conflict with no
correct resolution, so the API moved under a prefix.

The first test in this file is the one that matters: it re-derives both sets of
paths and asserts they cannot overlap. Everything else checks the mechanics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kurukuru.dashboard import find_dashboard, mount_dashboard
from kurukuru.main import app
from kurukuru.product import API_PREFIX

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ROUTER_TS = REPO_ROOT / "frontend" / "src" / "lib" / "router.ts"


def _dashboard_routes() -> set[str]:
    """The dashboard's client-side paths, read from the router it uses.

    Parsed from the frontend rather than restated here, so that a page added to
    the dashboard is covered by this file the moment it exists. A hand-kept copy
    would go stale precisely when a new page introduced a new collision, which
    is the only time it would have mattered.
    """
    source = ROUTER_TS.read_text(encoding="utf-8")
    paths = set(re.findall(r"path: '([^']+)'", source))
    # Two more the router accepts that are not in the table: `/instances` is
    # handled explicitly (the landing page is `/`), and the detail route.
    paths |= {"/instances", "/instances/{id}"}
    return paths


def _api_routes() -> set[str]:
    return {route.path for route in app.routes if getattr(route, "path", "").startswith(API_PREFIX)}


# --------------------------------------------------------------------------- #
# The guarantee
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not ROUTER_TS.is_file(), reason="frontend source not present")
def test_no_dashboard_path_can_ever_be_an_api_path():
    """The reason ``API_PREFIX`` exists, asserted rather than assumed.

    Every API route lives under the prefix and no dashboard route does, so the
    two sets are disjoint by construction. This checks the construction actually
    holds — a router registered directly on ``app`` would sit at the root, where
    the dashboard's paths are, and silently shadow a page.
    """
    dashboard = _dashboard_routes()
    api = _api_routes()

    assert dashboard, "failed to parse any dashboard routes; the check would be vacuous"
    assert api, "failed to find any API routes; the check would be vacuous"
    assert not any(path.startswith(API_PREFIX) for path in dashboard)
    assert dashboard & api == set()


def test_every_api_route_is_under_the_prefix():
    """Anything at the root would shadow a dashboard page."""
    stray = sorted(
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/")
        and not route.path.startswith(API_PREFIX)
        and route.path != "/{full_path:path}"
    )
    assert stray == [], f"routes outside {API_PREFIX}: {stray}"


@pytest.mark.skipif(not ROUTER_TS.is_file(), reason="frontend source not present")
def test_the_collision_was_real():
    """Guards the guard: strip the prefix and the two sets overlap badly.

    Without this, the test above would keep passing if the prefix were removed
    and the collision reintroduced under some other arrangement — it only proves
    the sets are disjoint, not that keeping them disjoint is doing any work.
    """
    unprefixed = {p[len(API_PREFIX):] or "/" for p in _api_routes()}
    overlap = _dashboard_routes() & unprefixed

    assert len(overlap) >= 8, (
        f"expected the historical collision to be visible, saw {sorted(overlap)}"
    )


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #
@pytest.fixture()
def built(tmp_path: Path) -> Path:
    """A directory shaped like a Vite build."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>K</title>", encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return root


@pytest.fixture()
def served(built: Path) -> TestClient:
    """A bare app with only the dashboard mounted, plus one stand-in API route."""
    application = FastAPI()

    @application.get(f"{API_PREFIX}/health")
    def health() -> dict:
        return {"status": "ok"}

    mount_dashboard(application, str(built))
    return TestClient(application)


def test_the_index_is_served_at_the_root(served: TestClient):
    response = served.get("/")
    assert response.status_code == 200
    assert "<!doctype html>" in response.text


def test_a_hashed_asset_is_served_and_cached_forever(served: TestClient):
    """Vite puts a content hash in the filename, so the name *is* the cache key.

    Caching these hard is safe for the same reason: a rebuild produces new
    names, and a name is never reused for different bytes.
    """
    response = served.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


def test_the_index_is_never_cached(served: TestClient):
    """It names the current hashed bundles.

    A cached copy pins the browser to the previous build's asset names — which,
    after an upgrade, are the files that no longer exist. The failure is a blank
    page on a working install, and a hard refresh fixes it, which is exactly the
    kind of bug users never report.
    """
    for path in ("/", "/instances/abc123"):
        assert "no-store" in served.get(path).headers["cache-control"]


@pytest.mark.parametrize(
    "path",
    ["/instances", "/instances/abc-123", "/images", "/settings", "/activity",
     "/projects/deeper/still"],
)
def test_a_deep_link_resolves_to_the_app(served: TestClient, path: str):
    """`/instances/{id}` is meaningful to the dashboard's router and is not a
    file. A bookmark, a refresh or a pasted link has to be answered with the
    app so it can route itself, which is what makes deep links work at all."""
    response = served.get(path)

    assert response.status_code == 200
    assert "<!doctype html>" in response.text


def test_an_unknown_api_path_is_a_json_404_not_the_dashboard(served: TestClient):
    """The one exception to the fallback, and it matters.

    Answering a misspelled endpoint with an HTML page hands a JSON client a
    document it will fail to parse, one stack frame away from the mistake and
    with nothing in the message about the URL being wrong.
    """
    response = served.get(f"{API_PREFIX}/nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


def test_head_is_answered_like_get(served: TestClient):
    """`curl -I`, a revalidating proxy and a link checker all use HEAD.

    Starlette does not add it to an API route automatically, so without asking
    for it the dashboard answers 405 to every one of them — which reads as the
    site being broken rather than as a missing method.
    """
    for path in ("/", "/instances/abc", "/assets/index-abc123.js"):
        head = served.head(path)
        assert head.status_code == 200, path
        assert head.headers["cache-control"] == served.get(path).headers["cache-control"]


def test_a_real_api_route_still_wins(served: TestClient):
    """The catch-all is registered last, which is the entire guarantee."""
    assert served.get(f"{API_PREFIX}/health").json() == {"status": "ok"}


def test_traversal_out_of_the_bundle_is_refused(built: Path):
    """A path that escapes the bundle must never be read.

    Asserted against the resolver directly rather than over HTTP, deliberately.
    httpx normalises `..` out of a URL before the request is ever made, so an
    end-to-end version of this test passes whether or not the guard exists —
    which is worse than no test. Checking the function is checking the thing
    that would actually be reached by a client that does not normalise.

    The refusal is `None`, which the caller turns into "serve index.html" — the
    same answer any other non-file path gets. That is intentional: a dedicated
    403 would confirm to a prober that the path resolved to something.
    """
    from kurukuru.dashboard import _safe_file

    secret = built.parent / "secret.txt"
    secret.write_text("do not serve me", encoding="utf-8")

    # Inside the bundle: resolved.
    assert _safe_file(built, "index.html") is not None
    assert _safe_file(built, "assets/index-abc123.js") is not None
    # A `..` that stays inside is fine — containment is the rule, not the spelling.
    assert _safe_file(built, "assets/../index.html") is not None

    for attempt in ("../secret.txt", "assets/../../secret.txt", "..\secret.txt"):
        assert _safe_file(built, attempt) is None, attempt


def test_a_traversal_over_http_falls_through_to_the_app(served: TestClient, built: Path):
    """And end to end, nothing leaks — whatever the client does to the path.

    Asserting only that the secret is absent is not enough, and this test used
    to do exactly that. "The response does not contain the secret" is satisfied
    by a 500, by a 404, by an empty body, and by a client that never sent the
    request — every way of *failing* passes it. The check has to say what
    happened, not only what did not.

    So: the request succeeds, and what comes back is the SPA fallback — the
    same index.html any unmatched path gets — which is the documented behaviour
    the refusal is supposed to produce. A traversal that 500s would be a leak
    of a different kind (it confirms the path resolved to something) and is now
    a failure rather than a pass.
    """
    (built.parent / "secret.txt").write_text("do not serve me", encoding="utf-8")

    for attempt in ("/../secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt"):
        response = served.get(attempt)
        assert response.status_code == 200, f"{attempt} -> {response.status_code}"
        assert "<!doctype html>" in response.text.lower(), attempt
        assert "do not serve me" not in response.text, attempt


# --------------------------------------------------------------------------- #
# Not having a dashboard is a supported state
# --------------------------------------------------------------------------- #
def test_no_bundle_means_no_catch_all(tmp_path: Path):
    """A developer running the API against the Vite dev server has no dist/.

    The API has to work perfectly well without one, and — more subtly — must not
    register a catch-all that turns every 404 into an HTML page.
    """
    application = FastAPI()

    @application.get(f"{API_PREFIX}/health")
    def health() -> dict:
        return {"status": "ok"}

    assert mount_dashboard(application, str(tmp_path / "absent")) is None

    client = TestClient(application)
    assert client.get(f"{API_PREFIX}/health").status_code == 200
    assert client.get("/anything").status_code == 404


def test_a_directory_without_an_index_is_not_a_dashboard(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    assert find_dashboard(str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# The prefix reaches the clients
# --------------------------------------------------------------------------- #
def test_the_dashboard_bundle_agrees_on_the_prefix():
    """The frontend restates the prefix; a drift would break every call.

    It cannot import the backend's constant, so the one place it spells it is
    checked against the one place the backend does.
    """
    client_ts = REPO_ROOT / "frontend" / "src" / "api" / "client.ts"
    if not client_ts.is_file():
        pytest.skip("frontend source not present")

    match = re.search(r"const API_PREFIX = '([^']+)'", client_ts.read_text(encoding="utf-8"))

    assert match, "API_PREFIX not found in src/api/client.ts"
    assert match.group(1) == API_PREFIX


def test_the_cli_appends_the_prefix_to_a_configured_origin():
    from kurukuru.cli.client import ApiClient

    assert ApiClient("http://host:7842").base_url == f"http://host:7842{API_PREFIX}"
    assert ApiClient("http://host:7842/").base_url == f"http://host:7842{API_PREFIX}"


def test_an_origin_that_already_has_the_prefix_is_not_doubled():
    """What somebody pastes out of the address bar after opening the API docs.

    Producing `/api/api/health` from it would be a 404 that blames the backend.
    """
    from kurukuru.cli.client import ApiClient

    assert ApiClient(f"http://host:7842{API_PREFIX}").base_url == (
        f"http://host:7842{API_PREFIX}"
    )


# --------------------------------------------------------------------------- #
# Finding the bundle in an installed copy
# --------------------------------------------------------------------------- #
def test_a_frozen_build_looks_beside_the_executable(monkeypatch, tmp_path: Path):
    """The candidate that only matters once the thing is actually installed.

    In a PyInstaller onedir build the package lives under ``_internal/``, so
    ``__file__`` is two directories below the .exe while the installer puts the
    dashboard *next to* the .exe. Looking only beside the package meant the
    installed application served its whole API correctly and answered every
    dashboard URL with ``{"detail": "Not Found"}`` — and the development
    checkout never showed it, because there ``frontend/dist`` is found instead.
    """
    from kurukuru import dashboard as module

    exe = tmp_path / "app" / "kurukuru.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    bundle = exe.parent / "dashboard"
    bundle.mkdir()
    (bundle / "index.html").write_text("<!doctype html>", encoding="utf-8")

    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "executable", str(exe))

    assert module._candidates()[0] == bundle
    assert module.find_dashboard() == bundle


def test_an_unfrozen_run_does_not_look_beside_the_interpreter(monkeypatch):
    """Otherwise a checkout would hunt for a dashboard next to python.exe."""
    from kurukuru import dashboard as module

    monkeypatch.setattr(module.sys, "frozen", False, raising=False)

    assert not any(
        c == Path(module.sys.executable).resolve().parent / "dashboard"
        for c in module._candidates()
    )
