"""The on-disk archive of every clip and still the monitor has captured.

Media has always been written under DATA_DIR, but nothing remembered it: the
event feed lived in a capped deque that a restart emptied, so a month of saved
clips was invisible the moment the process stopped. This module owns the
archive instead. Each capture gets a small JSON sidecar next to its media, the
whole tree is scanned once at start-up, and the resulting index is what the
library view pages through.

Sidecars are written alongside the media rather than into a single database so
that the archive stays self-describing: a directory of files carries
everything needed to rebuild the index, and losing the index costs nothing.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

from slugify import slugify

from . import config, media

_LOGGER = logging.getLogger(__name__)

# What a captured still or clip is: motion Blink reported, or a picture
# someone asked for with the refresh button.
MOTION = "motion"
STILL = "still"

# A small copy of a capture's still, next to it, for the library and the
# timelines. Derived entirely from the still, so it can always be remade.
THUMB_SUFFIX = "-thumb.jpg"

# Legacy filenames are slugified ISO stamps - "2026-08-04t12-58-07-00-00".
# Slugify drops the offset's sign, so a stamp parsed back out of a filename is
# read as UTC; every stamp Blink reports is UTC, and any sidecar written since
# carries the real value anyway.
_PREFIX = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})t(\d{2})-(\d{2})-(\d{2})(?:-(\d{2})-(\d{2}))?$"
)

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def prefix_for(stamp):
    """Return the filename stem used for a capture at this ISO stamp."""
    return slugify(stamp)


def parse_stamp(value):
    """Parse an ISO stamp into an aware datetime, or None."""
    if not value:
        return None
    # Python before 3.11 refuses the "Z" that JavaScript's toISOString() and
    # most other writers use for UTC.
    if isinstance(value, str) and value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp_from_prefix(prefix):
    """Recover an ISO stamp from a legacy filename stem, or None."""
    match = _PREFIX.match(prefix)
    if not match:
        return None
    year, month, day, hour, minute, second, off_h, off_m = match.groups()
    offset = f"+{off_h}:{off_m}" if off_h else "+00:00"
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}{offset}"


def _titleize(slug):
    """Turn a camera slug back into something readable.

    Only used for media captured before this process learned the camera's real
    name; apply_names() replaces it as soon as Blink tells us.
    """
    return " ".join(word.capitalize() for word in slug.split("-")) or slug


def dir_for(kind, slug):
    """Return the directory holding one camera's media of this kind."""
    root = config.EVENT_DIR if kind == MOTION else config.STILL_DIR
    return root / slug


def _media_url(kind, slug, name):
    """Return the browser-facing URL for a file in the archive."""
    branch = "events" if kind == MOTION else "stills"
    return f"/media/{branch}/{slug}/{name}"


def make_record(kind, camera, slug, stamp, **extra):
    """Build an archive record for one capture."""
    prefix = prefix_for(stamp)
    record = {
        "id": f"{kind[0]}-{slug}-{prefix}",
        "kind": kind,
        "camera": camera,
        "slug": slug,
        "stamp": stamp,
        "prefix": prefix,
        "image": None,
        # The same picture as `image`, small: what a list of captures loads.
        # None until one has been made; the UI falls back to `image`.
        "thumb": None,
        "clip": None,
        "summary": None,
        "state": None,
        # What the model reported seeing, from config.SUBJECTS. None means it
        # never said - which is not the same as saying the scene was empty.
        "subjects": None,
        "people_count": None,
        # Whether subjects came from the model ("model") or were inferred from
        # the wording of an older description ("text").
        "subjects_from": None,
        # Finer tags the model added, from config.DETAILS. None for anything
        # described before it was asked for them.
        "details": None,
        # Whether details and night came from the model ("model") or were read
        # out of the description's wording ("text"), like subjects_from.
        "details_from": None,
        # Whether the footage was dark or infrared, as the model judged it.
        "night": None,
        # Every tag, derived from subjects, details and who by tags_for(). Not
        # stored: the sidecar keeps what it was derived from instead.
        "tags": [],
        # How many times describing this has been attempted, so a clip the
        # model can never handle is not retried on every restart.
        "describe_attempts": 0,
        # Known people the model thinks it recognised, and how sure it was.
        # Always a guess: the UI must never present it as fact.
        "who": None,
        "who_confidence": None,
        # Stills only: what the image hashed to, so an unchanged picture is
        # recognised as one we already hold.
        "digest": None,
        "bytes": 0,
    }
    record.update(extra)
    return record


