from __future__ import annotations


class ConsoleNotifier:
    name = "console"

    def __init__(self, config, session=None, github=None):
        pass

    @classmethod
    def is_configured(cls, config, github=None) -> bool:
        return True

    def send(self, message, watch=None):
        print("=" * 72)
        print(f"[{watch.route if watch else 'monitor'}] {message.subject}\n")
        print(message.body)
        print("=" * 72, flush=True)
        return True
