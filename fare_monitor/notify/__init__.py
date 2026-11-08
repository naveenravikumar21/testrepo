"""Configurable notification channels: github (issue comments -> GitHub email), email, telegram, console."""
from __future__ import annotations

import logging

from .console import ConsoleNotifier
from .email import EmailNotifier
from .github import GitHubNotifier
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)

CHANNELS = {"github": GitHubNotifier, "telegram": TelegramNotifier, "email": EmailNotifier,
            "console": ConsoleNotifier}


class Dispatcher:
    def __init__(self, config, session, channels: list[str] | None = None, github=None):
        self.notifiers = []
        for name in channels or config.notify_channels:
            cls = CHANNELS.get(name.lower())
            if cls is None:
                log.error("Unknown notification channel %r (known: %s)", name, ", ".join(CHANNELS))
                continue
            if not cls.is_configured(config, github):
                (log.info if name == "github" else log.error)(
                    "Notification channel %s is not available here", name)
                continue
            self.notifiers.append(cls(config, session, github))

    @property
    def names(self) -> list[str]:
        return [n.name for n in self.notifiers]

    def send(self, message, watch=None) -> list[str]:
        """Send to every channel. Returns the channels that accepted the message."""
        delivered = []
        for n in self.notifiers:
            try:
                if n.send(message, watch) is not False:
                    delivered.append(n.name)
            except Exception as exc:  # one broken channel must not block the others
                log.error("Notification via %s failed: %s", n.name, exc)
        return delivered