# The words an older description uses for a human being. This exists to seed
# `subjects` for the events captured before the model was asked for them; it
# is a fallback, and marked as one, never a substitute for the model's answer.
_PEOPLE = re.compile(
    r"(\b(?:no|without)\s+)?"
    r"\b(?:persons?|people|man|men|woman|women|child(?:ren)?|kids?|boys?"
    r"|girls?|someone|somebody|individuals?|adults?)\b",
    re.IGNORECASE,
)


def subjects_from_text(summary):
    """Guess whether a description mentions a person. Returns a subject list.

    Only people are inferred: they are what the UI filters on, and the other
    subjects have no reader that would benefit from a guess.
    """
    if not summary:
        return []
    for match in _PEOPLE.finditer(summary):
        # "no people, animals or vehicles visible" is how the model reports an
        # empty clip, and it must not be read as the opposite.
        if not match.group(1):
            return ["person"]
    return []


# The words a description uses for each finer tag. Read out of descriptions
# written before the model was asked for tags, so the whole archive can be
# searched by them without describing anything again.
_DETAIL_WORDS = {
    "dog": r"dogs?|pupp(?:y|ies)",
    "cat": r"cats?|kittens?",
    "deer": r"deer|fawns?",
    "bird": r"birds?|crows?|robins?|turkeys?|geese|goose|ducks|hawks?|pigeons?"
    r"|sparrows?",
    "bicycle": r"bicycles?|bikes?|cyclists?|bicyclists?",
    # A courier, or something being dropped off - not a FedEx truck driving
    # past on the road, which is no delivery to this house.
    "delivery": r"(?:delivery|ups|fedex|usps|amazon|dhl)\s+"
    r"(?:driver|person|worker|man|men|courier|uniform)s?"
    r"|couriers?|mail\s?carriers?|mail(?:man|men)|delivering|delivers"
    r"|(?:drops?|dropped|leaves|left|places?|placed|sets?)\s+(?:off\s+)?"
    r"(?:a|an|the|two|several)\s+(?:\w+\s+)?(?:package|parcel|box)(?:e?s)?",
}
_DETAILS_IN_TEXT = {
    name: re.compile(rf"\b(?:{words})\b", re.IGNORECASE)
    for name, words in _DETAIL_WORDS.items()
}
_NIGHT_IN_TEXT = re.compile(
    r"\b(?:night(?:time)?|infrared|night[- ]vision|black[- ]and[- ]white"
    r"|gr[ae]yscale)\b",
    re.IGNORECASE,
)
# "No people, animals, or deliveries appear" is how the model reports an empty
# clip. A negating word earlier in the sentence cancels a match, unless a
# "but" or "while" turns the sentence around in between.
_NEGATION = re.compile(r"\b(?:no|nothing|without|not|neither|nor|none)\b", re.I)
_TURN = re.compile(r"\bbut\b|\bwhile\b|\bthough\b|;", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?]+")


def _negated(sentence, at):
    """Whether a match at this offset sits in a negated part of the sentence."""
    before = sentence[:at]
    negation = max((m.end() for m in _NEGATION.finditer(before)), default=-1)
    if negation < 0:
        return False
    return not any(m.start() > negation for m in _TURN.finditer(before))


def details_from_text(summary):
    """Guess the finer tags a description mentions, in config order."""
    found = set()
    for sentence in _SENTENCE.finditer(summary or ""):
        text = sentence.group()
        for name, pattern in _DETAILS_IN_TEXT.items():
            if name not in found and any(
                not _negated(text, match.start()) for match in pattern.finditer(text)
            ):
                found.add(name)
    return [name for name in config.DETAILS if name in found]


def infer_details(record):
    """Fill in details and night from a record's description, marked as such.

    Night is never negated: "nothing of note in this nighttime clip" is still
    a night clip. Only ever True or unset - a description that does not say
    it was dark is not evidence that it was light.
    """
    record["details"] = details_from_text(record["summary"])
    record["details_from"] = "text"
    if _NIGHT_IN_TEXT.search(record["summary"] or ""):
        record["night"] = True


def tag_for_name(name):
    """Return the tag a known person's name is filed under."""
    return slugify(name)


def tags_for(record):
    """Return a record's tags, without the leading #, in a stable order.

    Subjects first, then the finer details, then the derived ones, then
    whoever was recognised. All of them come from closed lists - the subjects
    and details the model may pick from, a few fixed words, and the people
    enrolled - so a tag search never has to guess at a word the model made up.
    """
    subjects = record.get("subjects") or ()
    tags = [name for name in config.SUBJECTS if name in subjects]
    if (record.get("people_count") or 0) > 1:
        tags.append("group")
    tags += [name for name in config.DETAILS if name in (record.get("details") or ())]
    if record.get("night") is True:
        tags.append("night")
    who = record.get("who")
    # An empty list, not None: identification was asked and found nobody it
    # knew. None means it never ran - switched off, or nobody enrolled yet -
    # and calling everyone a stranger then would only be noise.
    if "person" in subjects and who == []:
        tags.append("unknown")
    for name in who or ():
        tag = tag_for_name(name)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


