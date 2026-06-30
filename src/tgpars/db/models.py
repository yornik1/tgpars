"""SQLAlchemy ORM models.

Design principle: snapshot every incoming message immediately. Deletions and
edits are detected against this snapshot, because Telegram does not return the
content of a message once it has been deleted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Message(Base):
    """A single captured Telegram message and its current observed state."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_chat_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Telegram identity (message_id is unique only within a chat).
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)

    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    sender_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Content. original_text preserves what we first saw; text tracks current state.
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Timestamps (all UTC).
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edit_count: Mapped[int] = mapped_column(Integer, default=0)

    # Deletion tracking — the core manipulation signal.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    edits: Mapped[list["Edit"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class Edit(Base):
    """An observed edit of a message (history of changes)."""

    __tablename__ = "edits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)

    previous_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_edit_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    message: Mapped[Message] = relationship(back_populates="edits")


class DeletionEvent(Base):
    """Raw deletion events as reported by Telegram.

    Telegram's MessageDeleted update gives message ids (sometimes without a
    chat), which may or may not map to a row we captured. We log every event
    here for auditability, in addition to flagging the matched Message.
    """

    __tablename__ = "deletion_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    matched_message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
