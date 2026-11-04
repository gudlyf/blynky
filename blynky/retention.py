"""Delete archived captures once they are older than RETAIN_DAYS.

Nothing pruned the archive before this, and it grows at roughly a gigabyte a
fortnight. The rule here is deliberately the dull one: age, and nothing else.
A capture past the cut-off goes whatever is in it, so what is on disk can be
worked out from the calendar alone rather than from what a model once said.

The one exception is a starred capture, which is never deleted. A star is a
person saying "keep this", and nothing automatic outranks that: it is checked
when the sweep starts, and again against the sidecar on disk immediately
before each capture's files are touched.

Age comes from the record's stamp, never the file's mtime: the archive was
backfilled from filenames long after the events happened, so mtime would
delete almost exactly the wrong things.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from . import config, library

_LOGGER = logging.getLogger(__name__)

# Every file one capture can consist of. Listed rather than globbed on the
# prefix so that deletion can only ever touch names this app writes.
_SUFFIXES = (
    ".jpg",
    ".jpeg",
    "-frame.jpg",
    library.THUMB_SUFFIX,
    ".mp4",
    ".txt",
    ".json",
)


def _paths_for(record):
    """Return the files that make up one capture and are actually present."""
    base = library.dir_for(record["kind"], record["slug"])
    prefix = record["prefix"]
    found = []
    for suffix in _SUFFIXES:
        path = base / f"{prefix}{suffix}"
        if path.exists():
            found.append(path)
    return found


def _reason_to_keep(record):
    """Say why a capture past the cut-off must stay after all, or None.

    The sidecar is read fresh rather than trusted from memory: a star set
    while the sweep was running, or written by hand, must still save it. A
    sidecar that cannot be read is not evidence of a star, but nor is it
    evidence against one, so the capture is kept until it can be read.
    """
    if record.get("starred"):
        return "it is starred"
    path = library.dir_for(record["kind"], record["slug"]) / f"{record['prefix']}.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        return f"its sidecar could not be read to check for a star ({error})"
    if isinstance(stored, dict) and stored.get("starred") is True:
        # The disk was right and memory behind; catch memory up, so the
        # library shows the star and the next sweep need not read this again.
        record["starred"] = True
        return "it is starred"
    return None


def _delete(paths):
    """Unlink files, returning how many went and how many bytes they freed."""
    gone = freed = 0
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as error:
            _LOGGER.warning("could not delete %s: %s", path, error)
            continue
        gone += 1
        freed += size
    return gone, freed


async def sweep(index, now=None):
    """Delete captures past the cut-off. Returns a summary of what went.

    `latest/` is never touched: it holds the picture each card is currently
    showing, which is not archive, and clearing it would blank the grid.
    """
    if config.RETAIN_DAYS <= 0:
        return {"expired": 0, "deleted": 0, "freed": 0, "dry_run": False}

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.RETAIN_DAYS)
    old = [
        record
        for record in index.all()
        if (stamp := library.parse_stamp(record["stamp"])) and stamp < cutoff
    ]
    # A starred capture is one somebody asked to keep, whatever its age.
    expired = [record for record in old if not record.get("starred")]
    kept = len(old) - len(expired)
    if kept:
        _LOGGER.info(
            "retention: keeping %d starred capture(s) older than %d days",
            kept,
            config.RETAIN_DAYS,
        )
    if not expired:
        return {
            "expired": 0,
            "deleted": 0,
            "freed": 0,
            "kept": kept,
            "dry_run": config.RETAIN_DRY_RUN,
        }

    if config.RETAIN_DRY_RUN:
        for record in expired:
            _LOGGER.info(
                "retention (dry run): would delete %s %s",
                record["id"],
                record["stamp"],
            )
        _LOGGER.info(
            "retention (dry run): %d captures are older than %d days",
            len(expired),
            config.RETAIN_DAYS,
        )
        return {
            "expired": len(expired),
            "deleted": 0,
            "freed": 0,
            "kept": kept,
            "dry_run": True,
        }

    deleted = freed = removed = 0
    for record in expired:
        # Checked again for each one: deleting takes a while, and a star set
        # since the list was made has to win.
        reason = await asyncio.to_thread(_reason_to_keep, record)
        if reason:
            kept += 1
            _LOGGER.info("retention: kept %s, %s", record["id"], reason)
            continue
        paths = await asyncio.to_thread(_paths_for, record)
        gone, bytes_freed = await asyncio.to_thread(_delete, paths)
        # Drop it from the index even when a file could not be unlinked: the
        # media is gone as far as the UI is concerned either way, and leaving
        # it listed would offer a tile that plays nothing.
        index.remove(record)
        removed += 1
        deleted += gone
        freed += bytes_freed
        _LOGGER.info("retention: deleted %s (%s)", record["id"], record["stamp"])

    _LOGGER.info(
        "retention: removed %d captures older than %d days, freeing %s",
        removed,
        config.RETAIN_DAYS,
        library.human_bytes(freed),
    )
    return {
        "expired": removed,
        "deleted": deleted,
        "freed": freed,
        "kept": kept,
        "dry_run": False,
    }
