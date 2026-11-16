from unittest.mock import MagicMock

from blynky import config, daily, email


async def test_email_configured(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_FROM", "")
    assert not email.configured()

    monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config, "SMTP_USER", "user@gmail.com")
    assert email.configured()


async def test_send_daily_success(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_STARTTLS", True)
    monkeypatch.setattr(config, "SMTP_SSL", False)
    monkeypatch.setattr(config, "SMTP_USER", "sender@gmail.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(config, "SMTP_FROM", "")

    smtp_instance = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__.return_value = smtp_instance
    smtp_cls = MagicMock(return_value=smtp_cm)
    monkeypatch.setattr(email.smtplib, "SMTP", smtp_cls)

    ok = await daily.send_email(
        "Front Door",
        "2026-11-15",
        "Quiet day with two deliveries.",
        system="Home",
        to_address="dest@example.com",
    )
    assert ok is True
    smtp_cls.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
    smtp_instance.starttls.assert_called_once_with()
    smtp_instance.login.assert_called_once_with("sender@gmail.com", "app-password")
    smtp_instance.send_message.assert_called_once()
    sent_msg = smtp_instance.send_message.call_args.args[0]
    assert sent_msg["To"] == "dest@example.com"
    assert sent_msg["From"] == "sender@gmail.com"
    assert "Front Door" in sent_msg["Subject"]
    assert "Quiet day with two deliveries." in sent_msg.get_content()


async def test_daily_and_email_persist_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(config, "DAILY_STATE", tmp_path / "daily.json")
    monkeypatch.setattr(config, "EMAIL_STATE", tmp_path / "email.json")

    email.load()
    email.set_recipient("user@example.com")
    email.load()
    assert email.recipient() == "user@example.com"

    store = daily.Daily()
    await store.load()
    await store.set_email(True)

    reloaded = daily.Daily()
    await reloaded.load()
    assert reloaded.email is True

