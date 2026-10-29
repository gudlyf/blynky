"""Runtime configuration, all overridable by environment variable."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Values already in the real environment win; .env only fills the gaps.
load_dotenv(ROOT / ".env")

CREDFILE = os.environ.get("CREDFILE", str(ROOT / ".blink_creds.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
EVENT_DIR = DATA_DIR / "events"
LATEST_DIR = DATA_DIR / "latest"
# Every still the refresh button produces is kept here. LATEST_DIR only
# ever holds the current picture, so without this the older ones are lost.
STILL_DIR = DATA_DIR / "stills"

# blinkpy's README warns that calls faster than 60s can overwhelm Blink's
# servers, so this is floored rather than merely defaulted.
POLL_SECONDS = max(60, int(os.environ.get("POLL_SECONDS", "60")))

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

# Provider for the vision model: "ollama" (default) or "gemini".
VISION_PROVIDER = os.environ.get("VISION_PROVIDER", "ollama").lower()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if VISION_PROVIDER == "gemini" and not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is required when VISION_PROVIDER is 'gemini'")

PROVIDER_PAUSE = float(os.environ.get("PROVIDER_PAUSE", "0"))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "minimax-m3:cloud")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = os.environ.get(
    "GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
GEMINI_PAUSE = (
    float(os.environ["GEMINI_PAUSE"]) if "GEMINI_PAUSE" in os.environ else None
)

# Active vision model name for reporting in UI and diagnostics.
VISION_MODEL = GEMINI_MODEL if VISION_PROVIDER == "gemini" else OLLAMA_MODEL

MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "4"))
# Reasoning models spend tokens thinking before they answer, so this needs
# headroom well beyond the length of the description itself.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1500"))
VISION_TIMEOUT = int(os.environ.get("VISION_TIMEOUT", "180"))

# Max retries on transient errors (429 Rate Limit, 503 High Demand).
VISION_MAX_RETRIES = int(os.environ.get("VISION_MAX_RETRIES", "4"))
VISION_RETRY_DELAY = float(os.environ.get("VISION_RETRY_DELAY", "5.0"))

# Describe events already on the server when the app starts.
CATCH_UP = os.environ.get("CATCH_UP") == "1"

# How many past events to keep in the in-memory feed. The archive on disk
# keeps everything regardless; this only bounds what a fresh page load gets.
HISTORY = int(os.environ.get("HISTORY", "50"))

# How many captures one page of the library view asks for at a time.
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "60"))

# How many motion events the timeline lists: the one under the camera grid,
# and the per-camera one in the detail view.
TIMELINE = int(os.environ.get("TIMELINE", "25"))

# How many times a saved clip may be sent to the model before the archive
# stops offering it. Without a cap, a clip the model can never handle would be
# retried on every restart for as long as it is kept.
DESCRIBE_ATTEMPTS = int(os.environ.get("DESCRIBE_ATTEMPTS", "3"))

# Describe saved clips that have no description at start-up. Unlike CATCH_UP
# this never touches Blink - the media is already on disk.
BACKFILL = os.environ.get("BACKFILL", "1") != "0"

# Seconds to wait between clips while working through that backlog. Live
# motion and the refresh button share the one model, so a long backlog has to
# leave gaps rather than hold the lock end to end.
BACKFILL_PAUSE = float(os.environ.get("BACKFILL_PAUSE", "2"))

# How many recent clips per camera the "Sync now" button pulls. Blink records
# a burst of clips when something moves, so this is a handful rather than one.
SYNC_LIMIT = int(os.environ.get("SYNC_LIMIT", "5"))

# How far back that button asks Blink's cloud feed for clips. The sync
# module's own storage is read whole, so this only bounds the cloud query.
SYNC_DAYS = int(os.environ.get("SYNC_DAYS", "2"))

# Delete archived captures older than this many days. 0 keeps everything,
# which is the default: an existing install must not start deleting footage
# because it was upgraded. Age is read from the capture's own timestamp.
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "0"))

# Log what would be deleted and delete nothing. Worth one run before trusting
# a new RETAIN_DAYS with a month of footage.
RETAIN_DRY_RUN = os.environ.get("RETAIN_DRY_RUN") == "1"

# What the model is allowed to report seeing. The vocabulary is closed on
# purpose: a filter or an alert rule can be written against these, which is
# not true of free prose. Anything outside the list belongs in the summary.
SUBJECTS = ("person", "animal", "vehicle", "package")

# Finer tags the model may add on top of SUBJECTS, closed for the same reason.
# Kept short and capped per clip: a tag that is on everything finds nothing.
# Each subject, and each recognised person's name, is a tag as well, and so
# are a few derived from what is already known: "group" for two or more
# people, "night" for dark or infrared footage, and "unknown" for a person
# nobody was recognised as.
DETAILS = ("dog", "cat", "deer", "bird", "bicycle", "delivery")
MAX_DETAILS = 3

# Reference frames for the people the model is asked to recognise.
PEOPLE_DIR = DATA_DIR / "people"

# Every name each camera has gone by, keyed by Blink's id for it. Clips on a
# sync module's card keep the name the camera had when they were recorded, so
# without this a renamed camera's clips belong to nobody.
CAMERA_NAMES = DATA_DIR / "cameras.json"

# Renames made before the app was recording names, as Old Name=New Name pairs
# separated by commas. Only needed once: renames seen while it runs, or between
# one run and the next, are picked up on their own.
CAMERA_RENAMES = dict(
    (old.strip(), new.strip())
    for old, sep, new in (
        pair.partition("=") for pair in os.environ.get("CAMERA_RENAMES", "").split(",")
    )
    if sep and old.strip() and new.strip()
)

# Guess who a person is, when any have been enrolled. No people enrolled means
# this never runs, so it costs nothing until it is used.
IDENTIFY = os.environ.get("IDENTIFY", "1") != "0"

# References kept per person. Spread them over several days rather than one
# event: testing showed the model matches build and posture across a change of
# clothes, but only when it has seen more than one day's worth.
MAX_REFERENCES = int(os.environ.get("MAX_REFERENCES", "5"))

# Total reference images sent in one identification. Every one costs prompt
# space on every person event, so the gallery is capped and filled round-robin
# rather than letting one well-photographed person crowd out the rest.
IDENTIFY_MAX_IMAGES = int(os.environ.get("IDENTIFY_MAX_IMAGES", "12"))

# A gallery plus the clip's frames is a much bigger request than a plain
# description, so it gets its own, longer, ceiling.
IDENTIFY_TIMEOUT = int(os.environ.get("IDENTIFY_TIMEOUT", "240"))

# The least confidence that counts as a match. Deliberately strict, because
# the two mistakes do not cost the same: failing to recognise someone just
# means the normal alert arrives, while a wrong match quietly downgrades the
# alert about a stranger - which is the one you wanted. Testing produced a
# medium-confidence match on evidence as thin as "lean build and dark hair".
IDENTIFY_MIN_CONFIDENCE = os.environ.get("IDENTIFY_MIN_CONFIDENCE", "high")

IDENTIFY_SYSTEM_PROMPT = (
    "You identify whether a person in security camera footage is someone "
    "already known. You are shown labeled reference images of known people, "
    "then frames from one new clip. These are low-resolution wide-angle "
    "frames and faces are rarely legible, so judge on build, posture, hair, "
    "gait and context. Clothing changes from day to day and is weak evidence "
    "on its own. Answering unknown is correct and expected whenever you are "
    "not confident, and is far better than a wrong name."
)

IDENTIFY_PROMPT = (
    "Reply with JSON and nothing else:\n"
    '{"who": [], "confidence": "low|medium|high", "why": "..."}\n'
    "who: the names of people from the references above that you recognise in "
    "the new frames. Use an empty list when you recognise nobody, including "
    "when the people present are strangers. Name everyone you do recognise; "
    "there may be more than one, or none.\n"
    "confidence: how sure you are.\n"
    "why: a few words on what you matched on."
)

# Where to push a notification when a camera sees something worth knowing.
# Empty means the feature is inert, which is the default: nothing leaves this
# machine until a topic is named.
NTFY_URL = os.environ.get("NTFY_URL", "").strip()

# Which subjects are worth a notification. People, by default: animals,
# vehicles and weather are most of what these cameras see, and alerting on
# them is how notifications end up muted.
NTFY_SUBJECTS = tuple(
    name.strip()
    for name in os.environ.get("NTFY_SUBJECTS", "person").split(",")
    if name.strip()
)

# ntfy's own scale: 1 min, 3 default, 5 max.
NTFY_PRIORITY = int(os.environ.get("NTFY_PRIORITY", "4"))
NTFY_TIMEOUT = int(os.environ.get("NTFY_TIMEOUT", "15"))

# Priority for someone the model recognised. The default keeps them but drops
# them below a stranger, which is the whole point of enrolling anyone.
NTFY_KNOWN_PRIORITY = int(os.environ.get("NTFY_KNOWN_PRIORITY", "2"))

# Say nothing at all when everyone seen is a known face.
NTFY_SKIP_KNOWN = os.environ.get("NTFY_SKIP_KNOWN") == "1"

# Seconds one camera must wait before it may notify again. A camera stuck
# facing a swaying branch would otherwise spend the daily quota by morning.
NTFY_COOLDOWN = int(os.environ.get("NTFY_COOLDOWN", "120"))

# Where a pause set from the settings menu is kept, so a restart does not
# quietly turn notifications back on before the time someone asked for.
NTFY_STATE = DATA_DIR / "ntfy.json"

# The longest pause the settings menu may ask for, in hours. "Until turned
# back on" is separate and has no limit.
NTFY_MAX_PAUSE_HOURS = 24 * 7

SYSTEM_PROMPT = (
    "You summarize home security camera footage. Be brief and factual. "
    "Lead with whatever triggered the motion - people, animals, vehicles, "
    "packages - and skip scenery, weather, and architectural detail unless "
    "it is what moved. State only what you can actually see; do not guess at "
    "intent or identity. The 'blink' watermark and any timestamp overlay are "
    "camera UI, not part of the scene: never mention them. No preamble, no "
    "restating the question, no closing commentary."
)

PROMPT = (
    "These frames are from one motion clip, in time order.\n"
    "Reply with JSON and nothing else, in exactly this shape:\n"
    '{"summary": "...", "subjects": [...], "tags": [...], "people_count": 0, '
    '"night": false}\n'
    "summary: in two sentences or fewer, what is happening. If nothing of "
    "note is present, say so plainly.\n"
    "subjects: which of " + ", ".join(SUBJECTS) + " you can actually see. "
    "Use an empty list when the scene is empty.\n"
    "tags: at most " + str(MAX_DETAILS) + " of " + ", ".join(DETAILS) + ", "
    "only those that clearly apply - delivery means a courier dropping off or "
    "collecting something. Most clips need none: use an empty list.\n"
    "people_count: how many distinct people are visible, 0 if none.\n"
    "night: true if the footage is dark or in black-and-white infrared night "
    "vision, false if it is daylight or lit in colour."
)
