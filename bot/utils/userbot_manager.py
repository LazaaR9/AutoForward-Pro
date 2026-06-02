"""
bot/utils/userbot_manager.py
Manages dynamic Telethon userbot sessions running in the background.
"""

from __future__ import annotations

import logging
import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from bot.db import channels as channels_db
from bot.db import users as users_db
from bot.utils.filters import apply_filters, should_block_apk

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Session storage
# ─────────────────────────────────────────────────────────────────────────────
_clients: dict[int, TelegramClient] = {}
_session_dir = os.path.expanduser("~/.tg_bot_sessions")
os.makedirs(_session_dir, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Pending auth — module-level dict so the TelegramClient object is never
# copied or re-serialised between PTB handler calls.
# ─────────────────────────────────────────────────────────────────────────────
_pending_auth: dict[int, dict] = {}


def set_pending_auth(admin_id: int, client: TelegramClient, phone: str, code_hash: str) -> None:
    _pending_auth[admin_id] = {"client": client, "phone": phone, "code_hash": code_hash}


def get_pending_auth(admin_id: int) -> dict | None:
    return _pending_auth.get(admin_id)


def clear_pending_auth(admin_id: int) -> None:
    pending = _pending_auth.pop(admin_id, None)
    if pending and pending.get("client"):
        try:
            pending["client"].disconnect()
        except:
            pass


def _make_auth_client() -> TelegramClient:
    return TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        device_model="Samsung Galaxy S23",
        system_version="Android 14",
        app_version="10.3.2",
        lang_code="en",
        system_lang_code="en-IN",
        flood_sleep_threshold=60,
    )


async def prepare_auth_client(phone: str) -> TelegramClient:
    client = _make_auth_client()
    await client.connect()
    await asyncio.sleep(1)  # let connection stabilize
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Session persistence
# ─────────────────────────────────────────────────────────────────────────────

def save_session_string(admin_id: int, session_string: str) -> None:
    path = os.path.join(_session_dir, f"admin_{admin_id}.session_str")
    with open(path, "w") as f:
        f.write(session_string)
    logger.info("Session string saved to local file for admin %s", admin_id)

    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        db.table("users").update({"session_string": session_string}).eq("user_id", admin_id).execute()
        logger.info("Session string also saved to DB for admin %s", admin_id)
    except Exception as e:
        logger.debug("DB session_string save skipped: %s", e)


def load_session_string(admin_id: int) -> str | None:
    path = os.path.join(_session_dir, f"admin_{admin_id}.session_str")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()

    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        res = db.table("users").select("session_string").eq("user_id", admin_id).execute()
        data = getattr(res, "data", [])
        if isinstance(data, list) and len(data) > 0:
            row = data[0]
            if isinstance(row, dict) and row.get("session_string"):
                return str(row["session_string"])
    except Exception:
        pass
    return None


def is_userbot_authorized(admin_id: int) -> bool:
    return bool(load_session_string(admin_id))


def remove_session(admin_id: int) -> None:
    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        db.table("users").update({"session_string": None}).eq("user_id", admin_id).execute()
    except Exception:
        pass
    for path in [
        os.path.join(_session_dir, f"admin_{admin_id}.session_str"),
        os.path.join(_session_dir, f"admin_{admin_id}.session"),
        os.path.join(_session_dir, f"admin_{admin_id}"),
    ]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Userbot lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def start_userbot(admin_id: int, bot) -> TelegramClient | None:
    if admin_id in _clients:
        return _clients[admin_id]

    session_string = load_session_string(admin_id)
    if not session_string:
        return None

    try:
        logger.info("Starting background userbot for admin %s...", admin_id)
        client = TelegramClient(
            StringSession(session_string),
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
            device_model="Samsung Galaxy S23",
            system_version="Android 14",
            app_version="10.3.2",
            lang_code="en",
            system_lang_code="en-IN",
            flood_sleep_threshold=60,
        )
        await client.connect()

        if not await client.is_user_authorized():
            logger.warning("Session unauthorized for admin %s. Removing.", admin_id)
            await client.disconnect()
            remove_session(admin_id)
            return None

        source = channels_db.get_source_channel(admin_id)
        if source:
            _register_forwarding_listener(client, admin_id, source["channel_id"], bot)
            logger.info("Userbot listening on source %s for admin %s", source["channel_id"], admin_id)

        _clients[admin_id] = client
        return client

    except Exception as exc:
        logger.error("Failed to start userbot for admin %s: %s", admin_id, exc)
        return None


