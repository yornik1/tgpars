"""Telegram bot notifier — DMs alerts to a single recipient.

Uses the Bot API sendMessage over plain HTTPS (no extra deps). Sending is
independent of getUpdates, so it works even if another process polls the same
bot token. Failures are swallowed (logged) so a notification problem never
takes down the collector.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request

log = logging.getLogger("tgpars.notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, token: str, chat_id: str, *, enabled: bool = True) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token and chat_id)

    def send(self, text: str) -> bool:
        """Send a plain-text message. Returns True on success, never raises."""
        if not self.enabled:
            return False
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:4000],  # Telegram hard limit is 4096
                "disable_web_page_preview": "true",
            }
        ).encode()
        try:
            req = urllib.request.Request(_API.format(token=self.token), data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as exc:  # noqa: BLE001 - notifications must not crash the daemon
            log.warning("notify failed: %s", exc)
            return False
