"""
generate_session.py
Run this script LOCALLY on your own computer (NOT on the server).
It will ask for your phone number and OTP, then print a session string.
Paste that string into the bot using /importsession.

Usage:
    python generate_session.py
"""

import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

if not API_ID or not API_HASH:
    print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
    exit(1)


async def main():
    print("=" * 60)
    print("  Telegram Session Generator — run on your LOCAL machine")
    print("=" * 60)
    print()

    client = TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        device_model="iPhone 14 Pro Max",
        system_version="iOS 16.6",
        app_version="10.3.2",
        lang_code="en",
        system_lang_code="en-US",
    )

    await client.start()   # Handles phone + code + 2FA interactively

    me = await client.get_me()
    print()
    print(f"✅ Logged in as: {me.first_name} (@{me.username})")
    print()

    session_string = client.session.save()
    print("=" * 60)
    print("YOUR SESSION STRING (copy everything between the lines):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print()
    print("Now paste this into your Telegram bot with the command:")
    print("  /importsession <paste the session string here>")
    print()

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
