"""Point the app at a throwaway data folder before anything imports config.

config reads the environment once, at import, and fills gaps from the
checkout's .env - which on a developer's machine is their real setup. Every
setting a test depends on is therefore set here explicitly, so nothing from
that .env can leak in, and the data folder is a fresh temporary one.
"""

import os
import tempfile
from pathlib import Path

import pytest

DATA_DIR = Path(tempfile.mkdtemp(prefix="blynky-test-"))

os.environ.update(
    {
        "DATA_DIR": str(DATA_DIR),
        "CREDFILE": str(DATA_DIR / ".blink_creds.json"),
        "VISION_PROVIDER": "ollama",
        "NTFY_URL": "",
        "BLYNKY_URL": "",
        "ALLOWED_HOSTS": "",
        "AUTH_USER": "",
        "AUTH_PASSWORD": "",
        "RETAIN_DAYS": "0",
    }
)

from blynky import config, server  # noqa: E402
from blynky.people import People  # noqa: E402


class FakeMonitor:
    """Just enough of Monitor for the HTTP layer: no Blink, no model."""

    def __init__(self):
        """Start with nobody enrolled and nothing to stream."""
        self.people = People()
        self.queues = []

    def snapshot(self):
        """Return a state message with no cameras."""
        return {"type": "state", "status": "watching", "cameras": []}

    def subscribe(self):
        """Hand out an empty queue, as a fresh browser gets."""
        import asyncio

        queue = asyncio.Queue()
        self.queues.append(queue)
        return queue

    def unsubscribe(self, queue):
        """Forget a queue."""
        self.queues.remove(queue)

    def broadcast_ntfy(self):
        """Nothing is listening."""


@pytest.fixture
def data_dir():
    """Return the data folder the app was configured with."""
    return config.DATA_DIR


@pytest.fixture
async def client(aiohttp_client):
    """Return a test client for the app, over a fake monitor."""
    monitor = FakeMonitor()
    await monitor.people.load()
    app = server.create_app(monitor)
    return await aiohttp_client(app)
