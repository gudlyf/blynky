"""What Blynky sends to ntfy besides the message."""

from blynky import config, notify


def test_no_token_no_header(monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOKEN", "")
    assert notify._auth() == {}


def test_token_is_a_bearer_header(monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOKEN", "tk_abc")
    assert notify._auth() == {"Authorization": "Bearer tk_abc"}
