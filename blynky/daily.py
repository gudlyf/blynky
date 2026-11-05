"""A short account of each camera's day, written from that day's descriptions.

The archive already says what happened in every clip, but a busy camera logs
dozens a day and nobody reads them all. Once a day is over, the descriptions
of its clips go to the model - as text, no frames - and what comes back is
kept beside the archive, one small file per camera per day:

    daily/<camera slug>/<YYYY-MM-DD>.json

The same can be written for each location - a Blink system, every camera on
it together - and for every camera at once, kept the same way under keys no
camera's slug can take, since a slug never starts with an underscore:

    daily/_location-<system slug>/<YYYY-MM-DD>.json
    daily/_all/<YYYY-MM-DD>.json

A summary is only ever written from descriptions that already exist, so it
costs one text-only call per camera, location or whole day. It outlives the
footage it was written from: retention prunes clips, never these.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

from slugify import slugify

from . import config, library, vision

_LOGGER = logging.getLogger(__name__)


# What each kind of summary covers, in the order the settings menu lists them.
SCOPES = ("camera", "location", "all")
ALL = "_all"
_LOCATION = "_location-"


def kind_of(key):
    """Say what a summary's key covers: "camera", "location" or "all"."""
    if key == ALL:
        return "all"
    if key.startswith(_LOCATION):
        return "location"
    return "camera"


def location_key(name):
    """Return the key a location's summaries are kept under."""
    return f"{_LOCATION}{slugify(name)}"


def _local(stamp):
    """Return a capture's time in the server's time zone, or None."""
    parsed = library.parse_stamp(stamp)
    return parsed.astimezone() if parsed else None


def today():
    """Return today's date in the server's time zone."""
    return datetime.now().astimezone().date()


