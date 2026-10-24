"""What each camera has been called, so a rename is not a new camera.

Blink identifies a camera by a numeric id, but blinkpy - and so everything here
that follows it - keys cameras by name. Worse, the clips a sync module keeps on
its own card are labelled with the name the camera had when it recorded them,
and that label never changes. After a rename those clips belong to no camera at
all unless something remembers the name they were filed under.
"""

import asyncio
import json
import logging

from . import config

_LOGGER = logging.getLogger(__name__)


class Names:
    """Every name each camera id has gone by, kept on disk."""

    def __init__(self, path=None):
        """Start empty; call load() to read what was remembered."""
        self.path = path or config.CAMERA_NAMES
        # camera id -> {"name": current name, "former": [older names]}
        self._by_id = {}
        self._dirty = False

    async def load(self):
        """Read the file, tolerating one that is missing or damaged."""
        try:
            text = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as error:
            _LOGGER.warning("could not read %s: %s", self.path, error)
            return
        try:
            data = json.loads(text)
        except ValueError as error:
            _LOGGER.warning("ignoring unreadable %s: %s", self.path, error)
            return
        for camera_id, entry in (data or {}).items():
            if isinstance(entry, dict) and entry.get("name"):
                former = [n for n in entry.get("former") or [] if isinstance(n, str)]
                self._by_id[str(camera_id)] = {"name": entry["name"], "former": former}

    async def save(self):
        """Write the file if anything changed since it was last written."""
        if not self._dirty:
            return
        payload = json.dumps(self._by_id, indent=1, sort_keys=True)
        try:
            await asyncio.to_thread(self._write, payload)
        except OSError as error:
            _LOGGER.warning("could not write %s: %s", self.path, error)
            return
        self._dirty = False

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and moved into place: a half-written file would forget
        # every rename at once.
        spare = self.path.with_suffix(".tmp")
        spare.write_text(payload, encoding="utf-8")
        spare.replace(self.path)

    def observe(self, camera_id, name):
        """Record a camera's current name. Return its previous one if it changed."""
        if camera_id is None or not name:
            return None
        key = str(camera_id)
        entry = self._by_id.get(key)
        if entry is None:
            self._by_id[key] = {"name": name, "former": []}
            self._dirty = True
            return None
        if entry["name"] == name:
            return None
        old = entry["name"]
        # A name changed back is current again, not also a former one.
        entry["former"] = [n for n in entry["former"] if n not in (old, name)]
        entry["former"].append(old)
        entry["name"] = name
        self._dirty = True
        return old

    def id_for(self, name):
        """Return the camera id this name was last seen on, or None."""
        for camera_id, entry in self._by_id.items():
            if entry["name"] == name:
                return camera_id
        return None

    def supersede(self, camera_id, name):
        """Move a name onto a new camera id, keeping the names it went by.

        Deleting a camera in the Blink app and adding it back gives it a new
        id, so without this the old entry would keep the name and the clips a
        sync module still holds under a previous one would be orphaned.
        """
        key = str(camera_id)
        for old_id, entry in list(self._by_id.items()):
            if old_id == key or entry["name"] != name:
                continue
            carried = self._by_id.setdefault(key, {"name": name, "former": []})
            for former in entry["former"]:
                if former not in carried["former"] and former != name:
                    carried["former"].append(former)
            del self._by_id[old_id]
            self._dirty = True
            return old_id
        return None

    def adopt(self, renames):
        """Take renames from config that happened before anything was recorded.

        `renames` maps an old name to the new one. Returns True if it taught
        this anything new, and a rename naming no current camera is ignored.
        """
        learned = False
        for old, new in renames.items():
            for entry in self._by_id.values():
                if entry["name"] == new and old != new and old not in entry["former"]:
                    entry["former"].append(old)
                    self._dirty = learned = True
        return learned

    def former(self, name):
        """Return every earlier name of the camera now called `name`."""
        for entry in self._by_id.values():
            if entry["name"] == name:
                return list(entry["former"])
        return []
