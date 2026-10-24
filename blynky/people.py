"""Known people, and the reference frames used to recognise them.

This is not face recognition. These cameras are wide-angle and mounted high;
a person is a hundred-odd pixels tall and a face is barely twenty, so there is
nothing for an embedding to work with. What the model can do is judge build,
posture, hair and context against labeled examples, which is why a person here
is stored as a handful of frames out of the archive rather than as a vector.

Testing on this archive showed that works across a change of clothes - the
same man was identified in three different shirts from one afternoon's
references - so references are worth spreading over several days rather than
piling up from one event.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from slugify import slugify

from . import config

_LOGGER = logging.getLogger(__name__)

_MANIFEST = "person.json"


def dir_for(slug):
    """Return the directory holding one person's references."""
    return config.PEOPLE_DIR / slug


def media_url(slug, name):
    """Return the browser-facing URL for one reference frame."""
    return f"/media/people/{slug}/{name}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class People:
    """The enrolled people, and their reference frames."""

    def __init__(self):
        """Start empty; call load() before serving."""
        self._people = {}

    async def load(self):
        """Read every person off disk."""
        self._people = await asyncio.to_thread(self._scan)
        if self._people:
            _LOGGER.info("known people: %s", ", ".join(sorted(self._people)))
        return self._people

    def _scan(self):
        found = {}
        root = config.PEOPLE_DIR
        if not root.is_dir():
            return found
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / _MANIFEST
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                _LOGGER.warning("ignoring unreadable %s: %s", path, error)
                continue
            # References are re-derived from disk, so a manifest that outlived
            # its images cannot offer one that is no longer there.
            kept = [
                ref
                for ref in stored.get("references") or []
                if (entry / str(ref.get("file"))).is_file()
            ]
            stored["references"] = kept
            stored["slug"] = entry.name
            stored.setdefault("name", entry.name)
            stored.setdefault("notes", "")
            found[entry.name] = stored
        return found

    def all(self):
        """Return every person, with reference URLs the browser can load."""
        out = []
        for slug, person in sorted(self._people.items(), key=lambda kv: kv[1]["name"]):
            out.append(
                {
                    "slug": slug,
                    "name": person["name"],
                    "notes": person.get("notes") or "",
                    "references": [
                        {
                            "file": ref["file"],
                            "url": media_url(slug, ref["file"]),
                            "event": ref.get("event"),
                            "added": ref.get("added"),
                        }
                        for ref in person["references"]
                    ],
                }
            )
        return out

    def names(self):
        """Return the enrolled names, for prompting and for the UI."""
        return [person["name"] for person in self._people.values()]

    def gallery(self):
        """Return (name, image bytes) pairs to show the model.

        Capped, and taken round-robin so that every person is represented even
        when one of them has many more references than the others - each image
        costs prompt space on every identification.
        """
        queues = []
        for slug, person in sorted(self._people.items()):
            refs = person["references"][-config.MAX_REFERENCES :]
            if refs:
                queues.append((person["name"], slug, list(refs)))

        pairs = []
        while queues and len(pairs) < config.IDENTIFY_MAX_IMAGES:
            for name, slug, refs in list(queues):
                if not refs:
                    queues.remove((name, slug, refs))
                    continue
                ref = refs.pop(0)
                try:
                    payload = (dir_for(slug) / ref["file"]).read_bytes()
                except OSError as error:
                    _LOGGER.warning("reference %s unreadable: %s", ref["file"], error)
                    continue
                pairs.append((name, payload))
                if len(pairs) >= config.IDENTIFY_MAX_IMAGES:
                    break
        return pairs

    def resolve(self, names):
        """Map names the model returned onto enrolled ones, dropping the rest.

        The model is asked for names it was given, but a paraphrase or an
        invented one must never reach the UI as though somebody was there.
        """
        known = {
            person["name"].lower(): person["name"] for person in self._people.values()
        }
        out = []
        for raw in names or []:
            match = known.get(str(raw).strip().lower())
            if match and match not in out:
                out.append(match)
        return out

    async def add(self, name, notes=""):
        """Create a person, or return the existing one of that name."""
        slug = slugify(name)
        if not slug:
            raise ValueError("a person needs a name")
        if slug not in self._people:
            self._people[slug] = {
                "slug": slug,
                "name": name.strip(),
                "notes": notes,
                "references": [],
            }
            await self._save(slug)
        return self._people[slug]

    async def add_reference(self, slug, image, event_id=None):
        """Keep one frame as an example of this person."""
        person = self._people.get(slug)
        if person is None:
            raise KeyError(slug)

        # Numbered rather than named after the event so that the same event
        # can be used twice and a file can never collide.
        used = {ref["file"] for ref in person["references"]}
        index = 1
        while f"ref-{index:04d}.jpg" in used:
            index += 1
        filename = f"ref-{index:04d}.jpg"

        await asyncio.to_thread(self._write_reference, slug, filename, image)
        person["references"].append(
            {"file": filename, "event": event_id, "added": _now()}
        )
        await self._save(slug)
        return {"file": filename, "url": media_url(slug, filename)}

    def _write_reference(self, slug, filename, image):
        path = dir_for(slug)
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_bytes(image)

    async def remove_reference(self, slug, filename):
        """Drop one reference frame."""
        person = self._people.get(slug)
        if person is None:
            raise KeyError(slug)
        person["references"] = [
            ref for ref in person["references"] if ref["file"] != filename
        ]
        await asyncio.to_thread(self._unlink, slug, filename)
        await self._save(slug)

    def _unlink(self, slug, filename):
        path = dir_for(slug) / filename
        try:
            path.unlink()
        except OSError as error:
            _LOGGER.warning("could not delete %s: %s", path, error)

    async def remove(self, slug):
        """Forget a person entirely, references and all."""
        person = self._people.pop(slug, None)
        if person is None:
            return False
        await asyncio.to_thread(self._remove_dir, slug)
        return True

    def _remove_dir(self, slug):
        path = dir_for(slug)
        for child in sorted(path.glob("*")):
            try:
                child.unlink()
            except OSError as error:
                _LOGGER.warning("could not delete %s: %s", child, error)
        try:
            path.rmdir()
        except OSError as error:
            _LOGGER.warning("could not remove %s: %s", path, error)

    async def _save(self, slug):
        person = self._people[slug]
        payload = json.dumps(person, indent=1)
        await asyncio.to_thread(self._write_manifest, slug, payload)

    def _write_manifest(self, slug, payload):
        path = dir_for(slug)
        path.mkdir(parents=True, exist_ok=True)
        (path / _MANIFEST).write_text(payload, encoding="utf-8")
