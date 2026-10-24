"""Refile a renamed camera's archive under its new name.

Renaming a camera in the Blink app leaves everything already captured filed
under the old name: the folders under events/ and stills/, the id, name, slug
and media paths inside each sidecar, the picture in latest/, and the events a
known person's reference frames were taken from. This moves all of it across.

Stop the monitor first and start it again afterwards. A running monitor holds
the old paths in memory and would serve missing files until it restarts.

Nothing is changed without --apply; a plain run only reports what would be.
Running it again after an interrupted run carries on where that one stopped,
and a file already present under the new name is never overwritten.

    python scripts/rename_camera.py "Old Name" "New Name" --data ./data --apply

Clips still on a sync module's card keep the old name for good, and matching
those to the camera is the monitor's job, not this script's: see "Renaming a
camera" in the README.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from slugify import slugify
except ImportError:  # run on a host without the app's dependencies

    def slugify(text):
        """Match python-slugify's output for plain ASCII camera names."""
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# The folders a camera's captures live in, and the letter starting their ids.
KINDS = {"events": "m", "stills": "s"}

# What follows the timestamp in each file one capture is made of.
SUFFIXES = ("-frame.jpg", "-thumb.jpg", ".jpeg", ".jpg", ".mp4", ".txt", ".json")


def capture_of(filename):
    """Return the timestamp stem shared by every file of one capture."""
    for suffix in SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def load_json(path):
    """Read a JSON file, or return None if it cannot be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"  ! skipping unreadable {path}: {error}")
        return None


def write_json(path, data):
    """Write JSON the way the app does, replacing the file in one step."""
    spare = path.with_name(path.name + ".tmp")
    spare.write_text(json.dumps(data, indent=1), encoding="utf-8")
    spare.replace(path)


def swap_id(value, old_slug, new_slug):
    """Return an event id refiled under the new slug, or None if not the old one.

    Anchored on the timestamp that follows the slug, so an old slug that is a
    prefix of some other camera's ("front-door" and "front-door-2") cannot
    claim that camera's ids.
    """
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        rf"([ms])-{re.escape(old_slug)}-(\d{{4}}-\d{{2}}-\d{{2}}t.*)", value
    )
    return f"{match[1]}-{new_slug}-{match[2]}" if match else None


def refile_record(record, old, new, old_slug, new_slug):
    """Point one sidecar at the new name. Returns True if anything changed."""
    before = json.dumps(record, sort_keys=True)
    if record.get("camera") == old:
        record["camera"] = new
    if record.get("slug") == old_slug:
        record["slug"] = new_slug
    new_id = swap_id(record.get("id"), old_slug, new_slug)
    if new_id:
        record["id"] = new_id
    for key in ("image", "thumb", "clip"):
        value = record.get(key)
        for branch in KINDS:
            stale = f"/media/{branch}/{old_slug}/"
            if isinstance(value, str) and value.startswith(stale):
                record[key] = f"/media/{branch}/{new_slug}/" + value[len(stale) :]
    return json.dumps(record, sort_keys=True) != before


class Run:
    """One pass over the archive, either reporting or doing."""

    def __init__(self, data, old, new, apply):
        """Work out the slugs and start the tallies."""
        self.data, self.old, self.new, self.apply = data, old, new, apply
        self.old_slug, self.new_slug = slugify(old), slugify(new)
        self.moved = self.refiled = self.people = 0
        self.conflicts = []

    def act(self, do):
        """Carry out one change, or only say it would happen."""
        if self.apply:
            do()

    def move_folders(self):
        """Move every capture file from the old folder into the new one."""
        if self.old_slug == self.new_slug:
            return
        for branch in KINDS:
            source = self.data / branch / self.old_slug
            target = self.data / branch / self.new_slug
            if not source.is_dir():
                continue
            files = sorted(p for p in source.iterdir() if p.is_file())
            print(
                f"{branch}/{self.old_slug}/ -> {branch}/{self.new_slug}/: "
                f"{len(files)} file(s)"
            )
            # Never overwrite: two files with one name are two captures of one
            # moment, and which to keep is not ours to decide. The whole capture
            # stays put, or its clip would end up beside the other's sidecar.
            clashes = {
                capture_of(path.name) for path in files if (target / path.name).exists()
            }
            self.conflicts.extend(source / stem for stem in sorted(clashes))
            if self.apply:
                target.mkdir(parents=True, exist_ok=True)
            for path in files:
                destination = target / path.name
                if capture_of(path.name) in clashes:
                    continue
                self.act(lambda p=path, d=destination: os.replace(p, d))
                self.moved += 1
            if self.apply and not any(source.iterdir()):
                source.rmdir()

    def refile_sidecars(self):
        """Rewrite the name, id, slug and paths inside each moved sidecar."""
        for branch in KINDS:
            # In a report nothing has moved yet, so read them where they are.
            folders = {self.data / branch / self.new_slug}
            if not self.apply:
                folders.add(self.data / branch / self.old_slug)
            for folder in folders:
                if not folder.is_dir():
                    continue
                for path in sorted(folder.glob("*.json")):
                    record = load_json(path)
                    if not isinstance(record, dict):
                        continue
                    if refile_record(
                        record, self.old, self.new, self.old_slug, self.new_slug
                    ):
                        self.act(lambda p=path, r=record: write_json(p, r))
                        self.refiled += 1

    def move_latest(self):
        """Carry the picture the camera's card shows across, if there is none yet."""
        source = self.data / "latest" / f"{self.old_slug}.jpg"
        target = self.data / "latest" / f"{self.new_slug}.jpg"
        if self.old_slug == self.new_slug or not source.is_file():
            return
        if target.exists():
            # The monitor has already written a newer one under the new name.
            print(f"latest/{source.name}: left alone, {target.name} already exists")
            return
        print(f"latest/{source.name} -> latest/{target.name}")
        self.act(lambda: os.replace(source, target))

    def refile_people(self):
        """Point known people's reference frames at the events' new ids."""
        for path in sorted((self.data / "people").glob("*/person.json")):
            person = load_json(path)
            if not isinstance(person, dict):
                continue
            changed = 0
            for ref in person.get("references") or []:
                new_id = swap_id(ref.get("event"), self.old_slug, self.new_slug)
                if new_id:
                    ref["event"] = new_id
                    changed += 1
            if changed:
                print(f"people/{path.parent.name}: {changed} reference(s)")
                self.act(lambda p=path, d=person: write_json(p, d))
                self.people += changed

    def remember_name(self):
        """Record the old name in cameras.json so card clips still find the camera."""
        path = self.data / "cameras.json"
        names = load_json(path) if path.is_file() else None
        if isinstance(names, dict):
            for entry in names.values():
                if not isinstance(entry, dict) or entry.get("name") != self.new:
                    continue
                former = entry.setdefault("former", [])
                if self.old in former:
                    return
                former.append(self.old)
                print(f"cameras.json: {self.new!r} formerly {self.old!r}")
                self.act(lambda: write_json(path, names))
                return
        # Without an entry there is no camera id to attach the old name to.
        print(
            "cameras.json has no camera named "
            f"{self.new!r} yet. Clips still on the sync module's card are "
            "labelled with the old name, so set this in .env before starting:\n"
            f"  CAMERA_RENAMES={self.old}={self.new}"
        )

    def run(self):
        """Do every step in order, and say how it went."""
        print(f"{self.old!r} ({self.old_slug}) -> {self.new!r} ({self.new_slug})")
        print(
            "applying changes\n"
            if self.apply
            else "report only - nothing changes until --apply\n"
        )
        self.move_folders()
        self.refile_sidecars()
        self.move_latest()
        self.refile_people()
        self.remember_name()

        verb = "" if self.apply else "would be "
        print(
            f"\n{self.moved} file(s) {verb}moved, {self.refiled} sidecar(s) "
            f"{verb}rewritten, {self.people} people reference(s) {verb}updated"
        )
        if self.conflicts:
            print(
                f"\n{len(self.conflicts)} capture(s) clash with files already "
                "under the new name and were left in the old folder:"
            )
            for path in self.conflicts[:20]:
                print(f"  {path}")
            return 1
        return 0


def main():
    """Parse the arguments and run."""
    parser = argparse.ArgumentParser(
        description="Refile a renamed Blink camera's archive under its new name.",
        epilog="Stop the monitor first. Without --apply nothing is changed.",
    )
    parser.add_argument("old", help="the camera's old name, exactly as it was")
    parser.add_argument("new", help="the camera's new name, exactly as Blink has it")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(os.environ.get("DATA_DIR", "data")),
        help="the archive directory (default: $DATA_DIR, else ./data)",
    )
    parser.add_argument("--apply", action="store_true", help="make the changes")
    args = parser.parse_args()

    if args.old.strip() == args.new.strip():
        parser.error("the old and new names are the same")
    if not slugify(args.new):
        parser.error("the new name has no letters or digits to file it under")
    if not (args.data / "events").is_dir() and not (args.data / "stills").is_dir():
        parser.error(f"{args.data} has no events/ or stills/ - is it the archive?")

    try:
        return Run(args.data, args.old.strip(), args.new.strip(), args.apply).run()
    except OSError as error:
        # Most likely the files belong to the container's user. What has been
        # done so far is consistent, and running again picks up from here.
        print(f"\nstopped: {error}\nRe-run to continue once that is fixed.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
