"""aiohttp app: serves the UI, the saved media, and the live event stream."""

import asyncio
import contextlib
import logging
import platform
import shutil
import subprocess
from functools import cache
from importlib import metadata
from pathlib import Path

from aiohttp import WSMsgType, web

from . import config, library, notify

_LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
# Written by the Docker build, which has .git but not git at runtime.
BUILD_FILE = PACKAGE_DIR / "BUILD"


async def index(request):
    """Serve the single page."""
    del request
    return web.FileResponse(STATIC_DIR / "index.html")


async def state(request):
    """Return the current UI state as JSON."""
    return web.json_response(request.app["monitor"].snapshot())


def _version(package):
    """Return an installed package's version, or None if it isn't installed."""
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _git(*args):
    """Run git against this checkout, or return None if that isn't possible."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PACKAGE_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


@cache
def build_info():
    """Return the running version (nearest tag) and short commit hash.

    From the file the image bakes in, else from git when running from a
    checkout, else the package version with no commit.
    """
    try:
        version, commit = BUILD_FILE.read_text().splitlines()[:2]
    except (OSError, ValueError):
        version = _git("describe", "--tags", "--always", "--dirty")
        commit = _git("rev-parse", "--short", "HEAD")
    else:
        version = version if version != "unknown" else None
    return {
        "version": version or _version("blynky"),
        "commit": commit or None,
    }


async def diagnostics(request):
    """Return the facts a bug report needs, and nothing that identifies the user.

    These end up in a public GitHub issue, so no camera or system names, URLs,
    topics or account details: counts and versions only.
    """
    monitor = request.app["monitor"]
    cameras = list(monitor.cameras.values())
    return web.json_response(
        {
            **build_info(),
            "blinkpy": _version("blinkpy"),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "docker": Path("/.dockerenv").exists(),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "provider": config.VISION_PROVIDER,
            "model": config.VISION_MODEL,
            "status": monitor.status,
            "cameras": len(cameras),
            "offline": sum(1 for c in cameras if c.get("online") is False),
            "systems": len({c.get("sync") for c in cameras}),
            "poll_seconds": config.POLL_SECONDS,
            "ntfy": bool(config.NTFY_URL),
            "identify": config.IDENTIFY,
        }
    )


async def login(request):
    """Accept credentials from the browser and attempt a sign-in."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(reason="expected JSON") from None

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return web.json_response(
            {"status": "error", "message": "Username and password are required."}
        )

    result = await request.app["monitor"].login(username, password)
    return web.json_response(result)


async def submit_2fa(request):
    """Accept a 2FA code from the browser."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(reason="expected JSON") from None

    code = (body.get("code") or "").strip()
    if not code:
        return web.json_response(
            {"status": "error", "message": "Enter the code Blink sent you."}
        )

    result = await request.app["monitor"].submit_2fa(code)
    return web.json_response(result)


async def snap(request):
    """Ask one camera for a new still, on request from the browser."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(reason="expected JSON") from None

    camera = (body.get("camera") or "").strip()
    if not camera:
        return web.json_response({"status": "error", "message": "No camera named."})

    result = await request.app["monitor"].snap(camera)
    return web.json_response(result)


async def archive(request):
    """Return one page of the archive, newest first."""
    query = request.query
    kind = query.get("kind") or None
    if kind not in (None, library.MOTION, library.STILL):
        raise web.HTTPBadRequest(reason="unknown kind")

    try:
        limit = int(query.get("limit") or config.PAGE_SIZE)
    except ValueError:
        raise web.HTTPBadRequest(reason="limit must be a number") from None
    # A page big enough to matter is a page big enough to hurt; cap it here
    # rather than trusting whatever the query string asked for.
    limit = max(1, min(limit, 200))

    subject = query.get("subject") or None
    if subject is not None and subject not in config.SUBJECTS:
        raise web.HTTPBadRequest(reason="unknown subject")

    until = None
    if query.get("until"):
        until = library.parse_stamp(query["until"])
        if until is None:
            raise web.HTTPBadRequest(reason="until must be an ISO timestamp")

    page = request.app["monitor"].library.query(
        camera=query.get("camera") or None,
        kind=kind,
        subject=subject,
        search=query.get("q") or None,
        cursor=query.get("cursor") or None,
        limit=limit,
        until=until,
    )
    return web.json_response(page)


async def archive_stats(request):
    """Return archive totals and the cameras that appear in it."""
    return web.json_response(request.app["monitor"].library.stats())


async def known_people(request):
    """List the enrolled people and their reference frames."""
    return web.json_response({"people": request.app["monitor"].people.all()})


