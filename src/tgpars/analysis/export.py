"""Export collected messages into an LLM-friendly form.

The product does NOT call any LLM API. Instead this exporter turns the SQLite
contents into clean Markdown (or JSONL) that an in-session agent (Claude Code /
Codex, on a subscription) reads to classify signals, link result-updates to
their signal by tag, and flag manipulation (deleted / edited posts).

Usage:
    python -m tgpars.analysis.export                 # all chats -> markdown to stdout
    python -m tgpars.analysis.export --chat -100... --format jsonl --out dump.jsonl
    python -m tgpars.analysis.export --since 2026-06-30 --limit 200
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_settings
from ..db.models import Message
from ..db.session import create_db_engine, make_session_factory

# Hashtags used by the channels both as signal refs (#f10, #c30, #2170) and as
# assets (#BTCUSDT). We surface them all; the agent decides which is which.
_HASHTAG_RE = re.compile(r"#\w+")


def extract_hashtags(text: str | None) -> list[str]:
    return _HASHTAG_RE.findall(text) if text else []


def fetch_messages(
    session: Session,
    *,
    chat_id: int | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[Message]:
    query = select(Message)
    if chat_id is not None:
        query = query.where(Message.chat_id == chat_id)
    if since is not None:
        query = query.where(Message.posted_at >= since)
    query = query.order_by(Message.chat_id, Message.posted_at)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def _message_record(m: Message) -> dict:
    return {
        "chat_id": m.chat_id,
        "message_id": m.message_id,
        "posted_at": m.posted_at.isoformat() if m.posted_at else None,
        "sender": m.sender_username or m.sender_id,
        "text": m.text,
        "hashtags": extract_hashtags(m.text),
        "is_deleted": m.is_deleted,
        "deleted_detected_at": (
            m.deleted_detected_at.isoformat() if m.deleted_detected_at else None
        ),
        "edit_count": m.edit_count,
        "media_type": m.media_type,
    }


def to_jsonl(messages: Iterable[Message]) -> str:
    return "\n".join(
        json.dumps(_message_record(m), ensure_ascii=False) for m in messages
    )


def to_markdown(messages: Iterable[Message]) -> str:
    lines: list[str] = []
    current_chat: int | None = None
    for m in messages:
        if m.chat_id != current_chat:
            current_chat = m.chat_id
            lines.append(f"\n## chat {m.chat_id}\n")

        flags = []
        if m.is_deleted:
            ts = m.deleted_detected_at.isoformat() if m.deleted_detected_at else "?"
            flags.append(f"🗑 DELETED (detected {ts})")
        if m.edit_count:
            flags.append(f"✏️ edited x{m.edit_count}")
        flag_str = ("  " + " ".join(flags)) if flags else ""

        ts = m.posted_at.isoformat() if m.posted_at else "?"
        sender = m.sender_username or m.sender_id or "channel"
        lines.append(f"### [{m.message_id}] {ts} — {sender}{flag_str}")
        tags = extract_hashtags(m.text)
        if tags:
            lines.append(f"tags: {' '.join(tags)}")
        lines.append((m.text or "(no text / media only)").strip())
        lines.append("")
    return "\n".join(lines)


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export collected messages for analysis.")
    parser.add_argument("--chat", type=int, default=None, help="filter by chat_id")
    parser.add_argument("--since", default=None, help="ISO date/time lower bound (UTC)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--format", choices=["md", "jsonl"], default="md")
    parser.add_argument("--out", default=None, help="output file (default: stdout)")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        messages = fetch_messages(
            session, chat_id=args.chat, since=_parse_since(args.since), limit=args.limit
        )
        rendered = to_jsonl(messages) if args.format == "jsonl" else to_markdown(messages)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
        print(f"Wrote {len(messages)} messages to {args.out}", file=sys.stderr)
    else:
        print(rendered)


if __name__ == "__main__":
    main()
