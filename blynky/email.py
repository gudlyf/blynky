"""Send plain-text emails over standard SMTP."""

import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage
from email.utils import getaddresses

from . import config, notify
from .files import write_atomic

_LOGGER = logging.getLogger(__name__)
_recipient = ""


def configured():
    """Return True if SMTP is configured enough to send mail."""
    return bool(config.SMTP_HOST and (config.SMTP_FROM or config.SMTP_USER))


def recipient():
    """Return the configured recipient email address(es), or ''."""
    return _recipient


def status():
    """Return the email configuration state for the settings UI."""
    return {"configured": configured(), "to": _recipient}


def parse_recipients(text):
    """Return the addresses in a comma-separated list, or raise ValueError.

    Each must be a plain address with something either side of one @. Line
    breaks are refused outright: they could only be an attempt at a header.
    """
    text = str(text or "")
    if "\r" in text or "\n" in text:
        raise ValueError("line breaks are not allowed")
    found = []
    for name, address in getaddresses([text]):
        if not (name or address):
            continue  # nothing between two commas, or nothing at all
        local, at, domain = address.rpartition("@")
        if not (at and local and domain) or any(c.isspace() for c in address):
            raise ValueError(f"not an email address: {address or name!r}")
        found.append(address)
    return found


async def load():
    """Read the saved recipient address(es) from disk."""
    global _recipient
    try:
        text = await asyncio.to_thread(config.EMAIL_STATE.read_text, encoding="utf-8")
    except FileNotFoundError:
        _recipient = ""
        return
    except OSError as error:
        _LOGGER.warning("could not read %s: %s", config.EMAIL_STATE, error)
        _recipient = ""
        return
    try:
        stored = json.loads(text)
        _recipient = ", ".join(parse_recipients(stored.get("to", "")))
    except (ValueError, AttributeError) as error:
        _LOGGER.warning("ignoring unreadable %s: %s", config.EMAIL_STATE, error)
        _recipient = ""


async def set_recipient(addresses):
    """Set and persist the shared recipient address(es), a list of strings."""
    global _recipient
    _recipient = ", ".join(addresses)
    payload = json.dumps({"to": _recipient})
    try:
        await asyncio.to_thread(write_atomic, config.EMAIL_STATE, payload)
    except OSError as error:
        # Still set for as long as this process runs.
        _LOGGER.warning("could not write %s: %s", config.EMAIL_STATE, error)
    _LOGGER.info("email recipient %s", _recipient or "(cleared)")
    return status()


def _send_sync(message):
    if config.SMTP_SSL:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            if config.SMTP_USER and config.SMTP_PASSWORD:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(message)
        return
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        if config.SMTP_STARTTLS:
            smtp.starttls()
        if config.SMTP_USER and config.SMTP_PASSWORD:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(message)


async def send(to_address, subject, body):
    """Send a plain-text email. Returns True if it was sent."""
    if not configured():
        return False
    try:
        recipients = parse_recipients(to_address or _recipient)
    except ValueError as error:
        _LOGGER.error("email: bad recipient list: %s", error)
        return False
    if not recipients:
        return False

    try:
        message = EmailMessage()
        message["From"] = config.SMTP_FROM or config.SMTP_USER
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        await asyncio.to_thread(_send_sync, message)
    except (smtplib.SMTPException, OSError, ValueError) as error:
        _LOGGER.error(
            "email: failed to send %r to %s: %s", subject, ", ".join(recipients), error
        )
        return False
    _LOGGER.info("email: sent %r to %s", subject, ", ".join(recipients))
    return True


async def send_daily(camera, day, summary, system=None, to_address=None):
    """Email one daily summary. Returns True if it was sent.

    Like the ntfy copy, a pause from the settings menu holds for these too.
    """
    if notify.status()["paused"]:
        _LOGGER.debug("email: notifications paused, not sending %s", camera)
        return False
    label = f"{camera} ({system})" if system else camera
    subject = f"Blynky daily summary: {label} - {day}"
    body = f"{label} — {day}\n\n{summary}\n"
    return await send(to_address, subject, body)
