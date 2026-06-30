"""Shared Telethon client factory.

The client logs in as a regular *user* (MTProto user-client) so it can read
closed groups the account already belongs to. It is used read-only: the
collector never sends messages, reacts, or joins/leaves chats.
"""

from __future__ import annotations

import os
from pathlib import Path

from telethon import TelegramClient

from .config import Settings


def build_client(settings: Settings) -> TelegramClient:
    """Create a TelegramClient using the configured user session.

    The session file is created/reused at ``settings.tg_session_path``. The
    parent directory is created if needed.
    """
    session_path = Path(settings.tg_session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    return TelegramClient(
        os.fspath(session_path),
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        flood_sleep_threshold=settings.flood_sleep_threshold,
    )
