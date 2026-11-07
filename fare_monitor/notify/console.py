from __future__ import annotations


class ConsoleNotifier:
    name = "console"

    def __init__(self, config, session):
        pass

    @classmethod
    def is_configured(cls, config) -> bool:
        return True

    def send(self, message):
        print("=" * 72)
        print(message.text)
        print("=" * 72, flush=True)
