"""Vision model client, speaking OpenAI-compatible APIs (Ollama, Gemini, etc.)."""

import asyncio
import base64
import json
import logging
import random
import re
import time

from aiohttp import ClientTimeout

from . import config

_LOGGER = logging.getLogger(__name__)

# Active vision provider endpoint, headers, model and pause, with Gemini overrides.
_ENDPOINT = f"{config.OLLAMA_URL.rstrip('/')}/chat/completions"
_HEADERS: dict[str, str] = {}
_MODEL = config.OLLAMA_MODEL
_PAUSE = config.PROVIDER_PAUSE

if config.VISION_PROVIDER == "gemini":
    assert config.GEMINI_API_KEY, (
        "GEMINI_API_KEY is required when VISION_PROVIDER is 'gemini'"
    )
    _ENDPOINT = f"{config.GEMINI_URL.rstrip('/')}/chat/completions"
    _MODEL = config.GEMINI_MODEL
    _HEADERS["Authorization"] = f"Bearer {config.GEMINI_API_KEY}"
    if config.GEMINI_PAUSE is not None:
        _PAUSE = config.GEMINI_PAUSE

# Track the timestamp of the last request to pace calls across the process.
_last_request_time = 0.0
_pacer_lock = asyncio.Lock()


async def _pace():
    """Ensure at least _PAUSE seconds elapsed since last call."""
    if _PAUSE <= 0:
        return
    global _last_request_time
    async with _pacer_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _PAUSE:
            await asyncio.sleep(_PAUSE - elapsed)
        _last_request_time = time.monotonic()


def _retry_delay(response_headers, attempt):
    """Calculate wait seconds for a retry, checking Retry-After first."""
    retry_after = response_headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    base = config.VISION_RETRY_DELAY * (2**attempt)
    # Add random jitter between 0 and 1 second to desynchronize calls
    jitter = random.uniform(0.0, 1.0)
    return base + jitter


async def _post_chat(session, endpoint, headers, body, timeout):
    """Post chat request with rate-limiting pacing and retry on 429/503."""
    max_retries = config.VISION_MAX_RETRIES

    for attempt in range(max_retries + 1):
        await _pace()
        try:
            response = await session.post(
                endpoint,
                headers=headers,
                json=body,
                timeout=ClientTimeout(total=timeout),
            )
            text = await response.text()
            if response.status == 200:
                return response, text

            # Transient rate-limit (429) or high demand / unavailable (503)
            if response.status in (429, 503) and attempt < max_retries:
                delay = _retry_delay(response.headers, attempt)
                _LOGGER.warning(
                    "vision API returned %s (attempt %d/%d), retrying in %.1fs: %s",
                    response.status,
                    attempt + 1,
                    max_retries,
                    delay,
                    text[:200],
                )
                await asyncio.sleep(delay)
                continue

            _LOGGER.error("vision model returned %s: %s", response.status, text[:300])
            return response, text
        except (TimeoutError, asyncio.TimeoutError):
            if attempt < max_retries:
                delay = _retry_delay({}, attempt)
                _LOGGER.warning(
                    "vision request timed out (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            _LOGGER.error("vision model timed out after %ss", timeout)
            return None, None
        except Exception as error:  # noqa: BLE001
            _LOGGER.error("vision request failed: %s", error)
            return None, None
    return None, None


# A model that ignores "JSON and nothing else" usually still wraps the JSON in
# a markdown fence, or pads it with a sentence either side. Both are recovered
# rather than thrown away.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Models paraphrase a closed vocabulary. Asked for "animal" one will answer
# "dog", and dropping that would file a dog as an empty scene - the opposite
# of what it saw. Anything still unrecognised after this is dropped, which is
# why the summary remains the record of what actually happened.
_SYNONYMS = {
    "person": "person",
    "people": "person",
    "human": "person",
    "man": "person",
    "men": "person",
    "woman": "person",
    "women": "person",
    "child": "person",
    "children": "person",
    "kid": "person",
    "animal": "animal",
    "dog": "animal",
    "cat": "animal",
    "deer": "animal",
    "bird": "animal",
    "squirrel": "animal",
    "wildlife": "animal",
    "pet": "animal",
    "vehicle": "vehicle",
    "car": "vehicle",
    "truck": "vehicle",
    "van": "vehicle",
    "suv": "vehicle",
    "motorcycle": "vehicle",
    "bicycle": "vehicle",
    "package": "package",
    "parcel": "package",
    "box": "package",
    "delivery": "package",
}


def _singulars(word):
    """Yield a word and its plausible singulars, longest form first."""
    yield word
    # rstrip() would take "boxes" down to "boxe"; it strips a character set,
    # not a suffix.
    if word.endswith("es"):
        yield word[:-2]
    if word.endswith("s"):
        yield word[:-1]


def _normalise(name):
    """Map one reported subject onto the closed vocabulary, or None."""
    word = str(name).strip().lower()
    # "delivery person", "parked car": the last word is the head noun, and
    # what the model is actually reporting.
    for part in [word, *reversed(word.split())]:
        for candidate in _singulars(part):
            if candidate in _SYNONYMS:
                return _SYNONYMS[candidate]
    return None


def _is_empty_summary(text):
    """Whether a description indicates nothing of note was detected."""
    normalized = str(text).strip().lower()
    return (
        normalized.startswith("nothing of note")
        or "nothing of note is present" in normalized
    )


def _coerce(payload):
    """Turn a parsed JSON reply into a result, or None if it is not one."""
    if not isinstance(payload, dict):
        return None
    summary = (payload.get("summary") or "").strip()
    if not summary or _is_empty_summary(summary):
        return None

    subjects = payload.get("subjects")
    if isinstance(subjects, str):
        subjects = [subjects]
    if isinstance(subjects, list):
        # Keep the vocabulary closed: a filter should never have to know about
        # a word the model invented. Order comes from SUBJECTS so two events
        # seeing the same things always compare equal.
        seen = {_normalise(item) for item in subjects}
        subjects = [name for name in config.SUBJECTS if name in seen]
    else:
        subjects = None

    count = payload.get("people_count")
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        count = None
    else:
        count = max(0, int(count))
    # The two answers have to agree: a count with no "person" in subjects, or
    # the reverse, is the model contradicting itself, and subjects is the one
    # everything downstream reads.
    if subjects is not None and count is not None:
        if count > 0 and "person" not in subjects:
            subjects = [*subjects, "person"]
        elif count == 0 and "person" in subjects:
            count = None

    return {"summary": summary, "subjects": subjects, "people_count": count}


def parse_reply(text):
    """Read a model reply, however it chose to answer. Returns a result dict.

    A model that honours the schema gives structured subjects. One that
    ignores it and writes a plain sentence still yields a usable summary, with
    subjects left unset to mean "not known" rather than "nothing was there" -
    the archive can infer those from the text later.
    """
    text = (text or "").strip()
    if not text or _is_empty_summary(text):
        return None

    candidates = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    saw_empty_summary = False
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and _is_empty_summary(parsed.get("summary") or ""):
            saw_empty_summary = True
            continue
        result = _coerce(parsed)
        if result:
            return result

    if saw_empty_summary:
        return None

    # Prose, or JSON that carried no summary. Either way the sentence itself
    # is still the description, which is exactly what this used to return.
    _LOGGER.debug("vision reply was not the requested JSON; using it as prose")
    return {"summary": text, "subjects": None, "people_count": None}


def _encode(payload):
    """Wrap one JPEG as the data URL the chat API takes."""
    encoded = base64.b64encode(payload).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }


