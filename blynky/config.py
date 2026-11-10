"""Runtime configuration, all overridable by environment variable."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Values already in the real environment win; .env only fills the gaps.
load_dotenv(ROOT / ".env")


def _number(name, default, kind, noun):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        # One line naming the setting, not a traceback from deep in import.
        raise SystemExit(f"{name} must be {noun}, not {raw!r}.") from None


def _int(name, default):
    return _number(name, default, int, "a whole number")


def _float(name, default):
    return _number(name, default, float, "a number")


CREDFILE = os.environ.get("CREDFILE", str(ROOT / ".blink_creds.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
EVENT_DIR = DATA_DIR / "events"
LATEST_DIR = DATA_DIR / "latest"
# Every still the refresh button produces is kept here. LATEST_DIR only
# ever holds the current picture, so without this the older ones are lost.
STILL_DIR = DATA_DIR / "stills"

# blinkpy's README warns that calls faster than 60s can overwhelm Blink's
# servers, so this is floored rather than merely defaulted.
POLL_SECONDS = max(60, _int("POLL_SECONDS", 60))

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _int("PORT", 8080)

# Provider for the vision model: "ollama" (default) or "gemini".
VISION_PROVIDER = os.environ.get("VISION_PROVIDER", "ollama").lower()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if VISION_PROVIDER == "gemini" and not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY is required when VISION_PROVIDER is gemini.")

PROVIDER_PAUSE = _float("PROVIDER_PAUSE", 0)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "minimax-m3:cloud")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = os.environ.get(
    "GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
GEMINI_PAUSE = _float("GEMINI_PAUSE", None)

# Active vision model name for reporting in UI and diagnostics.
VISION_MODEL = GEMINI_MODEL if VISION_PROVIDER == "gemini" else OLLAMA_MODEL

MAX_FRAMES = _int("MAX_FRAMES", 4)
# Reasoning models spend tokens thinking before they answer, so this needs
# headroom well beyond the length of the description itself.
MAX_TOKENS = _int("MAX_TOKENS", 1500)
VISION_TIMEOUT = _int("VISION_TIMEOUT", 180)

# Max retries on transient errors (429 Rate Limit, 503 High Demand).
VISION_MAX_RETRIES = _int("VISION_MAX_RETRIES", 4)
VISION_RETRY_DELAY = _float("VISION_RETRY_DELAY", 5.0)

# Describe events already on the server when the app starts.
CATCH_UP = os.environ.get("CATCH_UP") == "1"

# How many past events to keep in the in-memory feed. The archive on disk
# keeps everything regardless; this only bounds what a fresh page load gets.
HISTORY = _int("HISTORY", 50)

# How many captures one page of the library view asks for at a time.
PAGE_SIZE = _int("PAGE_SIZE", 60)

# How many motion events the timeline lists: the one under the camera grid,
# and the per-camera one in the detail view.
TIMELINE = _int("TIMELINE", 25)

# How many times a saved clip may be sent to the model before the archive
# stops offering it. Without a cap, a clip the model can never handle would be
# retried on every restart for as long as it is kept.
DESCRIBE_ATTEMPTS = _int("DESCRIBE_ATTEMPTS", 3)

# Describe saved clips that have no description at start-up. Unlike CATCH_UP
# this never touches Blink - the media is already on disk.
BACKFILL = os.environ.get("BACKFILL", "1") != "0"

# Seconds to wait between clips while working through that backlog. Live
# motion and the refresh button share the one model, so a long backlog has to
# leave gaps rather than hold the lock end to end.
BACKFILL_PAUSE = _float("BACKFILL_PAUSE", 2)

# How many recent clips per camera the "Sync now" button pulls. Blink records
# a burst of clips when something moves, so this is a handful rather than one.
SYNC_LIMIT = _int("SYNC_LIMIT", 5)

# How far back that button asks Blink's cloud feed for clips. The sync
# module's own storage is read whole, so this only bounds the cloud query.
SYNC_DAYS = _int("SYNC_DAYS", 2)

# Delete archived captures older than this many days. 0 keeps everything,
# which is the default: an existing install must not start deleting footage
# because it was upgraded. Age is read from the capture's own timestamp.
RETAIN_DAYS = _int("RETAIN_DAYS", 0)

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

# Call each camera "System:Camera" - "Cabin:Driveway", "Home:Driveway" -
# rather than by its name alone. Blink only keeps names unique within one
# system, so an account with two systems can have two cameras of the same name,
# and without this only one of them is seen. Off by default. Turning it on is
# a rename of every camera, handled as any rename is: see the guide.
SYSTEM_PREFIX = os.environ.get("SYSTEM_PREFIX") == "1"

# Guess who a person is, when any have been enrolled. No people enrolled means
# this never runs, so it costs nothing until it is used.
IDENTIFY = os.environ.get("IDENTIFY", "1") != "0"

# References kept per person. Spread them over several days rather than one
# event: testing showed the model matches build and posture across a change of
# clothes, but only when it has seen more than one day's worth.
MAX_REFERENCES = _int("MAX_REFERENCES", 5)

# Total reference images sent in one identification. Every one costs prompt
# space on every person event, so the gallery is capped and filled round-robin
# rather than letting one well-photographed person crowd out the rest.
IDENTIFY_MAX_IMAGES = _int("IDENTIFY_MAX_IMAGES", 12)

# A gallery plus the clip's frames is a much bigger request than a plain
# description, so it gets its own, longer, ceiling.
IDENTIFY_TIMEOUT = _int("IDENTIFY_TIMEOUT", 240)

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

# Where this server is reached from a phone, such as https://blynky.home.lan
# or http://192.168.1.20:8080. Set, tapping a notification opens the capture it
# is about. Without AUTH_USER Blynky has no login, so this should be an address
# only you can reach - a LAN, a VPN or a tailnet - and never a port open to the
# internet. It also travels inside every notification, so anyone subscribed to
# the topic sees it.
BLYNKY_URL = os.environ.get("BLYNKY_URL", "").strip().rstrip("/")
if BLYNKY_URL and not BLYNKY_URL.startswith(("http://", "https://")):
    raise SystemExit("BLYNKY_URL must start with http:// or https://.")

# Names, beside BLYNKY_URL's, that this server answers to, comma separated:
# nas.local, blynky.home.lan. An IP address and localhost always work. Any
# other name is refused, which is what stops a web page from reaching this
# server through a DNS name it controls (DNS rebinding). * turns the check off.
ALLOWED_HOSTS = {
    part.strip().lower().rstrip(".")
    for part in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if part.strip()
}

# An optional login in front of the whole UI: one username and password, set
# here, nothing to do with the Blink account. Off unless both are set, so an
# existing install behaves as before. It keeps a stranger who reaches the
# port from the cameras; it does not make plain HTTP private, so the advice
# to stay behind tailscale serve or another HTTPS proxy stands.
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
if bool(AUTH_USER) != bool(AUTH_PASSWORD):
    raise SystemExit("Set both AUTH_USER and AUTH_PASSWORD, or neither.")
if AUTH_PASSWORD and len(AUTH_PASSWORD) < 8:
    raise SystemExit("AUTH_PASSWORD must be at least 8 characters.")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)

# How long one sign-in lasts, in days. Long on purpose: a phone signs in once.
# Each use in the second half of that time starts it over, so a device used
# now and then never has to sign in again.
AUTH_SESSION_DAYS = max(1, _int("AUTH_SESSION_DAYS", 365))

# The key sessions are signed with, made on first use. Deleting it signs out
# every device; so does changing AUTH_USER or AUTH_PASSWORD.
SESSION_SECRET = DATA_DIR / ".session_secret"

# Send each system's notifications to a topic of its own: <system>-<topic>
# beside NTFY_URL's, such as https://ntfy.sh/cabin-mytopic, so a phone can
# subscribe to one place and not another. ntfy allows only letters, digits,
# - and _ in a topic name, so the system's name is slugified. Anything not tied
# to one system, such as a summary of every camera, still goes to NTFY_URL.
# Follows SYSTEM_PREFIX unless set.
NTFY_SYSTEM_TOPICS = (
    os.environ.get("NTFY_SYSTEM_TOPICS", "1" if SYSTEM_PREFIX else "0") == "1"
)

# Which subjects are worth a notification. People, by default: animals,
# vehicles and weather are most of what these cameras see, and alerting on
# them is how notifications end up muted.
NTFY_SUBJECTS = tuple(
    name.strip()
    for name in os.environ.get("NTFY_SUBJECTS", "person").split(",")
    if name.strip()
)

# ntfy's own scale: 1 min, 3 default, 5 max.
NTFY_PRIORITY = _int("NTFY_PRIORITY", 4)
NTFY_TIMEOUT = _int("NTFY_TIMEOUT", 15)

# Priority for someone the model recognised. The default keeps them but drops
# them below a stranger, which is the whole point of enrolling anyone.
NTFY_KNOWN_PRIORITY = _int("NTFY_KNOWN_PRIORITY", 2)

# Say nothing at all when everyone seen is a known face.
NTFY_SKIP_KNOWN = os.environ.get("NTFY_SKIP_KNOWN") == "1"

# Seconds one camera must wait before it may notify again. A camera stuck
# facing a swaying branch would otherwise spend the daily quota by morning.
NTFY_COOLDOWN = _int("NTFY_COOLDOWN", 120)

# Where a pause set from the settings menu is kept, so a restart does not
# quietly turn notifications back on before the time someone asked for.
NTFY_STATE = DATA_DIR / "ntfy.json"

# The longest pause the settings menu may ask for, in hours. "Until turned
# back on" is separate and has no limit.
NTFY_MAX_PAUSE_HOURS = 24 * 7

# One short account of each camera's day, written by the model from that day's
# descriptions. Off unless switched on, from the settings menu or here: it is
# one more model call per camera per day, and a backfill is one per day held.
# A day runs midnight to midnight in the server's time zone, so set TZ (for
# example TZ=America/New_York) when the server does not run on local time -
# a container runs on UTC unless told otherwise.
DAILY_SUMMARIES = os.environ.get("DAILY_SUMMARIES") == "1"
DAILY_DIR = DATA_DIR / "daily"
# The switch in the settings menu, kept so a restart remembers it. Once it has
# been used it wins over DAILY_SUMMARIES.
DAILY_STATE = DATA_DIR / "daily.json"

# What gets a summary: "camera" (each camera), "location" (each Blink system,
# all its cameras together) and "all" (every camera together), comma separated,
# in any combination. The settings menu has these too, and wins once used.
DAILY_SCOPES = [
    scope
    for scope in (
        part.strip().lower()
        for part in os.environ.get("DAILY_SCOPES", "camera").split(",")
    )
    if scope in ("camera", "location", "all")
] or ["camera"]

# Push each camera's summary of yesterday to ntfy as it is written. The
# settings menu has a switch for this too, which wins once used. Only the
# summary written unasked each night is sent - never a backfill, which would
# be a notification for every day in the archive.
NTFY_DAILY = os.environ.get("NTFY_DAILY") == "1"
# Low by default: it lands in the small hours, and is reading, not an alert.
NTFY_DAILY_PRIORITY = _int("NTFY_DAILY_PRIORITY", 2)

# Hour of the day, local time, after which yesterday is summarized. An hour's
# grace lets the last clips before midnight finish being described first.
DAILY_HOUR = _int("DAILY_HOUR", 1)

# The most clip descriptions one summary is written from. A camera facing a
# road can log hundreds a day, and past a point more lines add tokens, not
# insight; a day over the limit is sampled evenly across its hours.
DAILY_MAX_CLIPS = _int("DAILY_MAX_CLIPS", 300)

# Seconds between days while a backfill works through them, for the same
# reason as BACKFILL_PAUSE: live motion shares the model.
DAILY_PAUSE = _float("DAILY_PAUSE", 2)

# A digest, not a log: the clips themselves are a click away in the library,
# so this should read as the gist of the day rather than a list of times.
DAILY_SYSTEM_PROMPT = (
    "You write a short daily digest for a home security system - one camera, "
    "or several cameras together - from the descriptions of each motion clip "
    "recorded that day. Give the general picture of the day - what kinds of "
    "activity there were and roughly when, in broad terms like morning, "
    "afternoon or evening - not a timeline of individual clips. Do not give "
    "exact times, and do not list every event: fold routine, repeated "
    "activity such as passing cars, wildlife or nothing of note into a "
    "phrase, and mention people, deliveries or anything unusual only in "
    "general terms. With several cameras, describe the day as a whole and "
    "name a camera only where it matters. Use only what the descriptions "
    "say; do not guess at intent or identity. Plain prose: no preamble, "
    "headings, lists or closing commentary."
)

DAILY_PROMPT = (
    "Summarize this camera's day in two or three sentences. Reply with the "
    "summary and nothing else."
)

DAILY_GROUP_PROMPT = (
    "Summarize the day across these cameras in two or three sentences. Reply "
    "with the summary and nothing else."
)

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
