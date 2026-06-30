"""Interactive one-time login + dialog lister.

Run once to authorise the user session (prompts for the Telegram code, and
2FA password if enabled). After authorising, it prints the groups/channels the
account can see so you can copy chat ids into ``TG_TARGET_CHATS``.

Usage:
    python -m tgpars.scripts.login
    tgpars-login   # if installed as a package
"""

from __future__ import annotations

import asyncio

from telethon.tl.types import Channel, Chat

from ..config import load_settings
from ..tg_client import build_client


async def _run() -> None:
    settings = load_settings()
    client = build_client(settings)

    await client.start(phone=lambda: settings.tg_phone or input("Phone (E.164): "))

    me = await client.get_me()
    username = f"@{me.username}" if me.username else "(no username)"
    print(f"Authorised as {me.first_name} {username} (id={me.id})\n")

    print("Groups / channels visible to this account:")
    print(f"{'chat_id':>16}  {'type':<10}  title")
    print("-" * 60)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, (Chat, Channel)):
            kind = "channel" if isinstance(entity, Channel) else "group"
            print(f"{dialog.id:>16}  {kind:<10}  {dialog.name}")

    print(
        "\nCopy the chat_id(s) you want to monitor into TG_TARGET_CHATS "
        "(comma-separated) in your .env file."
    )
    await client.disconnect()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
