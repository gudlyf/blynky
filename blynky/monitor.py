"""Blink polling, media capture, and the motion event pipeline."""

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from blinkpy import api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load, to_alphanumeric
from slugify import slugify

from . import cameras, config, library, media, notify, people, retention, vision

_LOGGER = logging.getLogger(__name__)


def _now():
    """Return the current time as an ISO string the browser can parse."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp):
    """Parse one of our ISO stamps back into a datetime, or None."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


# Ordered weakest to strongest, so a minimum can be read as an index.
_CONFIDENCE = ("low", "medium", "high")


def _confident_enough(confidence):
    """Say whether an identification is sure enough to be recorded."""
    floor = config.IDENTIFY_MIN_CONFIDENCE.strip().lower()
    if floor not in _CONFIDENCE:
        return True
    if confidence not in _CONFIDENCE:
        # The model did not say how sure it was, which is not a reason to
        # assume it was sure.
        return False
    return _CONFIDENCE.index(confidence) >= _CONFIDENCE.index(floor)


def _thumb_time(camera):
    """Return when Blink actually took the stored thumbnail, if we can tell.

    Blink only regenerates a camera's thumbnail when something explicitly asks
    for a new picture, so "the latest still" can be months old. The capture
    time rides along in the thumbnail URL as a unix timestamp, and it is the
    only honest answer to how old the image on screen is.
    """
    thumb = getattr(camera, "thumbnail", None) or ""
    stamp = parse_qs(urlparse(thumb).query).get("ts", [None])[0]
    if not stamp or not stamp.isdigit():
        return None
    taken = datetime.fromtimestamp(int(stamp), timezone.utc)
    return taken.isoformat(timespec="seconds")


