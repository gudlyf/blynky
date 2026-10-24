"""Push notifications to ntfy when a camera sees something worth knowing.

The point of filtering here rather than sending everything is that most of
what these cameras notice is animals, vehicles and weather. A notification for
each of those is a notification nobody reads, and one false alarm a night is
how people turn notifications off for good.
"""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone

from aiohttp import ClientTimeout

from . import config

_LOGGER = logging.getLogger(__name__)

# When each camera last sent something, so one stuck in a motion loop at 3am
# cannot empty the daily quota by itself.
_last_sent = {}

# Whether someone has paused notifications from the settings menu: None when
# they are on, a wall-clock time when they come back on by themselves, and
# _FOREVER when only someone turning them back on will do. Wall clock rather
# than monotonic, because the pause is kept on disk across restarts.
_FOREVER = float("inf")
_paused_until = None


def _stamp(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds")


def _paused():
    """Say whether a pause is in force, ending one whose time is up."""
    global _paused_until
    if _paused_until is None:
        return False
    if _paused_until != _FOREVER and time.time() >= _paused_until:
        _LOGGER.info("ntfy: pause over, notifications back on")
        _paused_until = None
        return False
    return True


def status():
    """Return the pause state the settings menu shows."""
    paused = _paused()
    timed = paused and _paused_until != _FOREVER
    return {
        # Without a topic there is nothing to pause, and the menu says nothing.
        "configured": bool(config.NTFY_URL),
        "paused": paused,
        "until": _stamp(_paused_until) if timed else None,
    }


async def load():
    """Pick up a pause that was in force when the app last stopped."""
    global _paused_until
    try:
        text = await asyncio.to_thread(config.NTFY_STATE.read_text, encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as error:
        _LOGGER.warning("could not read %s: %s", config.NTFY_STATE, error)
        return
    try:
        stored = json.loads(text)
        if not stored.get("paused"):
            return
        until = stored.get("until")
        _paused_until = (
            _FOREVER if until is None else datetime.fromisoformat(until).timestamp()
        )
    except (ValueError, TypeError, AttributeError) as error:
        _LOGGER.warning("ignoring unreadable %s: %s", config.NTFY_STATE, error)
        return
    if _paused():
        until = status()["until"]
        _LOGGER.info("ntfy: paused %s", f"until {until}" if until else "until resumed")


async def pause(hours=None):
    """Stop sending for this many hours, or until resume() when None."""
    global _paused_until
    _paused_until = _FOREVER if hours is None else time.time() + hours * 3600
    await _save()
    until = status()["until"]
    _LOGGER.info("ntfy: paused %s", f"until {until}" if until else "until resumed")


async def resume():
    """Start sending again."""
    global _paused_until
    _paused_until = None
    await _save()
    _LOGGER.info("ntfy: notifications back on")


async def _save():
    payload = json.dumps({"paused": _paused(), "until": status()["until"]})
    try:
        await asyncio.to_thread(_write, payload)
    except OSError as error:
        # Still paused for as long as this process runs; only a restart forgets.
        _LOGGER.warning("could not write %s: %s", config.NTFY_STATE, error)


def _write(payload):
    path = config.NTFY_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_suffix(".tmp")
    spare.write_text(payload, encoding="utf-8")
    spare.replace(path)


def _header(value):
    """Return a header value ntfy will accept, encoding it only if it must be.

    HTTP headers are latin-1; a description is model output and regularly
    carries a curly quote or an accent. RFC 2047 is what the header format
    itself defines for that case.
    """
    text = " ".join((value or "").split())
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{encoded}?="
    return text


def _tags(subjects, known):
    """Pick the emoji ntfy shows beside the message."""
    if "person" in subjects:
        return "house" if known else "bust_in_silhouette"
    if "package" in subjects:
        return "package"
    if "vehicle" in subjects:
        return "blue_car"
    if "animal" in subjects:
        return "paw_prints"
    return "eyes"


def should_send(event):
    """Say whether this event is one to push, and why not when it is not."""
    if not config.NTFY_URL:
        return False
    if _paused():
        _LOGGER.debug("ntfy: paused, staying quiet about %s", event["camera"])
        return False
    subjects = event.get("subjects")
    if subjects is None:
        # The model never said what it saw. Guessing from prose is fine for
        # highlighting a word; it is not good enough to wake a phone.
        return False
    if not any(name in subjects for name in config.NTFY_SUBJECTS):
        return False
    # The point of recognising anyone at all: a household member coming home
    # is not news, and being told about it every time is how someone learns to
    # swipe these away without reading them.
    if config.NTFY_SKIP_KNOWN and event.get("who"):
        _LOGGER.debug("ntfy: %s recognised, staying quiet", ", ".join(event["who"]))
        return False

    now = time.monotonic()
    last = _last_sent.get(event["camera"])
    if last is not None and now - last < config.NTFY_COOLDOWN:
        _LOGGER.debug("ntfy: %s still in cooldown", event["camera"])
        return False
    return True


async def send(session, event, image=None):
    """Push one event to ntfy. Returns True if it was accepted."""
    if not should_send(event):
        return False

    subjects = event.get("subjects") or []
    known = event.get("who") or []
    # A stranger at the door is the alert worth having; someone who lives here
    # is a note. Anyone unrecognised keeps the louder priority, which includes
    # the case where identification was never asked for.
    headers = {
        "Title": _header(event["camera"]),
        "Tags": _tags(subjects, known),
        "Priority": str(config.NTFY_KNOWN_PRIORITY if known else config.NTFY_PRIORITY),
    }
    summary = event.get("summary") or "Motion detected."
    if known:
        # Hedged on purpose. The model is matching build and clothing on a
        # hundred-pixel figure, and saying so keeps a wrong guess honest.
        summary = f"Looks like {', '.join(known)}. {summary}"

    # An attachment has to travel as the request body, which leaves the text
    # to the Message header. Without an image the body is the text itself.
    if image:
        headers["Message"] = _header(summary)
        headers["Filename"] = f"{event['prefix']}.jpg"
        body = image
    else:
        body = summary.encode("utf-8")

    _last_sent[event["camera"]] = time.monotonic()
    try:
        response = await session.post(
            config.NTFY_URL,
            data=body,
            headers=headers,
            timeout=ClientTimeout(total=config.NTFY_TIMEOUT),
        )
        text = await response.text()
        if response.status >= 400:
            _LOGGER.error("ntfy returned %s: %s", response.status, text[:200])
            return False
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - a missed alert is not fatal
        _LOGGER.error("ntfy request failed: %s", error)
        return False

    _LOGGER.info("ntfy: %s - %s", event["camera"], summary[:80])
    return True
