"""Always-on collector daemon.

Listens to the configured target chats with a Telethon user-client and:
  * stores every new message immediately (snapshot),
  * records edits (with previous text preserved),
  * flags deletions the moment Telegram reports them.

Deletions are only observable while the daemon is connected, so it is meant to
run continuously (e.g. systemd / docker with restart=always).

Usage:
    python -m tgpars.collector.daemon
    tgpars-collect   # if installed as a package
"""

from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events

from ..config import Settings, load_settings
from ..db.session import create_db_engine, init_db, make_session_factory
from ..notify import Notifier
from ..tg_client import build_client
from . import storage
from .util import as_utc, download_photo, media_type

log = logging.getLogger("tgpars.collector")


async def _resolve_targets(client: TelegramClient, settings: Settings) -> list[int]:
    """Resolve configured targets to marked peer ids matching event.chat_id."""
    from telethon import utils

    resolved: list[int] = []
    for target in settings.target_chats:
        ref: object = int(target) if target.lstrip("-").isdigit() else target
        try:
            entity = await client.get_entity(ref)
        except Exception as exc:  # noqa: BLE001 - surface and continue
            log.warning("Could not resolve target %r: %s", target, exc)
            continue
        peer_id = utils.get_peer_id(entity)
        resolved.append(peer_id)
        log.info("Monitoring %r -> %s", target, peer_id)

    if not resolved:
        raise SystemExit(
            "No resolvable target chats. Set TG_TARGET_CHATS in .env "
            "(run `tgpars-login` to list available chat ids)."
        )
    return resolved


def _register_handlers(
    client: TelegramClient, session_factory, target_ids: list[int], settings: Settings
) -> None:
    notifier = Notifier(
        settings.tg_bot_token, settings.tg_alert_chat_id, enabled=settings.alerts_enabled
    )
    alert_events = settings.alert_events

    @client.on(events.NewMessage(chats=target_ids))
    async def on_new_message(event):  # noqa: ANN001
        sender = await event.get_sender()
        username = getattr(sender, "username", None) if sender else None
        mpath = None
        if settings.download_media:
            mpath = await download_photo(
                event.message,
                chat_id=event.chat_id,
                message_id=event.message.id,
                media_dir=settings.media_dir,
            )
        with session_factory() as session:
            storage.upsert_message(
                session,
                chat_id=event.chat_id,
                message_id=event.message.id,
                sender_id=event.sender_id,
                sender_username=username,
                text=event.message.message or None,
                media_type=media_type(event.message),
                posted_at=as_utc(event.message.date),
                reply_to_msg_id=event.message.reply_to_msg_id,
                media_path=mpath,
            )
        log.info("msg %s/%s stored", event.chat_id, event.message.id)

    @client.on(events.MessageEdited(chats=target_ids))
    async def on_message_edited(event):  # noqa: ANN001
        with session_factory() as session:
            edit = storage.record_edit(
                session,
                chat_id=event.chat_id,
                message_id=event.message.id,
                new_text=event.message.message or None,
                telegram_edit_date=as_utc(event.message.edit_date),
            )
        if edit is not None:
            log.info("msg %s/%s edited", event.chat_id, event.message.id)
            if "edit" in alert_events:
                prev = (edit.previous_text or "")[:500]
                new = (edit.new_text or "")[:500]
                notifier.send(
                    f"✏️ EDITED in {event.chat_id} (msg {event.message.id})\n\n"
                    f"BEFORE:\n{prev}\n\nAFTER:\n{new}"
                )

    @client.on(events.MessageDeleted(chats=target_ids))
    async def on_message_deleted(event):  # noqa: ANN001
        with session_factory() as session:
            for message_id in event.deleted_ids:
                matched = storage.mark_deleted(
                    session, chat_id=event.chat_id, message_id=message_id
                )
                status = "matched" if matched else "unmatched"
                log.info("DELETION %s/%s (%s)", event.chat_id, message_id, status)
                if "deletion" in alert_events:
                    if matched is not None:
                        body = (matched.text or "(media / no text)")[:700]
                        when = (
                            matched.posted_at.isoformat() if matched.posted_at else "?"
                        )
                        notifier.send(
                            f"🗑 DELETED post caught in {event.chat_id}\n"
                            f"msg {message_id}, originally posted {when}\n\n{body}"
                        )
                    else:
                        notifier.send(
                            f"🗑 DELETION detected in {event.chat_id} (msg {message_id}) "
                            f"— content was not captured (posted while collector was down)."
                        )


async def _run() -> None:
    settings = load_settings()

    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    client = build_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "Not authorised yet. Run `python -m tgpars.scripts.login` first "
            "to create the session."
        )

    target_ids = await _resolve_targets(client, settings)
    _register_handlers(client, session_factory, target_ids, settings)

    log.info(
        "Alerts: %s (events: %s)",
        "ON" if settings.alerts_enabled else "off",
        ",".join(sorted(settings.alert_events)) if settings.alerts_enabled else "-",
    )
    log.info("Collector running. Watching %d chat(s). Ctrl-C to stop.", len(target_ids))
    await client.run_until_disconnected()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