def parse_day(value):
    """Parse a YYYY-MM-DD string into a date, or None."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _path(slug, day):
    return config.DAILY_DIR / slug / f"{day}.json"


def _write_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_name(f".{path.name}.tmp")
    spare.write_text(payload, encoding="utf-8")
    spare.replace(path)


def _sample(records, limit):
    """Pick at most `limit` records, spread evenly over the day."""
    if limit <= 0 or len(records) <= limit:
        return records
    step = len(records) / limit
    return [records[int(i * step)] for i in range(limit)]


def lines_for(records, cameras=False):
    """Format a day's described clips as the model is shown them, oldest first.

    With `cameras`, each line says which camera it came from, for a summary
    written from several.
    """
    ordered = sorted(records, key=lambda record: _local(record["stamp"]))
    return [
        f"{_local(record['stamp']):%H:%M} "
        f"{record['camera'] + ': ' if cameras else ''}"
        f"{' '.join(record['summary'].split())}"
        for record in _sample(ordered, config.DAILY_MAX_CLIPS)
    ]


# Roughly four characters a token, plus the prompts and headings every request
# carries. Only for the warning before a backfill: an estimate, said as one.
_OVERHEAD_TOKENS = 250


def _tokens(key, records):
    picked = _sample(records, config.DAILY_MAX_CLIPS)
    # A line from several cameras carries the camera's name as well.
    named = kind_of(key) != "camera"
    chars = sum(
        len(record["summary"]) + 7 + (len(record["camera"]) + 2 if named else 0)
        for record in picked
    )
    return _OVERHEAD_TOKENS + chars // 4


class Daily:
    """The summaries on disk, and whether new ones are being written."""

    def __init__(self, locate=None):
        """Start empty; call load() before serving.

        `locate` returns {camera slug: location name} for the cameras Blink
        reports now, under every slug their captures may be filed under.
        """
        self._entries = {}
        self._locate = locate or dict
        self.enabled = config.DAILY_SUMMARIES
        # Whether the summary written each night is also pushed to ntfy.
        self.notify = config.NTFY_DAILY
        # Which kinds of summary are written, from SCOPES.
        self.scopes = list(config.DAILY_SCOPES)

    # -- persistence ------------------------------------------------------

    async def load(self):
        """Read every saved summary, and the switch from the settings menu."""
        self._entries = await asyncio.to_thread(self._scan)
        try:
            text = await asyncio.to_thread(
                config.DAILY_STATE.read_text, encoding="utf-8"
            )
            stored = json.loads(text)
            self.enabled = bool(stored.get("enabled"))
            self.notify = bool(stored.get("notify", self.notify))
            scopes = [s for s in SCOPES if s in (stored.get("scopes") or [])]
            if scopes:
                self.scopes = scopes
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as error:
            _LOGGER.warning("ignoring unreadable %s: %s", config.DAILY_STATE, error)
        _LOGGER.info(
            "daily summaries: %s (by %s), %d saved",
            "on" if self.enabled else "off",
            ", ".join(self.scopes),
            len(self._entries),
        )

    def _scan(self):
        entries = {}
        root = config.DAILY_DIR
        if not root.is_dir():
            return entries
        for path in root.glob("*/*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                _LOGGER.warning("ignoring unreadable %s: %s", path, error)
                continue
            if isinstance(entry, dict) and entry.get("summary"):
                entries[(path.parent.name, path.stem)] = entry
        return entries

    async def set_enabled(self, enabled):
        """Turn writing summaries on or off, and remember it across restarts."""
        self.enabled = bool(enabled)
        await self._save()
        _LOGGER.info("daily summaries turned %s", "on" if self.enabled else "off")

    async def set_notify(self, notify):
        """Turn pushing each night's summaries to ntfy on or off."""
        self.notify = bool(notify)
        await self._save()
        _LOGGER.info("daily summaries to ntfy %s", "on" if self.notify else "off")

    async def set_scopes(self, scopes):
        """Choose which kinds of summary are written. Needs at least one."""
        chosen = [scope for scope in SCOPES if scope in scopes]
        if not chosen:
            raise ValueError("pick at least one of " + ", ".join(SCOPES))
        self.scopes = chosen
        await self._save()
        _LOGGER.info("daily summaries by %s", ", ".join(self.scopes))

    async def _save(self):
        payload = json.dumps(
            {"enabled": self.enabled, "notify": self.notify, "scopes": self.scopes}
        )
        try:
            await asyncio.to_thread(_write_atomic, config.DAILY_STATE, payload)
        except OSError as error:
            # Still switched for as long as this process runs.
            _LOGGER.warning("could not write %s: %s", config.DAILY_STATE, error)

    def entry(self, key, day):
        """Return the stored summary for one camera's day, or None."""
        return self._entries.get((key, day))

    # -- reading ----------------------------------------------------------

    def _locations(self):
        """Map each camera slug to its location's (key, name)."""
        return {
            slug: (location_key(name), name)
            for slug, name in (self._locate() or {}).items()
            if name
        }

    def groups(self):
        """List the summaries beyond each camera's, for the UI to show."""
        found = {ALL: {"key": ALL, "kind": "all", "name": "All cameras"}}
        for key, name in sorted(self._locations().values(), key=lambda kv: kv[1]):
            found.setdefault(key, {"key": key, "kind": "location", "name": name})
        return list(found.values())

    def _group(self, records, key=None):
        """Bucket motion by summary and local day: {(key, day): [records]}.

        Each clip counts towards its camera, its location and the whole day.
        With `key`, only that one summary's days; without, every summary of
        a kind that is being written.
        """
        locations = self._locations()
        days = {}
        for record in records:
            if record["kind"] != library.MOTION:
                continue
            when = _local(record["stamp"])
            if when is None:
                continue
            keys = [record["slug"], ALL]
            if record["slug"] in locations:
                keys.append(locations[record["slug"]][0])
            for held in keys:
                if key is not None and held != key:
                    continue
                if key is None and kind_of(held) not in self.scopes:
                    continue
                days.setdefault((held, when.date().isoformat()), []).append(record)
        return days

    def _name(self, key, records):
        """Return what a summary is about, as a person would call it."""
        kind = kind_of(key)
        if kind == "all":
            return "All cameras"
        if kind == "location":
            for held, name in self._locations().values():
                if held == key:
                    return name
            for (held, _), entry in self._entries.items():
                if held == key and entry.get("camera"):
                    return entry["camera"]
            return key[len(_LOCATION) :]
        return max(records, key=lambda r: _local(r["stamp"]))["camera"]

    def _day(self, slug, day, records, current):
        """Describe one camera's day the way the UI lists it."""
        described = [record for record in records if record["summary"]]
        entry = self._entries.get((slug, day))
        return {
            "date": day,
            "today": day >= current,
            "clips": len(records),
            "described": len(described),
            "summary": entry["summary"] if entry else None,
            "at": entry.get("at") if entry else None,
            # Descriptions that landed after the summary was written, such as
            # a backfill that finished later: it can be written again.
            "stale": bool(entry) and len(described) > (entry.get("described") or 0),
        }

    def days(self, all_records, slug, before=None, limit=14):
        """List a camera's days, newest first, from just before `before`.

        Every day with motion is listed, and every day with a summary, so a
        summary is still shown once retention has pruned the clips behind it.
        """
        grouped = self._group(all_records, slug)
        dates = {day for _, day in grouped}
        dates.update(day for held, day in self._entries if held == slug)
        dates = [day for day in dates if not before or day < before]
        ordered = sorted(dates, reverse=True)
        current = today().isoformat()
        page = [
            self._day(slug, day, grouped.get((slug, day), []), current)
            for day in ordered[:limit]
        ]
        return {"days": page, "more": len(ordered) > limit}

    def day(self, all_records, slug, day):
        """Return one camera's day as days() lists it."""
        records = self._group(all_records, slug).get((slug, day), [])
        return self._day(slug, day, records, today().isoformat())

    def pending(self, all_records):
        """Return the past days that could have a summary and have none.

        Oldest first, as (slug, day, records), for the same reason the
        description backlog drains that way. Today is never one: it is not
        over yet.
        """
        current = today().isoformat()
        found = []
        for (slug, day), records in self._group(all_records).items():
            if day >= current or (slug, day) in self._entries:
                continue
            described = [record for record in records if record["summary"]]
            if described:
                found.append((slug, day, described))
        found.sort(key=lambda item: (item[1], item[0]))
        return found

    def backlog(self, all_records):
        """Count the days without a summary, and roughly what they would cost."""
        found = self.pending(all_records)
        return {
            "pending": len(found),
            "clips": sum(len(records) for _, _, records in found),
            "tokens": sum(_tokens(key, records) for key, _, records in found),
        }

    # -- writing ----------------------------------------------------------

    async def write(self, session, all_records, slug, day):
        """Summarize one camera's day. Returns the listed day, or None.

        None means the model gave nothing back, and the log says why. A day
        with no descriptions to write from raises LookupError instead: asking
        the model would cost a request to learn nothing.
        """
        records = self._group(all_records, slug).get((slug, day), [])
        described = [record for record in records if record["summary"]]
        if not described:
            raise LookupError(f"{slug} has no descriptions on {day}")
        kind = kind_of(slug)
        camera = self._name(slug, described)
        group = None
        if kind != "camera":
            cameras = sorted({record["camera"] for record in described})
            group = (
                f"Location: {camera} (cameras: {', '.join(cameras)})"
                if kind == "location"
                else f"Every camera: {', '.join(cameras)}"
            )
        summary = await vision.summarize_day(
            session,
            camera,
            date.fromisoformat(day),
            lines_for(described, cameras=group is not None),
            group=group,
        )
        if not summary:
            return None

        entry = {
            "slug": slug,
            "kind": kind,
            # Whatever it covers, as a person would call it: the camera's
            # name, the location's, or "All cameras".
            "camera": camera,
            "date": day,
            "summary": summary,
            # How many descriptions it was written from, so a later one can
            # mark it as out of date.
            "described": len(described),
            "clips": len(records),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": config.VISION_MODEL,
        }
        try:
            await asyncio.to_thread(
                _write_atomic, _path(slug, day), json.dumps(entry, indent=1)
            )
        except OSError as error:
            # Kept in memory, so it is not asked for twice before a restart.
            _LOGGER.warning("could not write %s: %s", _path(slug, day), error)
        self._entries[(slug, day)] = entry
        _LOGGER.info("daily: %s on %s: %s", camera, day, summary)
        return self._day(slug, day, records, today().isoformat())


def yesterday():
    """Return yesterday's date as YYYY-MM-DD, in the server's time zone."""
    return (today() - timedelta(days=1)).isoformat()