async def enroll(request):
    """Tag one saved capture as being a named person.

    The frame becomes a reference, which is why this takes an event rather
    than an upload: every example comes out of footage these cameras actually
    produced, at the angle and resolution they produce it at.
    """
    monitor = request.app["monitor"]
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(reason="expected JSON") from None

    name = (body.get("name") or "").strip()
    event_id = (body.get("event") or "").strip()
    if not name:
        return web.json_response({"status": "error", "message": "Name required."})

    record = monitor.library.find(event_id) if event_id else None
    if event_id and record is None:
        return web.json_response({"status": "error", "message": "Unknown capture."})

    try:
        person = await monitor.people.add(name)
    except ValueError as error:
        return web.json_response({"status": "error", "message": str(error)})

    added = None
    note = None
    if record is not None:
        # A frame showing two people is a poor example of either: filed under
        # one name it teaches the model that the other one is also them. The
        # confirmation is still worth recording, the picture is not.
        crowded = (record.get("people_count") or 0) > 1
        if crowded:
            note = (
                f"Noted as {person['name']}, but not kept as an example - "
                "there is more than one person in this clip."
            )
        else:
            image = await monitor.reference_frame(record)
            if image is None:
                return web.json_response(
                    {"status": "error", "message": "That capture has no usable frame."}
                )
            added = await monitor.people.add_reference(
                person["slug"], image, event_id=record["id"]
            )
        # Somebody has just said, in as many words, who this is. That outranks
        # anything the model guessed, so it is recorded on the capture too -
        # otherwise tagging changes nothing visible and looks like it failed.
        await monitor.confirm_person(record, person["name"])

    return web.json_response(
        {
            "status": "ok",
            "person": person["slug"],
            "added": added,
            "note": note,
            "event": record,
            "people": monitor.people.all(),
        }
    )


async def forget_person(request):
    """Remove one person, or one of their reference frames."""
    monitor = request.app["monitor"]
    slug = request.match_info["slug"]
    reference = request.query.get("reference")
    try:
        if reference:
            await monitor.people.remove_reference(slug, reference)
        elif not await monitor.people.remove(slug):
            raise web.HTTPNotFound(reason="no such person")
    except KeyError:
        raise web.HTTPNotFound(reason="no such person") from None
    return web.json_response({"status": "ok", "people": monitor.people.all()})


async def backfill(request):
    """Start describing saved clips that have none, and say how many there are."""
    monitor = request.app["monitor"]
    if request.method == "GET":
        return web.json_response(monitor.backfill_status())
    return web.json_response(monitor.start_backfill())


async def retag(request):
    """Report on, start or stop re-describing clips from before tags existed."""
    monitor = request.app["monitor"]
    if request.method == "POST":
        return web.json_response(monitor.start_retag())
    if request.method == "DELETE":
        return web.json_response(monitor.stop_retag())
    return web.json_response(monitor.retag_status())


async def sync(request):
    """Pull the clips Blink still holds, and say how it is going."""
    monitor = request.app["monitor"]
    if request.method == "GET":
        return web.json_response(monitor.sync_status())
    return web.json_response(monitor.start_sync())


async def ntfy(request):
    """Pause or resume push notifications, and say whether they are paused.

    POST {"paused": true, "hours": 4} pauses for that long, and without hours
    until resumed; {"paused": false} resumes. The pause holds for everyone,
    since it is the server that sends them.
    """
    monitor = request.app["monitor"]
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(reason="expected JSON") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="expected a JSON object")
        if body.get("paused"):
            hours = body.get("hours")
            if hours is not None:
                if isinstance(hours, bool) or not isinstance(hours, (int, float)):
                    raise web.HTTPBadRequest(reason="hours must be a number")
                limit = config.NTFY_MAX_PAUSE_HOURS
                if not 0 < hours <= limit:
                    reason = f"hours must be over 0 and at most {limit}"
                    raise web.HTTPBadRequest(reason=reason)
            await notify.pause(hours)
        else:
            await notify.resume()
        monitor.broadcast_ntfy()
    return web.json_response(notify.status())


async def websocket(request):
    """Stream state and events to one browser."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    monitor = request.app["monitor"]
    queue = monitor.subscribe()
    await ws.send_json(monitor.snapshot())

    async def pump():
        while True:
            message = await queue.get()
            await ws.send_json(message)

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            # The client never sends anything meaningful; this loop exists to
            # notice disconnects.
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        monitor.unsubscribe(queue)

    return ws


async def revalidate_ui(request, response):
    """Make the browser check the page and its assets for a newer copy.

    aiohttp sends static files with Last-Modified and nothing else, which lets
    a browser reuse its copy for days on a guess. After an upgrade that pairs
    the new page with the old script, and the UI breaks in ways a reload does
    not obviously fix. no-cache still keeps the copy; it only has to be
    confirmed, which costs a 304. Media is left alone: a capture never changes.
    """
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")


def create_app(monitor):
    """Build the aiohttp application."""
    config.LATEST_DIR.mkdir(parents=True, exist_ok=True)
    config.EVENT_DIR.mkdir(parents=True, exist_ok=True)
    config.STILL_DIR.mkdir(parents=True, exist_ok=True)
    config.PEOPLE_DIR.mkdir(parents=True, exist_ok=True)

    app = web.Application()
    app["monitor"] = monitor
    app.on_response_prepare.append(revalidate_ui)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/2fa", submit_2fa)
    app.router.add_post("/api/snap", snap)
    app.router.add_get("/api/library", archive)
    app.router.add_get("/api/library/stats", archive_stats)
    app.router.add_get("/api/people", known_people)
    app.router.add_post("/api/people", enroll)
    app.router.add_delete("/api/people/{slug}", forget_person)
    app.router.add_get("/api/backfill", backfill)
    app.router.add_post("/api/backfill", backfill)
    app.router.add_get("/api/retag", retag)
    app.router.add_post("/api/retag", retag)
    app.router.add_delete("/api/retag", retag)
    app.router.add_get("/api/sync", sync)
    app.router.add_post("/api/sync", sync)
    app.router.add_get("/api/ntfy", ntfy)
    app.router.add_post("/api/ntfy", ntfy)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/media/", config.DATA_DIR)
    app.router.add_static("/static/", STATIC_DIR)
    return app
