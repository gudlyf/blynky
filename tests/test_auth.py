"""The optional login: sessions, the endpoints, and what it keeps out."""

import pytest

from blynky import auth, config

USER = "owner"
PASSWORD = "correct horse"


class Clock:
    """A clock the tests can move."""

    def __init__(self, now=1_800_000_000.0):
        """Start at a fixed time."""
        self.now = now

    def __call__(self):
        """Return the current fake time."""
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def secret(tmp_path):
    return tmp_path / ".session_secret"


@pytest.fixture
def login(clock, secret):
    return auth.Auth(USER, PASSWORD, days=365, secret_path=secret, clock=clock)


@pytest.fixture
async def client(make_client, login):
    return await make_client(login)


async def sign_in(client, user=USER, password=PASSWORD):
    return await client.post(
        "/api/auth/login", json={"user": user, "password": password}
    )


# ------------------------------------------------------------------- tokens


def test_cookie_round_trip(login, clock):
    value, expiry = login.issue()
    assert expiry == int(clock.now) + 365 * 86400
    assert login.verify(value) == expiry


def test_tampered_cookie_is_rejected(login):
    value, _ = login.issue()
    version, expiry, nonce, mac = value.split(".")
    longer = f"{version}.{int(expiry) + 999}.{nonce}.{mac}"
    assert login.verify(longer) is None
    assert login.verify(value[:-2] + "xx") is None
    for junk in ("", "v1", "v1.a.b.c", "x" * 300, "v2.1.2.3"):
        assert login.verify(junk) is None


def test_expired_cookie_is_rejected(login, clock):
    value, _ = login.issue()
    clock.now += 365 * 86400 + 1
    assert login.verify(value) is None


def test_changing_the_password_signs_everyone_out(login, clock, secret):
    value, _ = login.issue()
    changed = auth.Auth(USER, "another password", 365, secret, clock)
    assert changed.verify(value) is None


def test_deleting_the_secret_signs_everyone_out(login, clock, secret):
    value, _ = login.issue()
    secret.unlink()
    fresh = auth.Auth(USER, PASSWORD, 365, secret, clock)
    assert fresh.verify(value) is None


def test_secret_is_private(login, secret):
    login.issue()
    assert secret.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/#/event/x", "/#/event/x"),
        ("/library?q=dog", "/library?q=dog"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("", "/"),
        (None, "/"),
        ("/\r\nSet-Cookie: x", "/"),
    ],
)
def test_safe_next(value, expected):
    assert auth.safe_next(value) == expected


# -------------------------------------------------------------- signed out


async def test_page_redirects_to_login(client):
    response = await client.get(
        "/", headers={"Accept": "text/html"}, allow_redirects=False
    )
    assert response.status == 302
    assert response.headers["Location"] == "/login?next=/"


@pytest.mark.parametrize("path", ["/api/state", "/api/library", "/api/people"])
async def test_api_needs_a_session(client, path):
    response = await client.get(path)
    assert response.status == 401
    assert (await response.json())["login"] == "/login"


async def test_media_needs_a_session(client):
    folder = config.STILL_DIR / "porch"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "one.jpg").write_bytes(b"jpeg")
    assert (await client.get("/media/stills/porch/one.jpg")).status == 401


async def test_websocket_needs_a_session(client):
    from aiohttp import WSServerHandshakeError

    with pytest.raises(WSServerHandshakeError) as refused:
        await client.ws_connect("/ws")
    assert refused.value.status == 401


@pytest.mark.parametrize("path", ["/login", "/api/auth", "/healthz", "/static/app.js"])
async def test_public_paths(client, path):
    assert (await client.get(path)).status == 200


async def test_status_says_signed_out(client):
    assert await (await client.get("/api/auth")).json() == {
        "enabled": True,
        "signed_in": False,
    }


# ----------------------------------------------------------------- signing in


async def test_wrong_password_is_refused(client):
    response = await sign_in(client, password="nope nope")
    assert response.status == 401
    assert "blynky_session" not in response.cookies


async def test_wrong_user_is_refused(client):
    assert (await sign_in(client, user="someone")).status == 401


async def test_bad_bodies_are_refused(client):
    for body in ([], {"user": 1, "password": 2}, {"user": USER}):
        response = await client.post("/api/auth/login", json=body)
        assert response.status == 400


async def test_sign_in_sets_a_long_lived_cookie(client):
    response = await sign_in(client)
    assert response.status == 200
    header = response.headers["Set-Cookie"]
    assert "blynky_session=" in header
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header
    assert f"Max-Age={365 * 86400}" in header
    assert "Secure" not in header


async def test_cookie_is_secure_behind_https(client):
    response = await client.post(
        "/api/auth/login",
        json={"user": USER, "password": PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert "Secure" in response.headers["Set-Cookie"]


async def test_signed_in_reaches_everything(client):
    await sign_in(client)
    assert (await client.get("/api/state")).status == 200
    assert (
        await client.get("/", headers={"Accept": "text/html"}, allow_redirects=False)
    ).status == 200
    ws = await client.ws_connect("/ws")
    assert (await ws.receive_json())["type"] == "state"
    await ws.close()
    login_page = await client.get("/login?next=//evil.example", allow_redirects=False)
    assert login_page.status == 302
    assert login_page.headers["Location"] == "/"


async def test_sign_out(client):
    await sign_in(client)
    await client.post("/api/auth/logout")
    assert (await client.get("/api/state")).status == 401


async def test_session_survives_a_restart(make_client, client, clock, secret):
    response = await sign_in(client)
    value = response.cookies["blynky_session"].value
    again = await make_client(auth.Auth(USER, PASSWORD, 365, secret, clock))
    again.session.cookie_jar.update_cookies({"blynky_session": value})
    assert (await again.get("/api/state")).status == 200


async def test_session_is_renewed_in_its_second_half(client, clock):
    await sign_in(client)
    clock.now += 100 * 86400
    assert "Set-Cookie" not in (await client.get("/api/state")).headers
    clock.now += 100 * 86400
    renewed = await client.get("/api/state")
    assert "blynky_session=" in renewed.headers["Set-Cookie"]


async def test_repeated_failures_are_throttled(client, clock):
    for _ in range(5):
        assert (await sign_in(client, password="wrong one")).status == 401
    throttled = await sign_in(client)
    assert throttled.status == 429
    assert int(throttled.headers["Retry-After"]) == 30
    clock.now += 31
    assert (await sign_in(client)).status == 200


async def test_lockout_doubles_up_to_a_limit(login):
    waits = []
    for _ in range(12):
        login._failed()
        waits.append(login.retry_after())
    assert waits[:4] == [0, 0, 0, 0]
    assert waits[4:8] == [30, 60, 120, 240]
    assert waits[-1] == 15 * 60


async def test_next_keeps_a_query(client):
    response = await client.get(
        "/library?q=dog&camera=porch",
        headers={"Accept": "text/html"},
        allow_redirects=False,
    )
    location = response.headers["Location"]
    from urllib.parse import parse_qs, urlsplit

    assert parse_qs(urlsplit(location).query)["next"] == ["/library?q=dog&camera=porch"]


# ------------------------------------------------------------------- off


async def test_login_off_changes_nothing(make_client, tmp_path):
    client = await make_client(auth.Auth("", "", secret_path=tmp_path / "s"))
    assert (await client.get("/api/state")).status == 200
    response = await client.get("/login", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/"
    assert "Set-Cookie" not in response.headers
    assert not (tmp_path / "s").exists()
