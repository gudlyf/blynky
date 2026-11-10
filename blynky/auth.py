"""The optional login: one username and password in front of the whole UI.

A session is a signed cookie and nothing else - no table of sessions to keep,
expire or lose on a restart. The cookie carries its own expiry and a random
nonce, signed with a key derived from a secret kept in DATA_DIR and from the
username and password themselves. So:

- a restart keeps everyone signed in;
- changing AUTH_USER or AUTH_PASSWORD signs every device out;
- so does deleting DATA_DIR/.session_secret.

Sessions last AUTH_SESSION_DAYS and are renewed when used in the second half
of that, so a phone that opens Blynky now and then stays signed in for good.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import config

_LOGGER = logging.getLogger(__name__)

COOKIE = "blynky_session"
_VERSION = "v1"
LOGIN_PAGE = Path(__file__).resolve().parent / "static" / "login.html"

# Reachable signed out: the login page and what it needs, and a health check
# for Docker. /static/ is the page's own code, public in the repository anyway.
_PUBLIC = {"/login", "/api/auth", "/api/auth/login", "/healthz"}
_PUBLIC_PREFIXES = ("/static/",)

# Throttling, global rather than per address: behind tailscale serve every
# browser arrives from 127.0.0.1, so there is no address to tell them apart by.
_FREE_FAILURES = 5

# 2**15 x 8 x 128 bytes = 32 MB, briefly, at start-up; maxmem leaves headroom.
_SCRYPT_N = 2**15
_SCRYPT_MEM = 64 * 1024 * 1024
_FIRST_LOCK = 30
_MAX_LOCK = 15 * 60


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _load_secret(path):
    """Return the signing secret, making it the first time."""
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
        _LOGGER.warning("%s is too short; making a new one", path)
        path.unlink()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    # Created 0600 in one step, never readable by anyone else even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _LOGGER.info("made a new session key in %s", path)
    return data


def client_address(request):
    """Return who is asking: the proxy's word for it when the proxy is local."""
    peer = request.remote or "?"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded and peer in ("127.0.0.1", "::1"):
        return forwarded.split(",")[0].strip()
    return peer


def is_https(request):
    """Whether the browser reached this server over HTTPS.

    tailscale serve and most proxies say so in X-Forwarded-Proto; the request
    itself arrives as plain HTTP on loopback.
    """
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    return request.secure or proto.lower() == "https"


