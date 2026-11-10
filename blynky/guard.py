"""Refuse the requests a web page could make through the owner's browser.

Blynky trusts whoever can reach it, so a browser that can reach it must not
become a way in for some other site the owner has open. Three checks do that:

- The Host header must be a name this server is known by, or an address.
  Without that a page can point a DNS name it controls at 127.0.0.1 (DNS
  rebinding) and read everything here as if it were its own.
- A request that changes something, and the live WebSocket, must come from
  this server's own page. A browser always says where a request comes from
  (Origin, Sec-Fetch-Site); curl and the healthcheck say nothing, and pass.
- A request body must be JSON. A form on another site can send text/plain
  without asking, but never application/json.

It also keeps /media/ to the pictures and clips: no dotfile and no JSON from
there, whatever lands in the folders it serves.
"""

import ipaddress
import logging
from urllib.parse import urlsplit

from aiohttp import web

from . import config

_LOGGER = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_LOGGED = 100


def _split_host(value):
    """Return the hostname of a Host header value, lowercased, port dropped."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _is_address(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def known_hosts():
    """Return the names this server answers to, besides addresses."""
    names = {"localhost"} | config.ALLOWED_HOSTS
    if config.BLYNKY_URL:
        name = urlsplit(config.BLYNKY_URL).hostname
        if name:
            names.add(name.lower().rstrip("."))
    return names


class Guard:
    """The checks above, as one aiohttp middleware."""

    def __init__(self):
        """Read the allowed names once; they only change with a restart."""
        self.hosts = known_hosts()
        self.open = "*" in self.hosts
        self._logged = set()

    def host_allowed(self, header):
        """Whether a Host header names this server."""
        if self.open or header is None:
            # No Host at all is HTTP/1.0 or a hand-made request: never a
            # browser, so never rebinding.
            return True
        host = _split_host(header)
        return _is_address(host) or host in self.hosts or host.endswith(".localhost")

    @staticmethod
    def same_origin(request):
        """Whether a browser request came from this server's own page."""
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return False
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        parts = urlsplit(origin)
        if not parts.scheme or not parts.netloc:
            # "null", from a sandboxed frame or a file:// page.
            return False
        return parts.netloc.lower() == (request.headers.get("Host") or "").lower()

    def _refuse_host(self, header):
        host = _split_host(header)
        if host not in self._logged and len(self._logged) < _MAX_LOGGED:
            self._logged.add(host)
            _LOGGER.warning(
                "refused a request for %r: add it to ALLOWED_HOSTS if that is "
                "how this server is reached",
                host,
            )
        raise web.HTTPMisdirectedRequest(
            text=(
                f"Blynky does not answer to {host!r}. If that is how you reach "
                f"it, add ALLOWED_HOSTS={host} to .env (or set BLYNKY_URL to "
                "that address) and restart.\n"
            )
        )

    @web.middleware
    async def middleware(self, request, handler):
        """Apply the checks, then hand the request on."""
        if not self.host_allowed(request.headers.get("Host")):
            self._refuse_host(request.headers.get("Host"))

        websocket = request.headers.get("Upgrade", "").lower() == "websocket"
        if (request.method not in _SAFE_METHODS or websocket) and not (
            self.same_origin(request)
        ):
            raise web.HTTPForbidden(text="Cross-site request refused.\n")

        if (
            request.method not in _SAFE_METHODS
            and request.body_exists
            and request.content_type != "application/json"
        ):
            raise web.HTTPUnsupportedMediaType(
                text="Request bodies must be application/json.\n"
            )

        if request.path.startswith("/media/"):
            parts = request.path.lower().split("/")
            if any(part.startswith(".") or part.endswith(".json") for part in parts):
                raise web.HTTPNotFound()

        return await handler(request)


async def security_headers(request, response):
    """Tell the browser to hold this page to its own origin.

    The page runs no inline script and loads nothing from elsewhere, so the
    policy can be strict. connect-src names the WebSocket explicitly: older
    Safari does not count ws:// as 'self'. The Host is safe to repeat here,
    having passed the check above.
    """
    host = request.headers.get("Host") or ""
    connect = "'self'"
    if host and all(c.isalnum() or c in ".-:[]" for c in host):
        connect += f" ws://{host} wss://{host}"
    headers = response.headers
    headers["Server"] = "Blynky"
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; "
        f"media-src 'self' blob:; connect-src {connect}; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'",
    )
