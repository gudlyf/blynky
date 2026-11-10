"""Small failure modes: bad bodies, the index, partial writes, slow browsers."""

import asyncio

import pytest

from blynky import config, files, library, monitor


def record(event_id, stamp="2026-11-01T12:00:00+00:00"):
    return {"id": event_id, "stamp": stamp, "kind": library.MOTION}


# --------------------------------------------------------------- the index


def test_removed_record_comes_back_when_added_again():
    index = library.Library()
    first = record("porch/1")
    index.add(first)
    assert index.remove(first)
    assert index.find("porch/1") is None

    index.add(record("porch/1"))
    assert index.find("porch/1") is not None
    assert [r["id"] for r in index.all()] == ["porch/1"]


def test_remove_reports_whether_it_was_there():
    index = library.Library()
    assert not index.remove(record("nope"))


# ------------------------------------------------------------ request bodies


@pytest.mark.parametrize("path", ["/api/snap", "/api/login", "/api/2fa", "/api/people"])
@pytest.mark.parametrize("body", [[], "text", 3, None])
async def test_non_object_bodies_are_400(client, path, body):
    response = await client.post(path, json=body)
    assert response.status == 400


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/login", {"username": 1, "password": "x"}),
        ("/api/2fa", {"code": 123456}),
        ("/api/people", {"name": ["Sam"]}),
    ],
)
async def test_non_string_fields_are_400(client, path, body):
    response = await client.post(path, json=body)
    assert response.status == 400


# ---------------------------------------------------------------- writing


def test_write_atomic_replaces_whole(tmp_path):
    target = tmp_path / "sub" / "a.json"
    files.write_atomic(target, "one")
    files.write_atomic(target, b"two")
    assert target.read_text() == "two"
    assert [p.name for p in target.parent.iterdir()] == ["a.json"]


def test_write_atomic_private_mode(tmp_path):
    target = tmp_path / "secret"
    files.write_atomic(target, "x", mode=0o600)
    assert target.stat().st_mode & 0o777 == 0o600


def test_failed_write_leaves_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "a.json"
    target.write_text("old")

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(files.os, "replace", broken)
    with pytest.raises(OSError):
        files.write_atomic(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


# --------------------------------------------------------------- the monitor


async def make_monitor():
    return monitor.Monitor(session=None)


async def test_a_browser_that_stops_reading_is_dropped():
    watcher = await make_monitor()
    stalled = watcher.subscribe()
    for n in range(monitor._QUEUE_LIMIT + 1):
        watcher._broadcast({"n": n})
    assert stalled not in watcher._listeners
    assert stalled.qsize() == monitor._QUEUE_LIMIT


async def test_blink_sign_in_is_refused_while_a_session_is_running():
    watcher = await make_monitor()
    watcher.status = "error"
    result = await watcher.login("someone@example.com", "password")
    assert result["status"] == "error"
    assert watcher.blink is None


class FakeBlink:
    """Writes what blinkpy would, the way blinkpy does: open "w" and dump."""

    def __init__(self, fail=False):
        """Optionally fail mid-save."""
        self.fail = fail

    async def save(self, file_name):
        """Write a token file, or fail after starting to."""
        await asyncio.to_thread(self._save, file_name)

    def _save(self, file_name):
        with open(file_name, "w") as handle:
            handle.write('{"token": "new"}')
            if self.fail:
                raise OSError("disk full")


async def test_credentials_are_private_and_whole(data_dir, monkeypatch):
    creds = data_dir / "creds-test.json"
    monkeypatch.setattr(config, "CREDFILE", str(creds))
    watcher = await make_monitor()

    watcher.blink = FakeBlink()
    await watcher._save_creds()
    assert creds.read_text() == '{"token": "new"}'
    assert creds.stat().st_mode & 0o777 == 0o600

    watcher.blink = FakeBlink(fail=True)
    with pytest.raises(OSError):
        await watcher._save_creds()
    assert creds.read_text() == '{"token": "new"}'
    assert not (data_dir / ".creds-test.json.tmp").exists()
