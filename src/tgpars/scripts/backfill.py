"""Backfill recent history from the target chats into the database.

This is a one-shot read-only pull of the latest messages (current state only;
it cannot recover edits/deletions that happened before the live collector was
running). Useful to verify capture and to seed data for the signal parser.

Usage:
    python -m tgpars.scripts.backfill [limit_per_chat]
    # default limit is 200 messages per chat
"""

from __future__ import annotations

import asyncio
import logging
import sys

from telethon import TelegramClient, utils

from ..collector import storage
from ..collector.util import as_utc, download_photo, media_type
from ..config import Settings, load_settings
from ..db.session import create_db_engine, init_db, make_session_factory
from ..tg_client import build_client

log = logging.getLogger("tgpars.backfill")

DEFAULT_LIMIT = 200


async def _backfill_chat(
    client: TelegramClient, session_factory, target: str, limit: int | None, settings: Settings
) -> int:
    ref: object = int(target) if target.lstrip("-").isdigit() else target
    entity = await client.get_entity(ref)
    peer_id = utils.get_peer_id(entity)
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or peer_id

    stored = 0
    photos = 0
    async for message in client.iter_messages(entity, limit=limit):
        sender = await message.get_sender()
        username = getattr(sender, "username", None) if sender else None
        mpath = None
        if settings.download_media:
            mpath = await download_photo(
                message, chat_id=peer_id, message_id=message.id, media_dir=settings.media_dir
            )
            if mpath:
                photos += 1
        with session_factory() as session:
            storage.upsert_message(
                session,
                chat_id=peer_id,
                message_id=message.id,
                sender_id=message.sender_id,
                sender_username=username,
                text=message.message or None,
                media_type=media_type(message),
                posted_at=as_utc(message.date),
                reply_to_msg_id=message.reply_to_msg_id,
                media_path=mpath,
            )
        stored += 1
        if stored % 500 == 0:
            log.info("  %s: %d messages so far (%d photos)...", title, stored, photos)

    log.info("Backfilled %s: %d messages, %d photos (id=%s)", title, stored, photos, peer_id)
    return stored


async def _run(limit: int | None) -> None:
    settings: Settings = load_settings()

    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    client = build_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Not authorised. Run `python -m tgpars.scripts.login` first.")

    total = 0
    for target in settings.target_chats:
        try:
            total += await _backfill_chat(client, session_factory, target, limit, settings)
        except Exception as exc:  # noqa: BLE001 - report and continue with next chat
            log.warning("Backfill failed for %r: %s", target, exc)

    log.info("Backfill complete: %d messages total.", total)
    await client.disconnect()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Pass 0 (or "all") to fetch the entire channel history.
    arg = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_LIMIT)
    limit: int | None = None if arg in ("0", "all") else int(arg)
    asyncio.run(_run(limit))


if __name__ == "__main__":
    main()