# Every field a sidecar leaves out: derived from the rest, or from the disk.
_UNSTORED = {"bytes", "tags"}


async def write_sidecar(record):
    """Persist a record next to its media so a restart can find it again.

    Also refreshes the record's tags. Everything that changes what a record
    is tagged with saves it straight after, so this is the one place that
    cannot be missed - and it happens before the change is broadcast.
    """
    record["tags"] = tags_for(record)
    path = dir_for(record["kind"], record["slug"]) / f"{record['prefix']}.json"
    payload = json.dumps(
        {k: v for k, v in record.items() if k not in _UNSTORED}, indent=1
    )
    try:
        await asyncio.to_thread(_write_text, path, payload)
    except OSError as error:  # noqa: BLE001 - the media itself is already safe
        _LOGGER.warning("could not write %s: %s", path, error)


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _still_path(record):
    """Return the file behind a record's image, or None."""
    if not record.get("image"):
        return None
    name = record["image"].split("?", 1)[0].rsplit("/", 1)[-1]
    return dir_for(record["kind"], record["slug"]) / name


def _thumb_is_current(source, target):
    """Whether a thumbnail exists and was made from the still as it is now."""
    try:
        return target.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def _write_atomic(path, payload):
    # A dot-name the scanner ignores, then a rename: the browser can only ever
    # fetch a complete thumbnail or none at all.
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_bytes(payload)
    os.replace(temp, path)


async def ensure_thumb(record):
    """Make the record's thumbnail if it is missing or older than its still.

    Sets record["thumb"] whenever one exists afterwards, and returns whether a
    new one was written. Safe to call as often as you like.
    """
    source = _still_path(record)
    if source is None:
        return False
    target = source.parent / f"{record['prefix']}{THUMB_SUFFIX}"
    url = _media_url(record["kind"], record["slug"], target.name)
    if await asyncio.to_thread(_thumb_is_current, source, target):
        record["thumb"] = url
        return False
    payload = await media.thumbnail(source)
    if payload is None:
        return False
    try:
        await asyncio.to_thread(_write_atomic, target, payload)
    except OSError as error:
        _LOGGER.warning("could not write %s: %s", target, error)
        return False
    record["thumb"] = url
    return True


def _scan_camera_dir(directory, kind, names):
    """Build records for one camera directory."""
    slug = directory.name
    # prefix -> {role: (filename, size)}
    groups = {}
    for entry in os.scandir(directory):
        if not entry.is_file():
            continue
        name = entry.name
        if name.endswith(THUMB_SUFFIX):
            prefix, role = name[: -len(THUMB_SUFFIX)], "thumb"
        elif name.endswith("-frame.jpg"):
            prefix, role = name[: -len("-frame.jpg")], "frame"
        elif name.endswith(".jpg") or name.endswith(".jpeg"):
            prefix, role = name.rsplit(".", 1)[0], "snapshot"
        elif name.endswith(".mp4"):
            prefix, role = name[:-4], "clip"
        elif name.endswith(".txt"):
            prefix, role = name[:-4], "text"
        elif name.endswith(".json"):
            prefix, role = name[:-5], "sidecar"
        else:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        groups.setdefault(prefix, {})[role] = (name, size)

    records = []
    for prefix, parts in groups.items():
        record = _record_from_parts(kind, slug, prefix, parts, names)
        if record:
            records.append(record)
    return records