def parse_identity(text):
    """Read an identification reply into names and a confidence."""
    text = (text or "").strip()
    if not text:
        return None
    candidates = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        who = payload.get("who")
        if isinstance(who, str):
            who = [] if who.strip().lower() in ("", "unknown", "none") else [who]
        if not isinstance(who, list):
            who = []
        confidence = str(payload.get("confidence") or "").strip().lower()
        if confidence not in ("low", "medium", "high"):
            confidence = None
        return {
            "who": [str(name).strip() for name in who if str(name).strip()],
            "confidence": confidence,
            "why": str(payload.get("why") or "").strip()[:200],
        }
    # No JSON at all. Naming somebody on the strength of a sentence we could
    # not parse is exactly the false positive this must never produce.
    _LOGGER.warning("identification reply was not JSON; treating as unknown")
    return None


async def identify(session, images, gallery):
    """Ask which of the known people, if any, is in these frames.

    `gallery` is (name, jpeg bytes) pairs. Returns `{"who", "confidence",
    "why"}` with an empty `who` for nobody recognised, or None if the model
    could not be reached. The frames are the ones sampled from the clip, not
    the single saved still: the still is the last frame of the clip and often
    no longer has the person in it - a probe that answered "no person is
    visible" from the still identified them with high confidence from four.
    """
    if not images or not gallery:
        return None

    content = []
    for name, payload in gallery:
        content.append({"type": "text", "text": f"Reference - this is {name}:"})
        content.append(_encode(payload))
    heading = f"Now {len(images)} frames from one new clip, in order:"
    content.append({"type": "text", "text": heading})
    content.extend(_encode(payload) for payload in images)
    content.append({"type": "text", "text": config.IDENTIFY_PROMPT})

    body = {
        "model": _MODEL,
        "max_tokens": config.MAX_TOKENS,
        "messages": [
            {"role": "system", "content": config.IDENTIFY_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    }

    response, text = await _post_chat(
        session, _ENDPOINT, _HEADERS, body, timeout=config.IDENTIFY_TIMEOUT
    )
    if response is None or response.status != 200 or not text:
        return None

    try:
        choice = json.loads(text)["choices"][0]
        return parse_identity(choice["message"].get("content"))
    except (KeyError, IndexError, ValueError) as error:
        _LOGGER.error("could not parse identify response: %s", error)
    except Exception as error:  # noqa: BLE001 - never kill the poll loop
        _LOGGER.error("identify request failed: %s", error)
    return None


async def describe(session, images):
    """Ask the vision model what is in the frames.

    Returns `{"summary", "subjects", "people_count"}`, or None if the model
    could not be reached or gave nothing back at all. `subjects` is None when
    the model did not report any, which is not the same as reporting none.
    """
    if not images:
        return None

    content = [{"type": "text", "text": config.PROMPT}]
    content.extend(_encode(payload) for payload in images)

    body = {
        "model": _MODEL,
        "max_tokens": config.MAX_TOKENS,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    }

    response, text = await _post_chat(
        session, _ENDPOINT, _HEADERS, body, timeout=config.VISION_TIMEOUT
    )
    if response is None or response.status != 200 or not text:
        return None

    try:
        choice = json.loads(text)["choices"][0]
        raw_content = choice["message"].get("content")
        reply = parse_reply(raw_content)
        if reply:
            return reply
        if _is_empty_summary(raw_content or ""):
            return None
        # A reasoning model that burns its whole budget thinking returns an
        # empty content field rather than an error.
        _LOGGER.error(
            "vision model returned no text (finish_reason=%s) - raise MAX_TOKENS",
            choice.get("finish_reason"),
        )
    except (KeyError, IndexError, ValueError) as error:
        _LOGGER.error("could not parse vision response: %s", error)
    except Exception as error:  # noqa: BLE001 - never kill the poll loop
        _LOGGER.error("vision request failed: %s", error)
    return None
