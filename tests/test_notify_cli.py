import pytest

from fare_monitor import cli
from fare_monitor.config import load_config
from fare_monitor.messages import plain_message
from fare_monitor.notify import Dispatcher
from fare_monitor.notify.email import EmailNotifier

from .conftest import FakeResponse, FakeSession


def test_email_message_has_text_and_html(config, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "monitor@example.com")
    monkeypatch.setenv("EMAIL_TO", "a@example.com,b@example.com")
    cfg = load_config()
    assert EmailNotifier.is_configured(cfg)
    msg = EmailNotifier(cfg).build(plain_message("Subject <x>", ["line & 1"]))
    assert msg["To"] == "a@example.com, b@example.com"
    parts = {p.get_content_type(): p.get_content() for p in msg.iter_parts()}
    assert "line & 1" in parts["text/plain"] and "line &amp; 1" in parts["text/html"]


def test_one_failing_channel_does_not_block_others(config, monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_CHANNELS", "telegram,console")
    cfg = load_config()
    session = FakeSession(lambda *a: FakeResponse(401, {"ok": False, "description": "Unauthorized"}))
    d = Dispatcher(cfg, session)
    assert d.send(plain_message("hello", ["x"])) == ["console"]
    assert "hello" in capsys.readouterr().out


def test_cli_without_providers_exits_2(config, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY")
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN")
    assert cli.main(["run", "--dry-run"]) == 2


def test_cli_check_config_hides_secrets(config, capsys):
    assert cli.main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "configured" in out and "test-key" not in out and "123:abc" not in out
