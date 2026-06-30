"""Small shared helpers for the collector and backfill."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("tgpars.media")


def media_type(message) -> str | None:
    """Class name of the message's media, or None for text-only messages."""
    media = getattr(message, "media", None)
    return type(media).__name__ if media is not None else None


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a (possibly naive) datetime to timezone-aware UTC."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def download_photo(message, *, chat_id: int, message_id: int, media_dir: str) -> str | None:
    """Download a message's photo to ``media_dir/<chat_id>/<message_id>.jpg``.

    Returns the saved path (str) or None if the message has no photo. Skips the
    download if the file already exists (idempotent re-backfill). Never raises —
    a download failure must not stop collection.
    """
    if not getattr(message, "photo", None):
        return None
    out = Path(media_dir) / str(chat_id) / f"{message_id}.jpg"
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        saved = await message.download_media(file=str(out))
        return saved if saved else None
    except Exception as exc:  # noqa: BLE001 - downloads must not crash collection
        log.warning("photo download failed for %s/%s: %s", chat_id, message_id, exc)
        return None
