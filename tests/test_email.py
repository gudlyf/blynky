"""Emailing daily summaries, and the recipient setting behind it."""

from unittest.mock import MagicMock

import pytest

from blynky import config, daily, email, notify


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Give each test its own state files and no recipient or pause."""
    monkeypatch.setattr(config, "EMAIL_STATE", tmp_path / "email.json")
    monkeypatch.setattr(config, "NTFY_STATE", tmp_path / "ntfy.json")
    monkeypatch.setattr(config, "DAILY_STATE", tmp_path / "daily.json")
    monkeypatch.setattr(config, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(email, "_recipient", "")
    monkeypatch.setattr(notify, "_paused_until", None)


@pytest.fixture
def smtp(monkeypatch):
    """Configure SMTP and return the fake connection mail is sent over."""
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_STARTTLS", True)
    monkeypatch.setattr(config, "SMTP_SSL", False)
    monkeypatch.setattr(config, "SMTP_USER", "sender@gmail.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(config, "SMTP_FROM", "")
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    opener = MagicMock(return_value=context)
    monkeypatch.setattr(email.smtplib, "SMTP", opener)
    connection.opener = opener
    return connection


async def test_email_configured(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_FROM", "")
    assert not email.configured()

    monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config, "SMTP_USER", "user@gmail.com")
    assert email.configured()


async def test_send_daily_success(smtp):
    ok = await email.send_daily(
        "Front Door",
        "2026-11-15",
        "Quiet day with two deliveries.",
        system="Home",
        to_address="dest@example.com",
    )
    assert ok is True
    smtp.opener.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
    smtp.starttls.assert_called_once_with()
    smtp.login.assert_called_once_with("sender@gmail.com", "app-password")
    sent = smtp.send_message.call_args.args[0]
    assert sent["To"] == "dest@example.com"
    assert sent["From"] == "sender@gmail.com"
    assert "Front Door (Home)" in sent["Subject"]
    assert "Quiet day with two deliveries." in sent.get_content()


async def test_send_daily_goes_to_every_saved_recipient(smtp):
    await email.set_recipient(["a@example.com", "b@example.com"])
    assert await email.send_daily("Porch", "2026-11-15", "Nothing much.")
    sent = smtp.send_message.call_args.args[0]
    assert sent["To"] == "a@example.com, b@example.com"


async def test_pause_holds_for_email_too(smtp):
    await email.set_recipient(["dest@example.com"])
    await notify.pause()
    assert await email.send_daily("Porch", "2026-11-15", "Nothing much.") is False
    smtp.send_message.assert_not_called()


async def test_bad_header_is_logged_not_raised(smtp):
    assert await email.send("dest@example.com", "two\nlines", "body") is False
    smtp.send_message.assert_not_called()


@pytest.mark.parametrize(
    "text", ["a@b.com\nBcc: x@y.com", "nobody", "a@b.com, @c.com", "a b@c.com"]
)
def test_parse_recipients_refuses(text):
    with pytest.raises(ValueError):
        email.parse_recipients(text)


def test_parse_recipients_accepts():
    assert email.parse_recipients(" a@b.com ,b@c.org") == ["a@b.com", "b@c.org"]
    assert email.parse_recipients("") == []


async def test_daily_and_email_persist_settings():
    await email.set_recipient(["user@example.com"])
    email._recipient = ""
    await email.load()
    assert email.recipient() == "user@example.com"

    store = daily.Daily()
    await store.load()
    await store.set_email(True)

    reloaded = daily.Daily()
    await reloaded.load()
    assert reloaded.email is True


async def test_api_email_round_trip(client):
    response = await client.post("/api/email", json={"to": " a@b.com , c@d.org "})
    assert response.status == 200
    assert (await response.json())["to"] == "a@b.com, c@d.org"
    assert (await (await client.get("/api/email")).json())["to"] == "a@b.com, c@d.org"

    cleared = await client.post("/api/email", json={"to": ""})
    assert (await cleared.json())["to"] == ""


@pytest.mark.parametrize("to", ["a@b.com\nBcc: x@y.com", "nobody", 5, None])
async def test_api_email_refuses_bad_addresses(client, to):
    response = await client.post("/api/email", json={"to": to})
    assert response.status == 400
    assert email.recipient() == ""


async def test_api_email_from_another_site_is_refused(client):
    response = await client.post(
        "/api/email",
        json={"to": "a@b.com"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status == 403
    assert email.recipient() == ""
