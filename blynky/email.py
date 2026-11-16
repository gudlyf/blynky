"""Send plain-text emails over standard SMTP."""

import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage

from . import config

_LOGGER = logging.getLogger(__name__)
_recipient = ""


def configured():
    """Return True if SMTP is configured enough to send mail."""
    return bool(config.SMTP_HOST and (config.SMTP_FROM or config.SMTP_USER))


def recipient():
    """Return the configured recipient email address, or ''."""
    return _recipient


def status():
    """Return the email configuration state for the settings UI."""
    return {"configured": configured(), "to": _recipient}


def load():
    """Read the saved recipient address from disk."""
    global _recipient
    try:
        stored = json.loads(config.EMAIL_STATE.read_text(encoding="utf-8"))
        _recipient = str(stored.get("to", "") or "").strip()
    except FileNotFoundError:
        _recipient = ""
    except (OSError, ValueError, AttributeError) as error:
        _LOGGER.warning("ignoring unreadable %s: %s", config.EMAIL_STATE, error)
        _recipient = ""


def set_recipient(to_address):
    """Set and persist the shared recipient email address."""
    global _recipient
    _recipient = str(to_address or "").strip()
    payload = json.dumps({"to": _recipient})
    try:
        config.EMAIL_STATE.parent.mkdir(parents=True, exist_ok=True)
        spare = config.EMAIL_STATE.with_name(f".{config.EMAIL_STATE.name}.tmp")
        spare.write_text(payload, encoding="utf-8")
        spare.replace(config.EMAIL_STATE)
    except OSError as error:
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
    dest = (to_address or _recipient).strip()
    recipients = [addr.strip() for addr in dest.split(",") if addr.strip()]
    if not recipients:
        return False

    message = EmailMessage()
    message["From"] = config.SMTP_FROM or config.SMTP_USER
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    try:
        await asyncio.to_thread(_send_sync, message)
    except (smtplib.SMTPException, OSError) as error:
        _LOGGER.error(
            "email: failed to send %r to %s: %s", subject, ", ".join(recipients), error
        )
        return False
    _LOGGER.info("email: sent %r to %s", subject, ", ".join(recipients))
    return True
