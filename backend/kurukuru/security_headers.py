"""Response headers that limit what a page can be made to do.

**Why a loopback-only tool needs these at all.** The usual answer — "there is no
network, so there is no attacker" — is wrong in one specific way: a browser will
send requests to ``127.0.0.1`` from *any* page the user has open, and it will
attach the session cookie while doing it. So every page on the internet is,
loosely, a client of this server. That is the threat these headers address, and
it is the same one behind the path-containment fix in :mod:`kurukuru.safe_paths`.

Four headers, and the reasoning differs for each:

``X-Frame-Options: DENY``
    The one that matters most. Without it a hostile page can put the dashboard
    in an invisible iframe, position it under something the user is going to
    click, and have the click land on a real control — authenticated, because
    the cookie rides along. "Terminate" is the concrete version. There is no
    legitimate reason to frame this application, so the answer is never.

``X-Content-Type-Options: nosniff``
    Stops a browser from second-guessing a declared content type. The dashboard
    serves user-influenced bytes — an imported image's name, a cloud-init blob —
    and sniffing is how a response that says ``application/json`` gets executed
    as something else.

``Referrer-Policy: no-referrer``
    Stricter than the usual ``strict-origin-when-cross-origin`` on purpose. URLs
    here contain instance UUIDs, and there is no outbound navigation that has
    any business carrying one.

``Content-Security-Policy``
    Defence in depth against injected script. It cannot be a fixed string,
    because ``index.html`` contains one deliberate inline script — the theme
    bootstrap, which must be inline and blocking or the page paints the wrong
    theme and repaints. A policy without a hash for it would reintroduce exactly
    the flash that script exists to prevent, so the hash is computed *from the
    file being served*, at startup. That is deliberately not a build step: a
    build step can be skipped, and the failure mode is a white flash on every
    load that nobody connects back to a security header.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("kurukuru.security")

#: Inline ``<script>`` blocks — those with no ``src``. Deliberately narrow: it
#: matches the one shape the build actually emits rather than trying to be an
#: HTML parser, and if the build ever emits something this misses, the policy is
#: too *strict* and the dashboard fails loudly in the console. That is the right
#: way round for a security default to be wrong.
_INLINE_SCRIPT = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)

_STATIC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def inline_script_hashes(html: Path) -> list[str]:
    """``'sha256-…'`` for every inline script in ``html``, for use in a CSP.

    The hash covers the exact bytes between the tags, which is what the CSP
    specification hashes — one stray space and the browser refuses the script.
    """
    try:
        source = html.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read %s to hash its inline scripts", html)
        return []

    hashes = []
    for body in _INLINE_SCRIPT.findall(source):
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return hashes


def build_csp(script_hashes: list[str]) -> str:
    """The policy, with whatever inline scripts the served page actually has."""
    script_src = " ".join(["'self'", *script_hashes])
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script_src}",
            # Tailwind ships a stylesheet, but the console viewer sets element
            # styles from JavaScript, which counts as inline style. Narrowing
            # this further is possible and is not worth breaking the console
            # over on a release; script is where injection actually matters.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            # Same-origin only, which covers the console's WebSocket: 'self'
            # matches the ws:// form of the page's own origin.
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "form-action 'self'",
        ]
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the headers above to every response.

    Applied to API responses as well as HTML. A JSON body cannot be framed or
    sniffed into much, but a header that is present everywhere cannot be absent
    from the one route that turns out to matter.
    """

    def __init__(self, app: ASGIApp, dashboard_root: Path | None = None) -> None:
        super().__init__(app)
        hashes = (
            inline_script_hashes(dashboard_root / "index.html")
            if dashboard_root is not None
            else []
        )
        if dashboard_root is not None and not hashes:
            logger.warning(
                "No inline scripts found in the dashboard's index.html. If the "
                "page renders blank or flashes the wrong theme, the policy is "
                "refusing a script it should have hashed."
            )
        self._headers = {**_STATIC_HEADERS, "Content-Security-Policy": build_csp(hashes)}

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        response = await call_next(request)
        for name, value in self._headers.items():
            response.headers.setdefault(name, value)
        return response
