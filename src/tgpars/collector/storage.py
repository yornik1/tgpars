"""Persistence helpers for the collector.

Plain synchronous SQLAlchemy calls. At single-group scale SQLite writes are
fast enough to call inline from the async handlers; if multiple high-traffic
chats are added later, wrap these in ``asyncio.to_thread``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import DeletionEvent, Edit, Message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_message(
    session: Session,
    *,
    chat_id: int,
    message_id: int,
    sender_id: int | None,
    sender_username: str | None,
    text: str | None,
    media_type: str | None,
    posted_at: datetime,
    reply_to_msg_id: int | None = None,
) -> Message:
    """Insert a new message snapshot, or return the existing one if already stored.

    If the row already exists but is missing ``reply_to_msg_id`` (e.g. captured
    before that column existed), backfill the value without touching anything
    else — this preserves live-captured deletion/edit state.
    """
    existing = session.scalar(
        select(Message).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
    )
    if existing is not None:
        if existing.reply_to_msg_id is None and reply_to_msg_id is not None:
            existing.reply_to_msg_id = reply_to_msg_id
            session.commit()
        return existing

    msg = Message(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_username=sender_username,
        original_text=text,
        text=text,
        media_type=media_type,
        posted_at=posted_at,
        reply_to_msg_id=reply_to_msg_id,
        collected_at=_utcnow(),
    )
    session.add(msg)
    session.commit()
    return msg


def record_edit(
    session: Session,
    *,
    chat_id: int,
    message_id: int,
    new_text: str | None,
    telegram_edit_date: datetime | None,
) -> Edit | None:
    """Record an edit if the message is known and the text actually changed."""
    msg = session.scalar(
        select(Message).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
    )
    if msg is None or msg.text == new_text:
        return None

    edit = Edit(
        message_pk=msg.id,
        previous_text=msg.text,
        new_text=new_text,
        telegram_edit_date=telegram_edit_date,
        detected_at=_utcnow(),
    )
    msg.text = new_text
    msg.edited_at = telegram_edit_date or _utcnow()
    msg.edit_count += 1
    session.add(edit)
    session.commit()
    return edit


def mark_deleted(
    session: Session, *, chat_id: int | None, message_id: int
) -> Message | None:
    """Flag a captured message as deleted and log the raw deletion event.

    ``chat_id`` may be ``None`` for some Telegram deletion updates; in that case
    we match on ``message_id`` alone (best effort) and log the event regardless.
    """
    query = select(Message).where(Message.message_id == message_id)
    if chat_id is not None:
        query = query.where(Message.chat_id == chat_id)

    msg = session.scalar(query)
    now = _utcnow()

    if msg is not None and not msg.is_deleted:
        msg.is_deleted = True
        msg.deleted_detected_at = now

    session.add(
        DeletionEvent(
            chat_id=chat_id,
            message_id=message_id,
            matched_message_pk=msg.id if msg is not None else None,
            detected_at=now,
        )
    )
    session.commit()
    return msg
