"""What the server will and will not hand to a browser."""

import pytest
from aiohttp import WSServerHandshakeError

from blynky import config


@pytest.fixture
def secrets(data_dir):
    """Files that live in DATA_DIR and must never be served."""
    names = (".blink_creds.json", "cameras.json", "ntfy.json", "daily.json")
    for name in names:
        (data_dir / name).write_text("{}")
    return names


@pytest.mark.parametrize(
    "name", [".blink_creds.json", "cameras.json", "ntfy.json", "daily.json", ""]
)
async def test_data_dir_files_are_not_served(client, secrets, name):
    response = await client.get(f"/media/{name}")
    assert response.status == 404


async def test_media_is_still_served(client):
    folder = config.STILL_DIR / "porch"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "one.jpg").write_bytes(b"\xff\xd8jpeg")
    response = await client.get("/media/stills/porch/one.jpg")
    assert response.status == 200
    assert await response.read() == b"\xff\xd8jpeg"


async def test_json_and_dotfiles_under_media_are_refused(client):
    folder = config.EVENT_DIR / "porch"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "clip.json").write_text("{}")
    (folder / ".hidden.jpg").write_bytes(b"x")
    assert (await client.get("/media/events/porch/clip.json")).status == 404
    assert (await client.get("/media/events/porch/clip.JSON")).status == 404
    assert (await client.get("/media/events/porch/.hidden.jpg")).status == 404


async def test_traversal_out_of_media_is_refused(client, secrets):
    response = await client.get("/media/stills/%2e%2e/.blink_creds.json")
    assert response.status in (403, 404)


@pytest.mark.parametrize(
    "reference",
    ["../../.blink_creds.json", "/etc/hosts", "person.json", "ref-9999.jpg"],
)
async def test_people_reference_delete_only_takes_listed_files(
    client, data_dir, reference
):
    creds = data_dir / ".blink_creds.json"
    creds.write_text("{}")
    created = await client.post("/api/people", json={"name": "Sam"})
    assert (await created.json())["status"] == "ok"

    response = await client.delete("/api/people/sam", params={"reference": reference})
    assert response.status == 404
    assert creds.exists()
    assert (config.PEOPLE_DIR / "sam" / "person.json").exists()


async def test_foreign_host_is_refused(client):
    response = await client.get("/api/state", headers={"Host": "evil.example"})
    assert response.status == 421
    assert "ALLOWED_HOSTS" in await response.text()


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8080", "localhost:8080", "[::1]:8080", "192.168.1.20"]
)
async def test_addresses_and_localhost_are_allowed(client, host):
    response = await client.get("/api/state", headers={"Host": host})
    assert response.status == 200


async def test_websocket_from_another_site_is_refused(client):
    with pytest.raises(WSServerHandshakeError) as refused:
        await client.ws_connect("/ws", headers={"Origin": "https://evil.example"})
    assert refused.value.status == 403


async def test_websocket_from_own_page_connects(client):
    origin = f"http://{client.host}:{client.port}"
    ws = await client.ws_connect("/ws", headers={"Origin": origin})
    message = await ws.receive_json()
    assert message["type"] == "state"
    await ws.close()


async def test_cross_site_post_is_refused(client):
    response = await client.post(
        "/api/ntfy",
        json={"paused": True},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status == 403


async def test_cross_site_fetch_metadata_is_refused(client):
    response = await client.post("/api/sync", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status == 403


async def test_text_plain_body_is_refused(client):
    response = await client.post(
        "/api/ntfy", data="{}", headers={"Content-Type": "text/plain"}
    )
    assert response.status == 415


async def test_security_headers(client):
    response = await client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Server"] == "Blynky"
