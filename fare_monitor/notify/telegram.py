"""Telegram Bot API notifier (optional)."""
from __future__ import annotations

LIMIT = 4000


class TelegramNotifier:
    name = "telegram"

    def __init__(self, config, session, github=None):
        self.token = config.secrets.telegram_bot_token
        self.chat_ids = [c.strip() for c in (config.secrets.telegram_chat_id or "").split(",") if c.strip()]
        self.session = session

    @classmethod
    def is_configured(cls, config, github=None) -> bool:
        return bool(config.secrets.telegram_bot_token and config.secrets.telegram_chat_id)

    def send(self, message, watch=None):
        text = f"{message.subject}\n\n{message.body}"
        text = text if len(text) <= LIMIT else text[:LIMIT] + "\n…"
        for chat_id in self.chat_ids:
            resp = self.session.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                     json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                                     timeout=30)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"Telegram HTTP {resp.status_code}: {data.get('description') or resp.text[:200]}")
        return True
