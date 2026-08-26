"""
Serving the built dashboard from the backend, on one origin and one port.

In development there are two servers: uvicorn holds the API and Vite holds the
dashboard, on different ports, and the browser talks to both. Shipped, that is
not something to ask a user to run. This module makes the backend serve the
built frontend itself, so the whole product is one process listening on one
port.

**The hard part is not the files, it is the URLs.** The dashboard has a
client-side route per page and the API had a route with the identical path and
method for each one: ``/images``, ``/isos``, ``/volumes``, ``/networks``,
``/keypairs``, ``/projects``, ``/instances``, ``/settings`` — eight exact
collisions, plus ``/instances/{id}``, which is both a dashboard deep link and
an API resource. On two origins that was invisible. On one it is a direct
conflict, and the only way to serve both from the same path is to branch on the
request's ``Accept`` header — which means a tool that does not set one gets
whichever answer the branch happens to prefer, silently. So the API moved to
``API_PREFIX`` and the dashboard kept the readable paths, because those are the
ones a person types.

What is left here is the rule every single-page app needs: a URL like
``/instances/abc123`` is meaningful to the dashboard's router and is not a file
on disk, so a browser asking for it directly — a bookmark, a refresh, a pasted
link — has to be answered with ``index.html`` and allowed to route itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from kurukuru.product import API_PREFIX

logger = logging.getLogger("kurukuru.dashboard")

#: Where the built dashboard lands relative to the repo, and where it lands
#: relative to a frozen application. Both are checked, in this order, because
#: the answer differs between a developer's checkout and an installed copy and
#: neither should have to be configured for the common case.
_CANDIDATES = (
    # A frozen build: the bundle sits beside the executable's data root.
    Path(__file__).resolve().parent / "dashboard",
    # A source checkout: backend/kurukuru/ -> repo root -> frontend/dist.
    Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",
)

#: Files Vite fingerprints with a content hash. The hash *is* the cache key, so
#: these can be cached hard and permanently — a rebuild produces new names, and
#: a stale name is never reused for different bytes.
_IMMUTABLE_DIR = "assets"

#: ``index.html`` must never be cached. It is the file that names the current
#: hashed bundles, so a cached copy pins the browser to the previous build's
#: assets — which, after an upgrade, are the files that no longer exist.
_HTML_CACHE_CONTROL = "no-cache, no-store, must-revalidate"
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def find_dashboard(explicit: str | None = None) -> Path | None:
    """The directory holding a built dashboard, or None if there isn't one.

    None is a supported state, not a failure: a developer running the API
    against the Vite dev server has no ``dist/`` and does not want one, and the
    backend must start and serve the API perfectly well without it.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if (candidate / "index.html").is_file():
            return candidate
        logger.warning(
            "No index.html under %s, so no dashboard is being served. "
            "The API is unaffected.", candidate,
        )
        return None

    for candidate in _CANDIDATES:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _safe_file(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``root``, or None if it escapes or is absent.

    The traversal check is a *resolved-path containment* check rather than a
    scan for ``..``. Encoded separators, symlinks and Windows' several spellings
    of the same path all defeat the scan; none of them defeat comparing the
    fully resolved result against the fully resolved root. Same approach as
    ``kurukuru.isos``, for the same reason.
    """
    try:
        resolved = (root / relative).resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def mount_dashboard(app: FastAPI, directory: str | None = None) -> Path | None:
    """Serve a built dashboard from ``app``, if there is one. Returns its path.

    **Registered last, deliberately.** The catch-all below matches any path, so
    it is only correct where every route that means something else has already
    had its chance. Starlette matches in registration order, so "last" is the
    whole guarantee — mounting this before the API router would shadow the
    entire API with an HTML page.
    """
    root = find_dashboard(directory)
    if root is None:
        logger.info(
            "No built dashboard found, so the API is serving alone. Run "
            "`npm run build` in frontend/, or use the Vite dev server."
        )
        return None

    index = root / "index.html"

    # GET and HEAD. Starlette does not add HEAD for you on an API route, and a
    # static server that answers 405 to it is wrong in a way that shows up in
    # odd places: `curl -I`, proxies revalidating a cached asset, and link
    # checkers all use HEAD, and all of them would report the dashboard broken.
    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def dashboard(full_path: str, request: Request) -> Response:  # noqa: ARG001
        # An unmatched path under the API prefix is a 404 in the API, not an
        # invitation to render the dashboard. Returning HTML here would hand a
        # misspelled endpoint to a JSON client, which then fails at the parse
        # rather than at the request, one stack frame away from the mistake.
        if full_path == API_PREFIX.lstrip("/") or full_path.startswith(
            API_PREFIX.lstrip("/") + "/"
        ):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        asset = _safe_file(root, full_path) if full_path else None
        if asset is not None:
            immutable = full_path.startswith(f"{_IMMUTABLE_DIR}/")
            return FileResponse(
                asset,
                headers={
                    "Cache-Control": _IMMUTABLE_CACHE_CONTROL if immutable
                    else _HTML_CACHE_CONTROL
                },
            )

        # Anything else is a dashboard route. `/instances/abc123` is not a file
        # and never will be; the app resolves it in the browser.
        return FileResponse(index, headers={"Cache-Control": _HTML_CACHE_CONTROL})

    logger.info("Serving the dashboard from %s", root)
    return root