def _record_from_parts(kind, slug, prefix, parts, names):
    """Turn one prefix's files into a record, or None if there is no media."""
    sidecar = parts.get("sidecar")
    stored = {}
    if sidecar:
        path = dir_for(kind, slug) / sidecar[0]
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            _LOGGER.warning("ignoring unreadable sidecar %s: %s", path, error)
            stored = {}

    stamp = stored.get("stamp") or _stamp_from_prefix(prefix)
    if not stamp:
        # Not something this app wrote; leave it alone rather than guess.
        return None

    record = make_record(
        stored.get("kind") or kind,
        stored.get("camera") or names.get(slug) or _titleize(slug),
        slug,
        stamp,
    )
    # A sidecar written before the file was renamed or moved should never win
    # over what is actually on disk, so media URLs are always re-derived.
    frame, snapshot, clip, text = (
        parts.get("frame"),
        parts.get("snapshot"),
        parts.get("clip"),
        parts.get("text"),
    )
    still = frame or snapshot
    if still:
        record["image"] = _media_url(record["kind"], slug, still[0])
    if clip:
        record["clip"] = _media_url(record["kind"], slug, clip[0])
    if not (still or clip):
        return None
    thumb = parts.get("thumb")
    if thumb and still:
        record["thumb"] = _media_url(record["kind"], slug, thumb[0])

    summary = stored.get("summary")
    if summary is None and text:
        try:
            summary = (dir_for(kind, slug) / text[0]).read_text(encoding="utf-8")
        except OSError:
            summary = None
    record["summary"] = (summary or "").strip() or None
    record["state"] = stored.get("state") or (
        "described" if record["summary"] else "no-description"
    )
    # A run that ended mid-description leaves "describing" behind forever;
    # from the archive's point of view it simply has no summary.
    if record["state"] == "describing":
        record["state"] = "no-description"

    # Anything the sidecar knows that make_record defaulted has to be carried
    # over explicitly, or it is silently lost on every restart.
    record["people_count"] = stored.get("people_count")
    record["describe_attempts"] = stored.get("describe_attempts") or 0
    who = stored.get("who")
    record["who"] = who if isinstance(who, list) else None
    record["who_confidence"] = stored.get("who_confidence")
    subjects = stored.get("subjects")
    if isinstance(subjects, list):
        record["subjects"] = subjects
        record["subjects_from"] = stored.get("subjects_from") or "model"
    elif record["summary"]:
        # Written before the model was asked for subjects. The wording is all
        # there is to go on, so say so rather than passing it off as observed.
        record["subjects"] = subjects_from_text(record["summary"])
        record["subjects_from"] = "text"
    details = stored.get("details")
    night = stored.get("night")
    if isinstance(details, list) and stored.get("details_from") != "text":
        record["details"] = details
        record["details_from"] = "model"
        record["night"] = night if isinstance(night, bool) else None
    elif record["summary"]:
        # Described before the model was asked for tags. Read from the wording
        # on every load, even if a guess was saved since, so a better word
        # list here reaches the whole archive on the next restart.
        infer_details(record)
    record["tags"] = tags_for(record)

    record["digest"] = stored.get("digest")
    record["bytes"] = sum(size for _, size in parts.values())
    return record


