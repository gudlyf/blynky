"""Entry point: log in, start polling, serve the UI."""

import asyncio
import logging

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
