"""Media helpers: writing files and sampling frames out of a clip."""

import asyncio
import logging
import shutil
from pathlib import Path

import aiofiles

from . import config

_LOGGER = logging.getLogger(__name__)


async def write_bytes(path, payload):
    """Write bytes to path, creating parent directories as needed."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "wb") as handle:
        await handle.write(payload)


async def clip_duration(clip_path):
    """Return clip length in seconds, or None if it cannot be determined."""
    if not shutil.which("ffprobe"):
        return None
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


# The size of a thumbnail: a 16:9 tile, which is the shape every place that
# shows one draws it (library tiles and timeline rows, both object-fit: cover).
# 480 wide is a 240px tile on a 2x screen. Cropping here rather than in the
# browser also drops the half of a square doorbell frame nobody would see.
THUMB_SIZE = (480, 270)


async def thumbnail(source):
    """Return JPEG bytes of a small, cropped copy of a still, or None."""
    if not shutil.which("ffmpeg"):
        return None
    width, height = THUMB_SIZE
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}",
        "-frames:v",
        "1",
        "-q:v",
        "5",
        # Out through a pipe, so the caller decides where it lands and a
        # half-written file is never left where the browser could fetch it.
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0 or not stdout:
        _LOGGER.warning("thumbnail of %s failed: %s", source, stderr.decode()[:200])
        return None
    return stdout


async def extract_frames(clip_path, workdir):
    """Sample up to MAX_FRAMES stills spread across the clip."""
    if not shutil.which("ffmpeg"):
        _LOGGER.warning("ffmpeg not found - falling back to the snapshot")
        return []

    duration = await clip_duration(clip_path)
    if duration and duration > 0:
        # Spread the samples over the whole clip rather than the opening
        # seconds, which is what a fixed sampling rate would give us.
        video_filter = f"fps={config.MAX_FRAMES / duration},scale=768:-2"
    else:
        video_filter = "thumbnail,scale=768:-2"

    pattern = str(Path(workdir) / "frame_%02d.jpg")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        str(clip_path),
        "-vf",
        video_filter,
        "-frames:v",
        str(config.MAX_FRAMES),
        pattern,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if process.returncode != 0:
        _LOGGER.warning("ffmpeg failed: %s", stderr.decode()[:200])
        return []
    return sorted(Path(workdir).glob("frame_*.jpg"))