def _register_forwarding_listener(client: TelegramClient, admin_id: int, source_channel_id: int, bot) -> None:
    def normalize_id(cid) -> int:
        s = str(cid)
        if s.startswith("-100"): return int(s[4:])
        if s.startswith("-"):    return int(s[1:])
        return int(s)

    @client.on(events.NewMessage)
    async def handler(event):
        if normalize_id(event.chat_id) != normalize_id(source_channel_id):
            return

        targets = channels_db.get_target_channels(admin_id)
        if not targets:
            return

        # Prevent forwarding old messages if the bot was offline
        if event.message.date:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            msg_date = event.message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if (now - msg_date).total_seconds() > 120:
                return

        original_text = event.message.message or ""
        filtered_text = apply_filters(admin_id, original_text)
        
        if filtered_text is None:
            return  # Message was blocked by a keyword filter

        # Check for APK block
        if event.message.file and event.message.file.name and event.message.file.name.lower().endswith('.apk'):
            if should_block_apk(admin_id):
                return

        media_path = None
        if event.message.media:
            try:
                temp_dir = os.path.join(os.getcwd(), "scratch", "temp_media")
                os.makedirs(temp_dir, exist_ok=True)
                media_path = await event.message.download_media(file=temp_dir)
            except Exception as e:
                logger.error("Media download failed for admin %s: %s", admin_id, e)

        for target in targets:
            target_id = target["channel_id"]
            try:
                if media_path and os.path.exists(media_path):
                    if event.message.sticker or (media_path and media_path.lower().endswith(('.webm', '.tgs', '.webp'))):
                        try:
                            with open(media_path, "rb") as f:
                                await bot.send_sticker(chat_id=target_id, sticker=f)
                        except Exception as e:
                            logger.warning("Dropped invalid sticker (could be premium emoji): %s", e)
                    elif event.message.photo:
                        with open(media_path, "rb") as f:
                            await bot.send_photo(chat_id=target_id, photo=f, caption=filtered_text or None)
                    elif event.message.video:
                        with open(media_path, "rb") as f:
                            await bot.send_video(chat_id=target_id, video=f, caption=filtered_text or None)
                    elif event.message.audio:
                        with open(media_path, "rb") as f:
                            await bot.send_audio(chat_id=target_id, audio=f, caption=filtered_text or None)
                    elif event.message.voice:
                        with open(media_path, "rb") as f:
                            await bot.send_voice(chat_id=target_id, voice=f, caption=filtered_text or None)
                    elif event.message.video_note:
                        with open(media_path, "rb") as f:
                            await bot.send_video_note(chat_id=target_id, video_note=f)
                    else:
                        with open(media_path, "rb") as f:
                            await bot.send_document(chat_id=target_id, document=f, caption=filtered_text or None)
                else:
                    if filtered_text:
                        await bot.send_message(chat_id=target_id, text=filtered_text)
            except Exception as exc:
                logger.error("Forward to %s failed (admin %s): %s", target_id, admin_id, exc)

        if media_path and os.path.exists(media_path):
            try:
                os.remove(media_path)
            except Exception:
                pass


async def restart_userbot_listener(admin_id: int, bot) -> None:
    await stop_userbot(admin_id)
    await start_userbot(admin_id, bot)


async def stop_userbot(admin_id: int) -> None:
    client = _clients.pop(admin_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.error("Error stopping userbot for admin %s: %s", admin_id, exc)


async def start_all_userbots(bot) -> None:
    try:
        from bot.db.supabase_client import get_client
        db = get_client()
        res = db.table("users").select("user_id, session_string").neq("session_string", None).execute()
        data = getattr(res, "data", [])
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("session_string"):
                    await start_userbot(row["user_id"], bot)
    except Exception as e:
        logger.debug("start_all_userbots DB query failed: %s", e)

    for fname in os.listdir(_session_dir):
        if fname.endswith(".session_str"):
            try:
                admin_id = int(fname.replace("admin_", "").replace(".session_str", ""))
                if admin_id not in _clients:
                    await start_userbot(admin_id, bot)
            except ValueError:
                continue


async def stop_all_userbots() -> None:
    for admin_id in list(_clients.keys()):
        await stop_userbot(admin_id)