class Monitor:
    """Owns the Blink connection and the state the UI renders."""

    def __init__(self, session):
        """Set up empty state; call bootstrap() before serving."""
        self.session = session
        self.blink = None
        self.cameras = {}
        # The timeline under the grid reads out of this deque, so it has to
        # hold at least as much as the timeline shows however HISTORY is set.
        self.events = deque(maxlen=max(config.HISTORY, config.TIMELINE))
        # Everything ever captured, indexed from disk. The deque above is only
        # the tail of this.
        self.library = library.Library()
        # Who the household is, and the frames used to recognise them.
        self.people = people.People()
        # Every name each camera has had, so a rename is not a new camera.
        self.names = cameras.Names()
        # starting -> needs-credentials -> awaiting-2fa -> watching
        self.status = "starting"
        # Explains a fallback to the sign-in form, when there is one to give.
        self.message = None
        self.last_poll = None
        self._listeners = set()
        self._digests = {}
        self._seen = {}
        self._task = None
        self._login_lock = asyncio.Lock()
        # Serializes everything that touches Blink: the poll loop and the
        # snapshot button must never interleave their refreshes.
        self._blink_lock = asyncio.Lock()
        # Describing runs off the poll loop, but one clip at a time.
        self._describe_lock = asyncio.Lock()
        self._describing = set()
        # The backlog sweep over saved clips that have no description.
        self._backfill = None
        # The manual pull of whatever Blink is still holding for each camera.
        self._sync = None
        # Making thumbnails for captures saved before thumbnails existed.
        self._thumbs = None
        # Each system's name by network id, as of the last time blinkpy's
        # tables were built. None until the first check after a login.
        self._systems = None
        self._sync_state = {"running": False, "found": 0, "saved": 0, "at": None}
        self._closing = False

    # -- pub/sub ----------------------------------------------------------

    def subscribe(self):
        """Register a queue that receives broadcast messages."""
        queue = asyncio.Queue()
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue):
        """Drop a previously registered queue."""
        self._listeners.discard(queue)

    def _broadcast(self, message):
        for queue in list(self._listeners):
            # Never let a slow client stall the poll loop.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    # -- state ------------------------------------------------------------

    def snapshot(self):
        """Return the full UI state for a freshly connected client."""
        return {
            "type": "state",
            "status": self.status,
            "message": self.message,
            "last_poll": self.last_poll,
            "poll_seconds": config.POLL_SECONDS,
            "provider": config.VISION_PROVIDER,
            "model": config.VISION_MODEL,
            "page_size": config.PAGE_SIZE,
            "timeline": config.TIMELINE,
            "cameras": list(self.cameras.values()),
            "events": list(self.events),
            "ntfy": notify.status(),
        }

    def broadcast_ntfy(self):
        """Tell every browser that notifications were paused or resumed."""
        self._broadcast({"type": "ntfy", "ntfy": notify.status()})

    # -- connection -------------------------------------------------------

    def _new_blink(self, creds):
        """Build a fresh Blink/Auth pair for a login attempt."""
        # Sit just under the poll interval so a fractional timing shortfall
        # never makes refresh() skip a cycle.
        self.blink = Blink(session=self.session, refresh_rate=config.POLL_SECONDS - 5)
        # Never prompt: this process has no terminal to prompt on once it is
        # serving, so every interactive step goes through the browser.
        self.blink.auth = Auth(creds, no_prompt=True, session=self.session)

    async def bootstrap(self):
        """Try the cached token. Returns True if we came up authenticated."""
        # Index the archive before anything else: _seed_from_archive needs it
        # the moment a login lands, and it does not depend on Blink at all.
        await self.library.load()
        await self.people.load()
        await self.names.load()
        await notify.load()
        self.start_thumbnails()
        creds = await json_load(config.CREDFILE)
        if not creds:
            _LOGGER.info("no token file - waiting for sign-in via the UI")
            self.status = "needs-credentials"
            return False

        # The token file also carries the username and password, so blinkpy
        # re-logs-in by itself when the token has gone stale. The only thing
        # that can still need a human is the 2FA code.
        self._new_blink(creds)
        try:
            started = await self.blink.start()
        except BlinkTwoFARequiredError:
            _LOGGER.info("cached token stale - asking for a 2FA code")
            self.status = "awaiting-2fa"
            return False
        except Exception as error:  # noqa: BLE001 - fall back to the login form
            _LOGGER.warning("cached credentials unusable: %s", error)
            self._need_credentials(
                f"The saved credentials in {config.CREDFILE} could not be used. "
                "Sign in again to replace them."
            )
            return False

        if not started:
            _LOGGER.warning("cached credentials rejected - sign in via the UI")
            self._need_credentials(
                "Blink rejected the saved credentials - your password may have "
                "changed. Sign in again to replace them."
            )
            return False

        await self._finish_login()
        return True

    def _need_credentials(self, message=None):
        """Fall back to the sign-in form, saying why if we know."""
        self.status = "needs-credentials"
        self.message = message

    async def login(self, username, password):
        """Sign in with credentials supplied by the browser."""
        async with self._login_lock:
            if self.status == "watching":
                return {"status": "connected"}

            self._new_blink({"username": username, "password": password})
            try:
                started = await self.blink.start()
            except BlinkTwoFARequiredError:
                self.status = "awaiting-2fa"
                self._broadcast_status()
                return {"status": "2fa_required"}
            except Exception as error:  # noqa: BLE001 - surface, do not crash
                _LOGGER.error("login failed: %s", error)
                self.status = "needs-credentials"
                self._broadcast_status()
                return {"status": "error", "message": "Could not reach Blink."}

            if not started:
                self._need_credentials()
                self._broadcast_status()
                return {
                    "status": "error",
                    "message": "Blink rejected that username or password.",
                }

            return await self._settle()

    async def submit_2fa(self, code):
        """Complete a login that is waiting on a 2FA code."""
        async with self._login_lock:
            if self.status == "watching":
                return {"status": "connected"}
            if self.status != "awaiting-2fa" or self.blink is None:
                return {
                    "status": "error",
                    "message": "Not waiting for a code - start over.",
                }

            try:
                ok = await self.blink.send_2fa_code(code)
            except Exception as error:  # noqa: BLE001 - surface, do not crash
                _LOGGER.error("2FA submission failed: %s", error)
                ok = False

            if not ok:
                self._broadcast_status()
                return {"status": "error", "message": "That code was not accepted."}

            return await self._settle()

    async def _settle(self):
        """Finish a successful login, reporting setup failures honestly."""
        try:
            await self._finish_login()
        except Exception as error:  # noqa: BLE001 - the sign-in itself worked
            _LOGGER.exception("post-login setup failed: %s", error)
            self._need_credentials(
                "Signed in, but setting up the cameras failed. Check the server log."
            )
            self._broadcast_status()
            return {
                "status": "error",
                "message": "Signed in, but camera setup failed - see the server log.",
            }
        return {"status": "connected"}

    async def _finish_login(self):
        """Seed the token file, prime the grid, and begin polling."""
        await self._save_creds()
        await self._reconcile_cameras()
        await self._sync_snapshots(initial=True)
        self._seed_from_archive()
        self.status = "watching"
        self.message = None
        _LOGGER.info("connected: %s camera(s)", len(self.cameras))
        if self._task is None:
            self.start()
        self._broadcast(self.snapshot())
        # Off the critical path deliberately: this can run for a long time and
        # nothing about serving the UI waits on it.
        if config.BACKFILL:
            self.start_backfill()

    def _broadcast_status(self):
        self._broadcast(
            {"type": "status", "status": self.status, "message": self.message}
        )

    def _seed_from_archive(self):
        """Repopulate the live feed from disk so a restart is not a blank slate."""
        self.library.apply_names(
            {entry["slug"]: entry["name"] for entry in self.cameras.values()}
        )
        recent = self.library.recent(self.events.maxlen)
        self.events.clear()
        # recent() is newest-first and the deque is read the same way, so it
        # goes in as-is rather than reversed.
        self.events.extend(recent)

        for entry in self.cameras.values():
            # A renamed camera's history is still filed under its old name.
            held = [
                self.library.latest_for(slug) for slug in self._slugs(entry["name"])
            ]
            held = [record for record in held if record is not None]
            if not held:
                continue
            latest = max(
                held,
                key=lambda record: (
                    library.parse_stamp(record["stamp"])
                    or datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            entry["last_event"] = latest
            # Every camera gets its own cursor, not just the ones with an
            # event in the recent list: a quiet camera would otherwise start
            # with none and re-ingest whatever Blink still had on file.
            self._seen.setdefault(entry["name"], latest["stamp"])
            # Blink's stored thumbnail is usually older than the newest clip
            # frame, so prefer the frame we already pulled - same rule the
            # live path follows in _promote_still.
            shown, archived = _parse(entry["captured"]), _parse(latest["stamp"])
            if latest["image"] and (shown is None or (archived and archived > shown)):
                entry["image"] = latest["image"]
                entry["captured"] = latest["stamp"]
                entry["source"] = "clip"

        # The cursor starts where the archive ends, so a clip already saved is
        # never described a second time on the next start.
        for record in recent:
            self._seen.setdefault(record["camera"], record["stamp"])

    async def _save_creds(self):
        await self.blink.save(config.CREDFILE)
        # login_attributes includes the plaintext password, so lock it down.
        os.chmod(config.CREDFILE, 0o600)

    # -- polling ----------------------------------------------------------

    def start(self):
        """Launch the background poll loop."""
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self):
        """Cancel the poll loop and any description still in flight."""
        self._closing = True
        tasks = [
            task
            for task in (self._task, self._backfill, self._thumbs, *self._describing)
            if task
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self):
        first_pass = True
        while True:
            try:
                async with self._blink_lock:
                    await self.blink.refresh()
                    renamed = await self._reconcile_cameras()
                    await self._sync_snapshots()
                    if renamed:
                        # The browser holds cameras by name, so the old card
                        # would linger beside the new one until a reload.
                        self._broadcast(self.snapshot())
                    await self._handle_motion(first_pass)
                    first_pass = False
                    await self._save_creds()
                # Only a clean pass counts as "checked"; a failed one must not
                # make the header claim the cameras were just looked at.
                self.last_poll = _now()
                self.status = "watching"
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a bad poll must not end the loop
                _LOGGER.exception("poll failed: %s", error)
                self.status = "error"

            # Pruning runs outside the Blink lock: it only touches files this
            # process already owns, and must not hold up the next check.
            try:
                await retention.sweep(self.library)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - never end the loop over this
                _LOGGER.exception("retention sweep failed: %s", error)

            self._broadcast(
                {
                    "type": "poll",
                    "status": self.status,
                    "last_poll": self.last_poll,
                    "poll_seconds": config.POLL_SECONDS,
                }
            )
            await asyncio.sleep(config.POLL_SECONDS)

    async def _sync_snapshots(self, initial=False):
        """Write each camera's current thumbnail and tell the UI about it."""
        for name, camera in self.blink.cameras.items():
            slug = slugify(name)
            image = camera.image_from_cache
            entry = self.cameras.setdefault(
                name,
                {
                    "name": name,
                    "slug": slug,
                    "sync": getattr(camera.sync, "name", None),
                    "image": None,
                    # When the picture on screen was taken, and where it came
                    # from: Blink's stored thumbnail or a frame out of a clip.
                    "captured": None,
                    "source": None,
                    "updated": None,
                    "last_event": None,
                },
            )
            # Kept current rather than fixed at first sight: a camera moved to
            # another system, or a system renamed, changes this.
            entry["sync"] = getattr(camera.sync, "name", None)
            entry["armed"] = bool(getattr(camera.sync, "arm", False))
            entry["online"] = bool(camera.online)
            if camera.last_record:
                entry["updated"] = camera.last_record

            if not image:
                continue

            # A frame we already pulled out of a motion clip is usually newer
            # than the thumbnail Blink has on file, so never let a stale
            # thumbnail overwrite it.
            captured = _thumb_time(camera)
            if entry["source"] == "clip":
                shown, new = _parse(entry["captured"]), _parse(captured)
                if shown and (new is None or new <= shown):
                    continue

            digest = hashlib.sha1(image).hexdigest()
            if self._digests.get(name) == digest and entry["source"] == "snapshot":
                continue
            self._digests[name] = digest

            path = config.LATEST_DIR / f"{slug}.jpg"
            await media.write_bytes(path, image)
            entry["image"] = f"/media/latest/{slug}.jpg?v={digest[:12]}"
            entry["captured"] = captured
            entry["source"] = "snapshot"

            # LATEST_DIR only ever holds the current picture. Keep a dated copy
            # too, or every still taken with the refresh button is lost the
            # moment the next one arrives.
            await self._archive_still(name, slug, image, captured, digest)

            if not initial:
                self._broadcast({"type": "snapshot", "camera": entry})

    async def _archive_still(self, name, slug, image, captured, digest):
        """Keep a dated copy of one camera still and index it."""
        held = self.library.latest_for(slug, kind=library.STILL)
        if held is not None and held["digest"] == digest:
            # Not every thumbnail URL carries a capture time, and the ones that
            # do not would otherwise be filed under "now" and archived afresh
            # on every restart. The hash is what actually says it is new.
            return

        record = library.make_record(library.STILL, name, slug, captured or _now())
        record["digest"] = digest
        path = library.dir_for(library.STILL, slug) / f"{record['prefix']}.jpg"
        await media.write_bytes(path, image)
        record["image"] = f"/media/stills/{slug}/{record['prefix']}.jpg"
        record["state"] = "no-description"
        await library.ensure_thumb(record)
        record["bytes"] = len(image)
        await library.write_sidecar(record)
        self.library.add(record)
        self._broadcast({"type": "capture", "event": record})

    async def snap(self, name):
        """Ask one camera for a brand-new still, on request from the UI."""
        # A poll that just failed leaves the status at "error", which is no
        # reason to refuse: the button is often exactly how someone checks
        # whether Blink is reachable again.
        if self.blink is None or self.status in (
            "starting",
            "needs-credentials",
            "awaiting-2fa",
        ):
            return {"status": "error", "message": "Not connected to Blink."}

        camera = self.blink.cameras.get(name)
        if camera is None:
            return {"status": "error", "message": "Unknown camera."}
        if not camera.online:
            return {"status": "error", "message": f"{name} is offline."}

        async with self._blink_lock:
            try:
                # Returns once Blink reports the capture complete; the new
                # thumbnail only lands in the next homescreen read.
                await camera.snap_picture()
                # force_cache re-downloads the image without moving the motion
                # cursor, so pressing this can never swallow a clip that the
                # next poll would otherwise notice.
                await self.blink.refresh(force_cache=True)
                await self._sync_snapshots()
            except Exception as error:  # noqa: BLE001 - report, do not crash
                _LOGGER.exception("snapshot for %s failed: %s", name, error)
                return {
                    "status": "error",
                    "message": "Blink would not take a new picture.",
                }

        _LOGGER.info("%s: new still requested from the UI", name)
        return {"status": "ok", "camera": self.cameras.get(name)}

    # -- camera names -----------------------------------------------------

    def _current_names(self):
        """Map each camera id to the name Blink gives it right now."""
        home = self.blink.homescreen or {}
        current = {}
        for key in ("cameras", "owls", "doorbells"):
            for device in home.get(key) or []:
                if device.get("id") is not None and device.get("name"):
                    current[str(device["id"])] = device["name"]
        return current

    def _current_systems(self):
        """Map each system's network id to the name Blink gives it right now."""
        home = self.blink.homescreen or {}
        return {
            str(network["id"]): network.get("name")
            for network in home.get("networks") or []
            if network.get("id") is not None
        }

    async def _rebuild(self):
        """Rebuild blinkpy's systems, sync modules and cameras from scratch.

        blinkpy files each sync module under its system's name and, rebuilding,
        only ever adds: a system renamed or removed leaves its old module in
        place, still polled every cycle and still holding the cameras it had -
        including ones since moved elsewhere and given new ids. Every module
        that still exists is replaced by a fresh one on a rebuild anyway, so
        starting from an empty table loses nothing but the stale ones.
        """
        held = self.blink.sync
        self.blink.sync = type(held)()
        if await self.blink.setup_post_verify():
            return True
        # Blink failed mid-rebuild. The old tables are wrong in places, but
        # they are better than none, and the next poll tries again.
        _LOGGER.warning("could not rebuild the camera list; keeping the old one")
        self.blink.sync = held
        return False

    async def _reconcile_cameras(self):
        """Keep our idea of the cameras, and blinkpy's, in step with Blink's.

        Returns True when anything about the line-up changed. blinkpy builds
        its cameras, motion flags, clip records and card-label table once, from
        the names and ids it saw at start-up, and never revisits them. A camera
        renamed, added, removed, or deleted and added back - which gives it a
        new id under the same name - leaves those tables describing a line-up
        that no longer exists, and it takes a restart to notice. So does a
        system added, renamed or removed, which is how cameras get moved from
        one sync module to another.
        """
        current = self._current_names()
        if not current:
            # A homescreen that came back empty or unreadable is no reason to
            # decide every camera has been deleted.
            return False

        systems = self._current_systems()
        regrouped = bool(
            systems and self._systems is not None and systems != self._systems
        )
        if regrouped:
            for network_id, name in systems.items():
                was = self._systems.get(network_id)
                if was is None:
                    _LOGGER.info("system new in Blink: %r", name)
                elif was != name:
                    _LOGGER.warning("system renamed in Blink: %r is now %r", was, name)
            for network_id, name in self._systems.items():
                if network_id not in systems:
                    _LOGGER.warning("system no longer in Blink: %r", name)

        held = {
            key: str(camera.camera_id) for key, camera in self.blink.cameras.items()
        }
        stale = {
            key: current[camera_id]
            for key, camera_id in held.items()
            if current.get(camera_id, key) != key
        }
        for old, new in stale.items():
            _LOGGER.warning("camera renamed in Blink: %r is now %r", old, new)

        by_name = {name: camera_id for camera_id, name in current.items()}
        replaced = {
            name: (held[name], camera_id)
            for name, camera_id in by_name.items()
            if name in held and held[name] != camera_id
        }
        for name, (was, now) in replaced.items():
            _LOGGER.warning(
                "camera %r was removed and added back: id %s is now %s", name, was, now
            )
        gone = [name for name in held if name not in by_name and name not in stale]
        added = [name for name in by_name if name not in held]
        for name in gone:
            _LOGGER.warning("camera no longer in Blink: %r", name)
        for name in added:
            _LOGGER.info("camera new in Blink: %r", name)

        rebuilt = True
        if stale or replaced or gone or added or regrouped:
            # The homescreen refresh() just fetched already describes the new
            # line-up, so this rebuilds from it without another login.
            rebuilt = await self._rebuild()
        # Only once blinkpy's tables match them: a rebuild that failed has to
        # see the same difference on the next poll, or it is never retried.
        if systems and rebuilt:
            self._systems = systems
        for name in gone:
            # Otherwise the grid keeps showing a card for a camera that is not
            # there any more, and nothing will ever update it.
            self.cameras.pop(name, None)
            self._digests.pop(name, None)

        changed = False
        for name, camera in self.blink.cameras.items():
            # A camera added back carries the same name on a new id, so the
            # names it used to go by have to be carried across first.
            was = self.names.id_for(name)
            if was not in (None, str(camera.camera_id)):
                # Also how a restart meets a camera moved to another system
                # while the app was stopped: same name, new id.
                _LOGGER.info(
                    "camera %r has a new id %s (was %s); keeping its history",
                    name,
                    camera.camera_id,
                    was,
                )
                self.names.supersede(camera.camera_id, name)
                changed = True
            old = self.names.observe(camera.camera_id, name)
            if old is not None:
                changed = True
                if old not in stale:
                    _LOGGER.warning(
                        "camera renamed while stopped: %r is now %r", old, name
                    )
                self._rename(old, name)
        if config.CAMERA_RENAMES and self.names.adopt(config.CAMERA_RENAMES):
            changed = True
        await self.names.save()
        self._alias_card_names()
        return changed or bool(stale or replaced or gone or added or regrouped)

    def _rename(self, old, new):
        """Carry what is held under a camera's old name over to its new one."""
        entry = self.cameras.pop(old, None)
        if entry is not None and new not in self.cameras:
            entry["name"] = new
            entry["slug"] = slugify(new)
            self.cameras[new] = entry
        if old in self._seen:
            self._seen.setdefault(new, self._seen.pop(old))
        # Forgotten rather than moved, so the picture is rewritten under the
        # new name's file instead of being skipped as unchanged.
        self._digests.pop(old, None)

    def _alias_card_names(self):
        """Let clips filed on a card under a former name reach their camera."""
        for name, camera in self.blink.cameras.items():
            table = getattr(camera.sync, "_names_table", None)
            if table is None:
                continue
            for former in self.names.former(name):
                # Never take a label a camera answers to today.
                table.setdefault(to_alphanumeric(former), name)

    def _slugs(self, name):
        """Return every archive directory name a camera's captures may be under."""
        return [slugify(each) for each in (name, *self.names.former(name))]

    def _archived(self, name, stamp):
        """Say whether this clip is already saved, under any name of its camera."""
        prefix = library.prefix_for(stamp)
        return any(
            self.library.find(f"{library.MOTION[0]}-{slug}-{prefix}") is not None
            for slug in self._slugs(name)
        )

    # -- motion -----------------------------------------------------------

    def _new_records(self, camera):
        """Return every clip Blink is reporting for this camera that is new.

        blinkpy hands back a list of records per refresh, but the camera
        object only remembers the last of them. One person walking past sets
        off a burst of clips seconds apart, so reading camera.last_record
        alone quietly threw away most of what was actually recorded - matched
        against the sync module's own storage, about half of it.
        """
        records = (getattr(camera.sync, "last_records", None) or {}).get(camera.name)
        cursor = library.parse_stamp(self._seen.get(camera.name))
        fresh = []
        for record in records or []:
            stamp = library.parse_stamp(record.get("time"))
            if stamp is None or (cursor and stamp <= cursor):
                continue
            fresh.append((stamp, record.get("time"), record.get("clip")))
        # Oldest first, so the cursor only ever moves forward and a run cut
        # short leaves the newest still to be found next time.
        fresh.sort(key=lambda item: item[0])
        return [(stamp, clip) for _, stamp, clip in fresh]

    async def _handle_motion(self, first_pass):
        """Turn any newly reported motion into a described event."""
        for name, camera in self.blink.cameras.items():
            records = self._new_records(camera)
            if not records:
                continue

            if first_pass and not config.CATCH_UP:
                self._seen[name] = records[-1][0]
                _LOGGER.info(
                    "%s: skipping %d pre-existing event(s)", name, len(records)
                )
                continue

            for stamp, clip_url in records:
                # Moved before the work, not after: a clip that cannot be
                # fetched must not be retried on every poll from here on.
                self._seen[name] = stamp
                _LOGGER.info("%s: motion at %s", name, stamp)
                await self._process_event(camera, stamp, clip_url)

    async def _fetch_clip(self, url):
        """Download one clip by URL, or return None."""
        if not url:
            return None
        if not url.startswith("http"):
            url = f"{self.blink.urls.base_url}{url}"
        try:
            response = await api.http_get(self.blink, url, json=False)
        except Exception as error:  # noqa: BLE001 - one clip is not the poll loop
            _LOGGER.warning("could not fetch %s: %s", url, error)
            return None
        if response and response.status == 200:
            return await response.read()
        _LOGGER.warning(
            "clip fetch returned %s for %s",
            getattr(response, "status", "no response"),
            url,
        )
        return None

    async def _process_event(self, camera, stamp, clip_url=None):
        """Save media for one event, describe it, and push it to the UI."""
        # refresh() already cached the thumbnail, so this costs nothing.
        snapshot = camera.image_from_cache

        if clip_url is None:
            clip = camera.video_from_cache
            if clip is None and camera.clip:
                response = await camera.get_video_clip()
                if response and response.status == 200:
                    clip = await response.read()
        else:
            # One clip out of a burst: the camera only caches the newest, so
            # every other one has to be asked for by its own URL.
            clip = await self._fetch_clip(clip_url)

        await self._capture(camera.name, stamp, clip, snapshot)

    async def _capture(self, camera_name, stamp, clip, snapshot):
        """Write one clip and its still to the archive, then describe it."""
        slug = slugify(camera_name)
        event = library.make_record(library.MOTION, camera_name, slug, stamp)
        prefix = event["prefix"]
        base = library.dir_for(library.MOTION, slug)

        # The poll and the sync button can both reach the same clip. Whichever
        # gets there second must not rewrite the media or describe it again.
        if self._archived(camera_name, stamp):
            _LOGGER.debug("%s: already have %s", camera_name, stamp)
            return None

        event["state"] = "describing"

        frames = []
        if snapshot:
            await media.write_bytes(base / f"{prefix}.jpg", snapshot)
            event["image"] = f"/media/events/{slug}/{prefix}.jpg"

        if clip:
            clip_path = base / f"{prefix}.mp4"
            await media.write_bytes(clip_path, clip)
            event["clip"] = f"/media/events/{slug}/{prefix}.mp4"
            with tempfile.TemporaryDirectory() as workdir:
                for frame in await media.extract_frames(clip_path, workdir):
                    frames.append(frame.read_bytes())

        if frames:
            # The snapshot above is Blink's stored thumbnail, which can be
            # months old - a frame lifted out of this clip is the only still
            # that actually shows what moved.
            await media.write_bytes(base / f"{prefix}-frame.jpg", frames[-1])
            event["image"] = f"/media/events/{slug}/{prefix}-frame.jpg"

        # Fall back to the snapshot when the clip yielded no frames.
        images = frames or ([snapshot] if snapshot else [])
        if not images:
            _LOGGER.warning("%s: no media available for %s", camera_name, stamp)
            return None

        # Made from whichever still won above, so the library's copy matches.
        await library.ensure_thumb(event)
        event["bytes"] = sum(
            path.stat().st_size for path in base.glob(f"{prefix}*") if path.is_file()
        )
        # Write the sidecar before announcing the event: a crash between the
        # two must not leave media on disk that nothing can find again.
        await library.write_sidecar(event)
        self.library.add(event)

        # Show the event immediately; the description lands a moment later.
        self._record(event)
        self._broadcast({"type": "motion", "event": event})
        if frames:
            self._promote_still(event)

        # Describing can take minutes. Run it off the poll loop so neither the
        # next check nor the snapshot button waits behind the model.
        text_path = base / f"{prefix}.txt"
        task = asyncio.create_task(self._describe(event, text_path, images))
        self._describing.add(task)
        task.add_done_callback(self._describing.discard)
        return event

    # -- pulling what Blink still holds -----------------------------------

    def sync_status(self):
        """Report on the manual pull of the clips Blink still has on file."""
        running = self._sync is not None and not self._sync.done()
        return {**self._sync_state, "running": running}

    def start_sync(self):
        """Fetch each camera's most recent clips, whatever the cursor says."""
        if self.blink is None or self.status in (
            "starting",
            "needs-credentials",
            "awaiting-2fa",
        ):
            return {
                "status": "error",
                "message": "Not connected to Blink.",
                **self.sync_status(),
            }
        status = self.sync_status()
        if status["running"]:
            return {"status": "ok", **status}
        self._sync_state = {"running": True, "found": 0, "saved": 0, "at": None}
        self._sync = asyncio.create_task(self._run_sync())
        return {"status": "ok", **self.sync_status()}

    def _broadcast_sync(self):
        self._broadcast({"type": "sync", "sync": self.sync_status()})

    async def _run_sync(self):
        """Save every recent clip Blink can still offer that we do not have."""
        saved = 0
        found = 0
        try:
            candidates = await self._recent_clips()
            found = len(candidates)
            self._sync_state["found"] = found
            _LOGGER.info("sync: %d clip(s) to fetch", found)
            self._broadcast_sync()

            for name, stamp, fetch in candidates:
                if self._closing:
                    break
                try:
                    clip = await fetch()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - one clip is not the run
                    _LOGGER.warning("sync: %s at %s failed: %s", name, stamp, error)
                    continue
                if clip is None:
                    continue
                camera = self.blink.cameras.get(name)
                thumb = camera.image_from_cache if camera else None
                if await self._capture(name, stamp, clip, thumb) is not None:
                    saved += 1
                    # A clip pulled by hand still has to move the poll cursor,
                    # or the next check offers it straight back.
                    seen = library.parse_stamp(self._seen.get(name))
                    if seen is None or library.parse_stamp(stamp) > seen:
                        self._seen[name] = stamp
                    self._sync_state["saved"] = saved
                    self._broadcast_sync()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - report, never crash the app
            _LOGGER.exception("sync failed: %s", error)
        finally:
            _LOGGER.info("sync: saved %d of %d", saved, found)
            self._sync_state = {
                "running": False,
                "found": found,
                "saved": saved,
                "at": _now(),
            }
            self._broadcast_sync()

    async def _recent_clips(self):
        """List the recent clips Blink still holds and we have not saved.

        Two places have to be asked. Clips reach the cloud feed only while a
        subscription is active; without one they live on the sync module's own
        storage instead, and that is a separate listing which the poll only
        reads through blinkpy's motion cursor.
        """
        found = {}

        def offer(name, stamp, fetch):
            if name not in self.blink.cameras:
                return
            parsed = library.parse_stamp(stamp)
            if parsed is None:
                return
            found.setdefault(name, {})[parsed] = (parsed.isoformat(), fetch)

        async with self._blink_lock:
            await self.blink.refresh(force=True)
            if await self._reconcile_cameras():
                self._broadcast(self.snapshot())

            for sync in self.blink.sync.values():
                try:
                    await sync.update_local_storage_manifest()
                except Exception as error:  # noqa: BLE001 - the cloud may still answer
                    _LOGGER.warning(
                        "sync: %s manifest unavailable: %s", sync.name, error
                    )
                    continue
                # blinkpy exposes no reader for the manifest it just built.
                store = getattr(sync, "_local_storage", {}) or {}
                for item in store.get("manifest") or []:
                    offer(
                        item.name,
                        item.created_at.isoformat(),
                        self._local_fetch(item),
                    )

            since = datetime.now(timezone.utc) - timedelta(days=config.SYNC_DAYS)
            for page in range(1, 6):
                try:
                    resp = await api.request_videos(
                        self.blink, time=since.timestamp(), page=page
                    )
                except Exception as error:  # noqa: BLE001 - local storage may cover it
                    _LOGGER.warning("sync: cloud feed unavailable: %s", error)
                    break
                entries = (resp or {}).get("media")
                if not entries:
                    break
                for entry in entries:
                    offer(
                        entry.get("device_name"),
                        entry.get("created_at"),
                        self._url_fetch(entry.get("media")),
                    )

        candidates = []
        for name, clips in found.items():
            # Newest first to choose which few to take, since the most recent
            # clip is the one somebody pressing this button wants to see.
            for parsed in sorted(clips, reverse=True)[: config.SYNC_LIMIT]:
                stamp, fetch = clips[parsed]
                if not self._archived(name, stamp):
                    candidates.append((parsed, name, stamp, fetch))

        # Then oldest first to save them, so a run cut short leaves the newest
        # to be found again rather than a hole in the middle.
        candidates.sort()
        return [(name, stamp, fetch) for _, name, stamp, fetch in candidates]

    def _local_fetch(self, item):
        """Return a callable that uploads one clip off the sync module."""

        async def fetch():
            # Local clips are not on Blink's servers until asked for.
            await item.prepare_download(self.blink)
            return await self._fetch_clip(item.url())

        return fetch

    def _url_fetch(self, url):
        """Return a callable that downloads one clip already in the cloud."""

        async def fetch():
            return await self._fetch_clip(url)

        return fetch

    def backfill_status(self):
        """Return what the description backlog looks like right now."""
        pending = self.library.needing_description(config.DESCRIBE_ATTEMPTS)
        return {
            "pending": len(pending),
            "running": self._backfill is not None and not self._backfill.done(),
        }

    def start_backfill(self):
        """Begin describing saved clips that have none. Returns the status."""
        status = self.backfill_status()
        if status["running"] or not status["pending"]:
            return status
        self._backfill = asyncio.create_task(self._run_backfill())
        status["running"] = True
        return status

    def start_thumbnails(self):
        """Begin making thumbnails for captures that have none."""
        if self._thumbs is None or self._thumbs.done():
            self._thumbs = asyncio.create_task(self._run_thumbnails())

    async def _run_thumbnails(self):
        """Give every saved capture a thumbnail, newest first.

        Only the archive from before thumbnails existed needs this - new
        captures get theirs as they are saved - so it runs once, off the
        critical path, and never needs Blink. Until a capture has one, the UI
        shows the full still, so being slow costs nothing but bandwidth.
        """
        missing = self.library.missing_thumbs()
        if not missing:
            return
        _LOGGER.info("thumbnails: %d captures have none yet", len(missing))
        made = 0
        for record in missing:
            if self._closing:
                break
            try:
                if await library.ensure_thumb(record):
                    made += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad still is not fatal
                _LOGGER.warning("thumbnail of %s failed: %s", record["id"], error)
            # ffmpeg is quick here, but a big archive is a lot of processes;
            # leave the poll loop and describing room to breathe.
            await asyncio.sleep(0.05)
        _LOGGER.info("thumbnails: made %d of %d", made, len(missing))

    async def _run_backfill(self):
        """Work through the events that have no description, oldest first."""
        pending = self.library.needing_description(config.DESCRIBE_ATTEMPTS)
        _LOGGER.info("backfill: %d saved clips have no description", len(pending))
        done = 0

        for record in pending:
            # The list was taken once; by now a run may have been asked to
            # stop, or the same event described by something else.
            if self._closing or record["summary"]:
                break
            try:
                if await self._redescribe(record):
                    done += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad clip is not fatal
                _LOGGER.exception("backfill of %s failed: %s", record["id"], error)
            # Live motion and the snapshot button share the model. Yield
            # between clips so a long backlog cannot monopolise it.
            await asyncio.sleep(config.BACKFILL_PAUSE)

        _LOGGER.info("backfill: described %d of %d", done, len(pending))

    async def _redescribe(self, record):
        """Describe one already-saved event. Returns True if a summary landed."""
        base = library.dir_for(record["kind"], record["slug"])
        prefix = record["prefix"]

        images = []
        clip_path = base / f"{prefix}.mp4"
        if clip_path.exists():
            with tempfile.TemporaryDirectory() as workdir:
                for frame in await media.extract_frames(clip_path, workdir):
                    images.append(frame.read_bytes())
        if not images:
            # No clip, or ffmpeg made nothing of it: the saved still is all
            # there is, and it is what the live path falls back to as well.
            for name in (f"{prefix}-frame.jpg", f"{prefix}.jpg"):
                still = base / name
                if still.exists():
                    images.append(still.read_bytes())
                    break
        if not images:
            _LOGGER.warning("nothing to describe for %s", record["id"])
            return False

        await self._describe(record, base / f"{prefix}.txt", images, backfill=True)
        return bool(record["summary"])

    async def _describe(self, event, text_path, images, backfill=False):
        """Caption one event and push the result to the UI."""
        event["describe_attempts"] = (event.get("describe_attempts") or 0) + 1
        async with self._describe_lock:
            try:
                reply = await vision.describe(self.session, images)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the media is already saved
                _LOGGER.exception("describing %s failed: %s", event["id"], error)
                reply = None

        summary = reply["summary"] if reply else None
        if reply and reply["subjects"] is not None:
            event["subjects"] = reply["subjects"]
            event["subjects_from"] = "model"
            event["people_count"] = reply["people_count"]
        elif summary:
            # The model answered in prose. Its own words are still the best
            # evidence available, so read them the way the archive does.
            event["subjects"] = library.subjects_from_text(summary)
            event["subjects_from"] = "text"

        event["summary"] = summary
        event["state"] = "described" if summary else "no-description"

        # Only worth asking who when something said a person was there. Two
        # thirds of events have one, so the other third never pays for this.
        if "person" in (event.get("subjects") or ()):
            await self._identify(event, images)
        if summary:
            await media.write_bytes(text_path, summary.encode())
            _LOGGER.info("%s: %s", event["camera"], summary)
        else:
            _LOGGER.info("%s: media saved, no description available", event["camera"])

        # Same record object the library indexed, so this only has to be
        # re-persisted, not re-added.
        await library.write_sidecar(event)

        # A description arriving for a clip captured weeks ago must not be
        # mistaken for something that just happened: no card flash, no jump to
        # the camera, no reordering of a timeline it is not the newest of.
        self._broadcast({"type": "summary", "event": event, "backfill": backfill})

        # Working through the backlog must never notify. Those clips are days
        # or weeks old, and a phone buzzing fifty times says nothing useful.
        if not backfill:
            await notify.send(self.session, event, image=images[-1] if images else None)

    async def confirm_person(self, record, name):
        """Record that somebody has confirmed who is in one capture.

        Stored in the same field as the model's guess but marked "confirmed",
        so the UI can stop hedging about it and a later identification does
        not quietly overwrite what a person said.
        """
        named = list(record.get("who") or [])
        if name not in named:
            named.append(name)
        record["who"] = sorted(named)
        record["who_confidence"] = "confirmed"
        await library.write_sidecar(record)
        # Old capture, new information: the same shape as a late description,
        # so timelines update the row in place rather than treating it as news.
        self._broadcast({"type": "summary", "event": record, "backfill": True})
        return record

    async def reference_frame(self, record):
        """Return the best still for one capture, for use as a reference.

        The frame cut out of the clip is preferred over Blink's thumbnail: the
        thumbnail can predate the event by months, so it is often a picture of
        an empty yard rather than of the person being enrolled.
        """
        base = library.dir_for(record["kind"], record["slug"])
        for name in (f"{record['prefix']}-frame.jpg", f"{record['prefix']}.jpg"):
            path = base / name
            if path.exists():
                return await asyncio.to_thread(path.read_bytes)
        return None

    async def _identify(self, event, images):
        """Guess which known people are in these frames, if any."""
        if not config.IDENTIFY:
            return
        gallery = await asyncio.to_thread(self.people.gallery)
        if not gallery:
            return

        async with self._describe_lock:
            try:
                result = await vision.identify(self.session, images, gallery)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the description stands
                _LOGGER.exception("identifying %s failed: %s", event["id"], error)
                return
        if result is None:
            return

        # Only names actually enrolled: a paraphrase or an invented one must
        # never reach the UI looking like somebody was recognised.
        named = self.people.resolve(result["who"])
        if named and not _confident_enough(result["confidence"]):
            _LOGGER.info(
                "%s: %s guess for %s not confident enough - treating as unknown",
                event["camera"],
                result["confidence"],
                ", ".join(named),
            )
            named = []

        if event.get("who_confidence") == "confirmed":
            # Somebody already said who this is. A guess does not get to
            # overrule them.
            return

        event["who"] = named
        event["who_confidence"] = result["confidence"] if named else None
        if event["who"]:
            _LOGGER.info(
                "%s: looks like %s (%s) - %s",
                event["camera"],
                ", ".join(event["who"]),
                result["confidence"],
                result["why"],
            )

    def _promote_still(self, event):
        """Show an event's clip frame in the grid, and say when it was taken."""
        entry = self.cameras.get(event["camera"])
        if not entry:
            return
        entry["image"] = event["image"]
        entry["captured"] = event["stamp"]
        entry["source"] = "clip"
        self._broadcast({"type": "snapshot", "camera": entry})

    def _record(self, event):
        self.events.appendleft(event)
        entry = self.cameras.get(event["camera"])
        if entry:
            entry["last_event"] = event
