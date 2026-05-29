import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

async def main():
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
    await client.connect()
    print("Connected to DC:", client.session.dc_id)
    
    phone = "+91" + "9876543210" # Replace with actual logic if needed, but we just want to see if we can trigger the error on the user's phone.
    # Actually, we can't test without the user's phone number.
