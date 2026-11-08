"""SMTP email notifier (optional; e.g. Gmail with an app password)."""
from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage


class EmailNotifier:
    name = "email"

    def __init__(self, config, session=None, github=None):
        self.s = config.secrets

    @classmethod
    def is_configured(cls, config, github=None) -> bool:
        s = config.secrets
        return bool(s.smtp_host and s.email_from and s.email_to)

    def build(self, message) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = self.s.email_from
        msg["To"] = ", ".join(self.s.email_to)
        msg.set_content(message.body)
        msg.add_alternative("<html><body><pre style=\"font-family:system-ui,sans-serif;white-space:pre-wrap\">"
                            + html.escape(message.body) + "</pre></body></html>", subtype="html")
        return msg

    def send(self, message, watch=None):
        msg = self.build(message)
        context = ssl.create_default_context()
        if self.s.smtp_use_ssl:
            server = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
            server.starttls(context=context)
        with server:
            if self.s.smtp_user:
                server.login(self.s.smtp_user, self.s.smtp_password or "")
            server.send_message(msg)
        return True