class Library:
    """An in-memory index over the archive on disk."""

    def __init__(self):
        """Start empty; call load() before serving."""
        self._records = []
        self._by_id = {}
        self.loaded = False

    # -- building ---------------------------------------------------------

    async def load(self, names=None):
        """Scan the archive from disk, replacing whatever is indexed now."""
        records = await asyncio.to_thread(self._scan, names or {})
        self._records = records
        self._by_id = {record["id"]: record for record in records}
        self._sort()
        self.loaded = True
        _LOGGER.info(
            "archive: %s captures across %s camera(s), %s",
            len(records),
            len({record["slug"] for record in records}),
            human_bytes(sum(record["bytes"] for record in records)),
        )
        return records

    def _scan(self, names):
        records = []
        for kind, root in ((MOTION, config.EVENT_DIR), (STILL, config.STILL_DIR)):
            if not root.is_dir():
                continue
            for directory in sorted(root.iterdir()):
                if directory.is_dir():
                    records.extend(_scan_camera_dir(directory, kind, names))
        return records

    def _sort(self):
        self._records.sort(key=_sort_key, reverse=True)

    def add(self, record):
        """Index a freshly captured record, or replace one already there."""
        existing = self._by_id.get(record["id"])
        if existing is not None:
            existing.update(record)
            return existing
        self._by_id[record["id"]] = record
        self._records.append(record)
        self._sort()
        return record

    def all(self):
        """Return every record, newest first."""
        return list(self._records)

    def find(self, event_id):
        """Return one record by id, or None."""
        for record in self._records:
            if record["id"] == event_id:
                return record
        return None

    def remove(self, record):
        """Drop one record from the index. Returns True if it was there."""
        for position, held in enumerate(self._records):
            if held["id"] == record["id"]:
                del self._records[position]
                return True
        return False

    def apply_names(self, mapping):
        """Adopt real camera names once Blink has told us what they are."""
        for record in self._records:
            name = mapping.get(record["slug"])
            if name:
                record["camera"] = name

    # -- reading ----------------------------------------------------------

    def recent(self, limit, kind=MOTION):
        """Return the newest records, newest first."""
        picked = [r for r in self._records if kind is None or r["kind"] == kind]
        return picked[:limit]

    def needing_description(self, max_attempts):
        """Return saved motion that has no description, oldest first.

        Oldest first because this is a backlog: it should drain in the order
        the events happened, and a run that is cut short leaves the newest -
        the ones someone is most likely to look at - already done.
        """
        pending = [
            record
            for record in self._records
            if record["kind"] == MOTION
            and not record["summary"]
            and (record["clip"] or record["image"])
            and (record["describe_attempts"] or 0) < max_attempts
        ]
        return pending[::-1]

    def missing_thumbs(self):
        """Return captures with a still but no thumbnail, newest first.

        Newest first because those are what the library opens on, so a sweep
        cut short has already covered what anyone is likely to look at.
        """
        return [
            record
            for record in self._records
            if record["image"] and not record["thumb"]
        ]

    def latest_for(self, slug, kind=MOTION):
        """Return a camera's newest record of this kind, or None."""
        for record in self._records:
            if record["slug"] == slug and (kind is None or record["kind"] == kind):
                return record
        return None

    def query(
        self,
        camera=None,
        kind=None,
        subject=None,
        search=None,
        cursor=None,
        limit=60,
        until=None,
    ):
        """Page through the archive, newest first.

        `cursor` is the `next` value from a previous page; passing it back
        continues from exactly where that page stopped, so a capture arriving
        mid-scroll cannot shift the page boundaries. `until`, an aware
        datetime, skips everything captured after it - how the library jumps
        back to a given day without paging through everything since.
        """
        after = _decode_cursor(cursor)
        tags, needle = split_search(search)
        matches = []
        for record in self._records:
            if camera and record["slug"] != camera:
                continue
            if kind and record["kind"] != kind:
                continue
            if subject and subject not in (record["subjects"] or ()):
                continue
            if until and (parse_stamp(record["stamp"]) or _EPOCH) > until:
                continue
            if tags and not tags.issubset(record["tags"]):
                continue
            if needle and needle not in (record["summary"] or "").lower():
                if needle not in record["camera"].lower():
                    continue
            if after and _sort_key(record) >= after:
                continue
            matches.append(record)
            # One extra tells us whether another page exists without paying
            # for a second walk.
            if len(matches) > limit:
                break

        more = len(matches) > limit
        page = matches[:limit]
        return {
            "events": page,
            "next": _encode_cursor(page[-1]) if more and page else None,
        }

    def stats(self):
        """Return archive totals for the library header."""
        clips = sum(1 for record in self._records if record["clip"])
        stills = sum(1 for record in self._records if record["kind"] == STILL)
        per_camera = {}
        tags = {}
        for record in self._records:
            for tag in record["tags"]:
                tags[tag] = tags.get(tag, 0) + 1
            entry = per_camera.setdefault(
                record["slug"], {"slug": record["slug"], "camera": None, "count": 0}
            )
            entry["count"] += 1
            # The newest record for a camera carries its current name.
            if entry["camera"] is None:
                entry["camera"] = record["camera"]
        return {
            "total": len(self._records),
            "clips": clips,
            "stills": stills,
            "motion": len(self._records) - stills,
            "bytes": sum(record["bytes"] for record in self._records),
            "oldest": self._records[-1]["stamp"] if self._records else None,
            "newest": self._records[0]["stamp"] if self._records else None,
            "cameras": sorted(per_camera.values(), key=lambda e: e["camera"] or ""),
            # Most used first, for suggesting tags as someone types.
            "tags": dict(sorted(tags.items(), key=lambda item: (-item[1], item[0]))),
        }


def split_search(search):
    """Split a search into the #tags it names and the text that is left.

    "#dog #delivery front door" is every capture tagged both dog and delivery
    whose description or camera mentions "front door".
    """
    tags, words = set(), []
    for word in (search or "").lower().split():
        if word.startswith("#"):
            if word[1:]:
                tags.add(word[1:])
        else:
            words.append(word)
    return tags, " ".join(words)


def _sort_key(record):
    """Order by capture time, then id so equal stamps still page cleanly."""
    return (parse_stamp(record["stamp"]) or _EPOCH, record["id"])


def _encode_cursor(record):
    return f"{record['stamp']}|{record['id']}"


def _decode_cursor(cursor):
    if not cursor or "|" not in cursor:
        return None
    stamp, _, identifier = cursor.partition("|")
    parsed = parse_stamp(stamp)
    return (parsed, identifier) if parsed else None


def human_bytes(size):
    """Format a byte count the way the UI shows it."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
