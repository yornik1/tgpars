"""Non-interactive one-time login + dialog lister.

This environment has no interactive stdin, so the login code is supplied via
``.env`` (``TG_LOGIN_CODE``) rather than typed at a prompt. The flow is:

  1. Run with no ``TG_LOGIN_CODE`` set. Telegram sends a code to your app and we
     remember the pending login.
  2. Put that code in ``.env`` as ``TG_LOGIN_CODE=12345`` (and
     ``TG_2FA_PASSWORD=...`` if you use two-factor), then run again to sign in.

Once authorised the session is saved and the script lists visible chats so you
can copy ids into ``TG_TARGET_CHATS``.

Usage:
    python -m tgpars.scripts.login
    tgpars-login   # if installed as a package
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat

from ..config import Settings, load_settings
from ..tg_client import build_client

_PENDING = Path("sessions/.pending_login.json")


async def _list_dialogs(client: TelegramClient) -> None:
    me = await client.get_me()
    username = f"@{me.username}" if me.username else "(no username)"
    print(f"\nAuthorised as {me.first_name} {username} (id={me.id})\n")
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


async def _request_code(client: TelegramClient, phone: str) -> None:
    sent = await client.send_code_request(phone)
    _PENDING.parent.mkdir(parents=True, exist_ok=True)
    _PENDING.write_text(json.dumps({"phone": phone, "hash": sent.phone_code_hash}))
    print(
        "A login code was sent to your Telegram app.\n"
        "Put it in .env as  TG_LOGIN_CODE=12345  (and TG_2FA_PASSWORD=... if you "
        "use two-factor), then run this command again."
    )


async def _sign_in(client: TelegramClient, settings: Settings) -> bool:
    """Complete sign-in with the code in settings. Returns True on success."""
    pending = json.loads(_PENDING.read_text()) if _PENDING.exists() else {}
    phone = pending.get("phone") or settings.tg_phone
    phone_code_hash = pending.get("hash")

    try:
        await client.sign_in(
            phone=phone,
            code=settings.tg_login_code.strip(),
            phone_code_hash=phone_code_hash,
        )
    except SessionPasswordNeededError:
        if not settings.tg_2fa_password:
            print(
                "Two-factor is enabled. Set TG_2FA_PASSWORD in .env (keep "
                "TG_LOGIN_CODE too) and run this command again."
            )
            return False
        await client.sign_in(password=settings.tg_2fa_password)

    _PENDING.unlink(missing_ok=True)
    print("Sign-in successful. You can now clear TG_LOGIN_CODE/TG_2FA_PASSWORD from .env.")
    return True


async def _run(code: str | None = None, password: str | None = None) -> None:
    settings = load_settings()
    if not settings.tg_phone:
        raise SystemExit("Set TG_PHONE in .env (E.164, e.g. +1234567890).")

    # CLI args (interactive path) take precedence over .env values.
    if code:
        settings.tg_login_code = code
    if password:
        settings.tg_2fa_password = password

    client = build_client(settings)
    await client.connect()

    try:
        if await client.is_user_authorized():
            await _list_dialogs(client)
            return

        if settings.tg_login_code.strip():
            if not await _sign_in(client, settings):
                return
            await _list_dialogs(client)
        else:
            await _request_code(client, settings.tg_phone)
    finally:
        await client.disconnect()


def main() -> None:
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else None
    password = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(_run(code=code, password=password))


if __name__ == "__main__":
    main()