class Auth:
    """Session cookies, the sign-in endpoints and the middleware that asks."""

    def __init__(
        self,
        user=None,
        password=None,
        days=None,
        secret_path=None,
        clock=time.time,
    ):
        """Take the settings from config unless given."""
        self.user = config.AUTH_USER if user is None else user
        self.password = config.AUTH_PASSWORD if password is None else password
        self.enabled = bool(self.user and self.password)
        self.lifetime = int(
            (config.AUTH_SESSION_DAYS if days is None else days) * 86400
        )
        self.secret_path = secret_path or config.SESSION_SECRET
        self.clock = clock
        self._key = None
        self._failures = 0
        self._locked_until = 0.0

    # --------------------------------------------------------------- tokens

    @property
    def key(self):
        """Return the signing key, read from disk the first time it is needed."""
        if self._key is None:
            secret = _load_secret(self.secret_path)
            # scrypt rather than a plain HMAC of the password: someone holding
            # both .session_secret and a valid cookie could otherwise test
            # passwords against the cookie at SHA-256 speed. This costs a
            # fraction of a second once per start, and the key never leaves
            # memory.
            material = f"{self.user}\0{self.password}".encode()
            self._key = hashlib.scrypt(
                material,
                salt=secret,
                n=_SCRYPT_N,
                r=8,
                p=1,
                maxmem=_SCRYPT_MEM,
                dklen=32,
            )
        return self._key

    def _sign(self, body):
        return _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())

    def issue(self):
        """Return a new session cookie value, and when it expires."""
        expiry = int(self.clock()) + self.lifetime
        body = f"{_VERSION}.{expiry}.{_b64(secrets.token_bytes(16))}"
        return f"{body}.{self._sign(body)}", expiry

    def verify(self, value):
        """Return a cookie's expiry if it is genuine and current, else None."""
        if not value or len(value) > 256:
            return None
        parts = value.split(".")
        if len(parts) != 4 or parts[0] != _VERSION:
            return None
        body = ".".join(parts[:3])
        if not hmac.compare_digest(parts[3], self._sign(body)):
            return None
        try:
            expiry = int(parts[1])
            _unb64(parts[2])
        except ValueError:
            return None
        return expiry if expiry > self.clock() else None

    def set_cookie(self, request, response):
        """Put a fresh session on the response."""
        value, _ = self.issue()
        response.set_cookie(
            COOKIE,
            value,
            max_age=self.lifetime,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=is_https(request),
        )

    @staticmethod
    def clear_cookie(response):
        """Take the session off this browser."""
        response.del_cookie(COOKIE, path="/")

    def signed_in(self, request):
        """Whether this request carries a valid session."""
        return self.verify(request.cookies.get(COOKIE)) is not None

    # ----------------------------------------------------------- throttling

    def retry_after(self):
        """Return whole seconds until a sign-in may be tried, 0 if now."""
        wait = self._locked_until - self.clock()
        return max(0, int(wait + 0.999))

    def _failed(self):
        self._failures += 1
        over = self._failures - _FREE_FAILURES
        if over >= 0:
            lock = min(_FIRST_LOCK * 2**over, _MAX_LOCK)
            self._locked_until = self.clock() + lock

    def check(self, user, password):
        """Whether these are the configured username and password.

        Both are compared every time, in constant time, so neither how long
        this takes nor what it says gives away which one was wrong.
        """
        user_ok = hmac.compare_digest(user.encode(), self.user.encode())
        password_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return user_ok and password_ok

    # ------------------------------------------------------------- handlers

    async def login_page(self, request):
        """Serve the sign-in page.

        Served signed in too: login.js asks /api/auth and goes on to ?next=
        itself. The server never redirects to a target taken from the
        request, so there is no open redirect here to get wrong; the one
        check of ?next= lives in login.js.
        """
        if not self.enabled:
            raise web.HTTPFound("/")
        return web.FileResponse(LOGIN_PAGE)

    async def status(self, request):
        """Say whether login is on and whether this browser is signed in."""
        return web.json_response(
            {"enabled": self.enabled, "signed_in": self.signed_in(request)}
        )

    async def login(self, request):
        """Check a username and password, and start a session if they match."""
        if not self.enabled:
            return web.json_response({"status": "ok"})
        wait = self.retry_after()
        if wait:
            return web.json_response(
                {
                    "status": "error",
                    "message": f"Too many attempts. Try again in {wait} seconds.",
                },
                status=429,
                headers={"Retry-After": str(wait)},
            )
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(reason="expected JSON") from None
        user = body.get("user") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(user, str) or not isinstance(password, str):
            raise web.HTTPBadRequest(reason="user and password must be strings")

        if not self.check(user.strip(), password):
            self._failed()
            # Never the password, and the address so a run of these can be
            # traced to a device.
            _LOGGER.warning(
                "failed sign-in from %s (%d in a row)",
                client_address(request),
                self._failures,
            )
            return web.json_response(
                {"status": "error", "message": "Wrong username or password."},
                status=401,
            )

        self._failures = 0
        self._locked_until = 0.0
        _LOGGER.info("signed in from %s", client_address(request))
        response = web.json_response({"status": "ok"})
        self.set_cookie(request, response)
        return response

    async def logout(self, request):
        """End this browser's session."""
        del request
        response = web.json_response({"status": "ok"})
        self.clear_cookie(response)
        return response

    @staticmethod
    async def healthz(request):
        """Answer the Docker health check, which has no session."""
        del request
        return web.Response(status=200)

    # ----------------------------------------------------------- middleware

    @staticmethod
    def _public(path):
        return path in _PUBLIC or path.startswith(_PUBLIC_PREFIXES)

    @web.middleware
    async def middleware(self, request, handler):
        """Turn away anything without a session, once login is on."""
        if not self.enabled:
            return await handler(request)

        expiry = self.verify(request.cookies.get(COOKIE))
        if expiry is None:
            if self._public(request.path):
                return await handler(request)
            if self._wants_page(request):
                # The fragment (#/event/...) is not sent here, but a browser
                # carries it across the redirect, and the login page keeps it.
                raise web.HTTPFound(f"/login?next={quote(request.path_qs, safe='')}")
            raise web.HTTPUnauthorized(
                text='{"status": "error", "message": "Sign in first.", '
                '"login": "/login"}',
                content_type="application/json",
            )

        response = await handler(request)
        # Used in the second half of its life: start it over, so a device that
        # opens Blynky now and then never meets the login page again. Here,
        # not in on_response_prepare, which runs after aiohttp has already
        # written the cookies out. A WebSocket has been sent by now and is
        # skipped; the page's next request renews instead.
        if (
            expiry - self.clock() < self.lifetime / 2
            and not response.prepared
            and response.status < 400
        ):
            self.set_cookie(request, response)
        return response

    @staticmethod
    def _wants_page(request):
        return (
            request.method == "GET"
            and "text/html" in request.headers.get("Accept", "")
            and not request.path.startswith(("/api/", "/media/", "/ws"))
        )

    def setup(self, app):
        """Add the login's routes to an app."""
        if self.enabled:
            # Now rather than at the first sign-in, so a data folder that
            # cannot be written to stops the server instead of every login.
            _ = self.key
        app.router.add_get("/login", self.login_page)
        app.router.add_get("/api/auth", self.status)
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_post("/api/auth/logout", self.logout)
        app.router.add_get("/healthz", self.healthz)
