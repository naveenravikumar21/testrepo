"""Telegram Bot API notifier (https://core.telegram.org/bots/api#sendmessage)."""
from __future__ import annotations

LIMIT = 4000  # Telegram hard limit is 4096 characters


class TelegramNotifier:
    name = "telegram"

    def __init__(self, config, session):
        self.token = config.secrets.telegram_bot_token
        self.chat_ids = [c.strip() for c in (config.secrets.telegram_chat_id or "").split(",") if c.strip()]
        self.session = session

    @classmethod
    def is_configured(cls, config) -> bool:
        return bool(config.secrets.telegram_bot_token and config.secrets.telegram_chat_id)

    def send(self, message):
        body = message.html if len(message.html) <= LIMIT else message.html[:LIMIT] + "\n…"
        for chat_id in self.chat_ids:
            resp = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": chat_id, "text": body, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=30,
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"Telegram HTTP {resp.status_code}: {data.get('description') or resp.text[:200]}")
