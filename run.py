"""Entry point: log in, start polling, serve the UI."""

import asyncio
import logging
from pathlib import Path

from aiohttp import ClientSession, web

from blynky import config
from blynky.monitor import Monitor
from blynky.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# blinkpy logs the full homescreen payload at INFO on every poll.
logging.getLogger("blinkpy").setLevel(logging.WARNING)

_LOGGER = logging.getLogger("blynky")


async def main():
    """Connect to Blink, then serve until interrupted."""
    async with ClientSession() as session:
        monitor = Monitor(session)
        # Try the cached token. If it is missing or stale the server still
        # comes up and the browser collects credentials instead.
        if not await monitor.bootstrap():
            _LOGGER.info("not authenticated - sign in at the URL below")

        runner = web.AppRunner(create_app(monitor))
        await runner.setup()
        site = web.TCPSite(runner, config.HOST, config.PORT)
        try:
            await site.start()
        except OSError as error:
            # Almost always a second copy of this app already running - which
            # looks like "it ignored my token file" from the browser, because
            # the tab is talking to the older process.
            await monitor.stop()
            await runner.cleanup()
            raise SystemExit(
                f"Cannot bind {config.HOST}:{config.PORT} ({error.strerror}). "
                "Another instance is probably already running - stop it, or "
                "set PORT to something else."
            ) from None

        _LOGGER.info("http://%s:%s", config.HOST, config.PORT)
        if config.AUTH_ENABLED:
            _LOGGER.info("login is on, as %s", config.AUTH_USER)
        elif config.HOST not in ("127.0.0.1", "localhost", "::1") and not (
            Path("/.dockerenv").exists()
        ):
            # Under Docker HOST is always 0.0.0.0 and BIND_ADDR decides, which
            # this cannot see; outside it, this is the whole of the exposure.
            _LOGGER.warning(
                "listening on %s with no login: anyone who can reach this "
                "port sees the cameras. Set AUTH_USER and AUTH_PASSWORD, or "
                "HOST=127.0.0.1 behind tailscale serve",
                config.HOST,
            )
        try:
            await asyncio.Event().wait()
        finally:
            await monitor.stop()
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
