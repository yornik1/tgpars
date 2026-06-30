"""Small shared helpers for the collector and backfill."""

from __future__ import annotations

from datetime import datetime, timezone


def media_type(message) -> str | None:
    """Class name of the message's media, or None for text-only messages."""
    media = getattr(message, "media", None)
    return type(media).__name__ if media is not None else None


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a (possibly naive) datetime to timezone-aware UTC."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
