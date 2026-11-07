"""Configurable notification channels: telegram, email, console."""
from __future__ import annotations

import logging

from .console import ConsoleNotifier
from .email import EmailNotifier
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)

CHANNELS = {"telegram": TelegramNotifier, "email": EmailNotifier, "console": ConsoleNotifier}


class NotificationError(Exception):
    pass


class Dispatcher:
    def __init__(self, config, session, channels: list[str] | None = None):
        self.notifiers = []
        for name in channels or config.notify_channels:
            cls = CHANNELS.get(name.lower())
            if cls is None:
                log.error("Unknown notification channel %r (known: %s)", name, ", ".join(CHANNELS))
                continue
            if not cls.is_configured(config):
                log.error("Notification channel %s is selected but not configured (missing secrets)", name)
                continue
            self.notifiers.append(cls(config, session))

    @property
    def names(self) -> list[str]:
        return [n.name for n in self.notifiers]

    def send(self, message) -> list[str]:
        """Send to every channel. Returns the channels that accepted the message."""
        delivered = []
        for n in self.notifiers:
            try:
                n.send(message)
                delivered.append(n.name)
            except Exception as exc:  # one broken channel must not block the others
                log.error("Notification via %s failed: %s", n.name, exc)
        return delivered
