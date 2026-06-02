"""
bot/utils/userbot_manager.py
Manages dynamic Telethon userbot sessions running in the background.

Performance architecture (v2):
  • Telethon receives new messages from source channels.
  • Each message handler fires asyncio.create_task() so it never blocks
    subsequent incoming messages (prevents the "batching" effect).
  • Forwarding tries the FAST path first: Telethon-native send_message()
    which copies media using Telegram's internal file references — zero
    download, zero upload, near-instant delivery.
  • If the fast path fails (e.g. userbot lacks posting rights in a target),
    falls back to download-once + re-upload via Bot API.
  • Target channels and filter rules are cached in-memory with a 5-minute
    TTL so we never hit Supabase on the hot path.
"""

from __future__ import annotations

import logging
import os
import re
import time
import asyncio
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from bot.db import channels as channels_db
from bot.db import users as users_db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Session storage
# ─────────────────────────────────────────────────────────────────────────────
_clients: dict[int, TelegramClient] = {}
_session_dir = os.path.expanduser("~/.tg_bot_sessions")
os.makedirs(_session_dir, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache — avoids hitting Supabase on every incoming message
# ─────────────────────────────────────────────────────────────────────────────
_target_cache: dict[int, dict] = {}   # admin_id -> {"data": list, "ts": float}
_filter_cache: dict[int, dict] = {}   # admin_id -> {"data": list, "ts": float}
_CACHE_TTL = 300  # 5 minutes


def _get_cached_targets(admin_id: int) -> list[dict]:
    """Return target channels from cache, refreshing if stale."""
    cached = _target_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    data = channels_db.get_target_channels(admin_id)
    _target_cache[admin_id] = {"data": data, "ts": time.time()}
    return data


def _get_cached_filters(admin_id: int) -> list[dict]:
    """Return filter rules from cache, refreshing if stale."""
    cached = _filter_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    from bot.db.filters import get_filters
    data = get_filters(admin_id)
    _filter_cache[admin_id] = {"data": data, "ts": time.time()}
    return data


def invalidate_cache(admin_id: int) -> None:
    """Call when an admin changes targets, filters, or source channel."""
    _target_cache.pop(admin_id, None)
    _filter_cache.pop(admin_id, None)


def _apply_cached_filters(rules: list[dict], text: str | None) -> str | None:
    """Apply filter rules using pre-fetched rules (no DB call)."""
    if text is None:
        return None

    # Block rules first
    for rule in rules:
        if rule["find_text"] == "<BLOCK>":
            if rule["replace_text"].lower() in text.lower():
                return None  # Signal to block this message

    # Regex wildcard rules
    for rule in rules:
        find = rule["find_text"]
        replace = rule["replace_text"]
        if find == "<ALL_LINKS>":
            text = re.sub(r'https?://\S+|www\.\S+|t\.me/\S+', replace, text)
        elif find == "<ALL_USERNAMES>":
            text = re.sub(r'@[a-zA-Z0-9_]+', replace, text)

    # Exact match rules
    for rule in rules:
        find = rule["find_text"]
        if find not in ("<ALL_LINKS>", "<ALL_USERNAMES>", "<BLOCK>", "<BLOCK_APK>"):
            text = text.replace(find, rule["replace_text"])

    return text


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


# ─────────────────────────────────────────────────────────────────────────────
# Forwarding engine (v2) — real-time, non-blocking, zero-download fast path
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_id(cid) -> int:
    """Strip the -100 prefix so Telethon and DB channel IDs can be compared."""
    s = str(cid)
    if s.startswith("-100"): return int(s[4:])
    if s.startswith("-"):    return int(s[1:])
    return int(s)


def _register_forwarding_listener(client: TelegramClient, admin_id: int, source_channel_id: int, bot) -> None:
    """
    Register a Telethon event handler that dispatches each incoming message
    to a background task immediately, so it never blocks the next message.
    """

    @client.on(events.NewMessage)
    async def handler(event):
        if _normalize_id(event.chat_id) != _normalize_id(source_channel_id):
            return
        # Fire-and-forget: handler returns instantly, processing runs in background
        asyncio.create_task(_process_and_forward(client, bot, event, admin_id))


async def _process_and_forward(client: TelegramClient, bot, event, admin_id: int) -> None:
    """Core forwarding pipeline for a single incoming message."""
    try:
        # ── Skip stale messages (prevents backlog flood on restart) ──────
        if event.message.date:
            now = datetime.now(timezone.utc)
            msg_date = event.message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if (now - msg_date).total_seconds() > 120:
                return

        # ── Get targets and filters from cache (no Supabase calls) ───────
        targets = _get_cached_targets(admin_id)
        if not targets:
            return

        rules = _get_cached_filters(admin_id)
        original_text = event.message.message or ""
        filtered_text = _apply_cached_filters(rules, original_text)

        if filtered_text is None:
            return  # Blocked by keyword filter

        # ── APK block check ──────────────────────────────────────────────
        if event.message.file and event.message.file.name:
            if event.message.file.name.lower().endswith('.apk'):
                if any(r["find_text"] == "<BLOCK_APK>" for r in rules):
                    return

        text_changed = filtered_text != original_text

        # ── Phase 1: Telethon-native fast path (instant, zero bandwidth) ─
        fast_results = await asyncio.gather(
            *[_fast_forward(client, t["channel_id"], event, filtered_text, text_changed)
              for t in targets],
            return_exceptions=True,
        )

        # ── Phase 2: Bot API fallback for failed targets ─────────────────
        failed_ids = [
            targets[i]["channel_id"]
            for i, r in enumerate(fast_results)
            if isinstance(r, Exception)
        ]
        if failed_ids:
            logger.info(
                "Telethon fast-path failed for %d target(s) (admin %s), using Bot API fallback",
                len(failed_ids), admin_id,
            )
            await _slow_forward(bot, event, filtered_text, failed_ids, admin_id)

    except Exception as exc:
        logger.error("_process_and_forward error (admin %s): %s", admin_id, exc)


async def _fast_forward(
    client: TelegramClient,
    target_id: int,
    event,
    filtered_text: str,
    text_changed: bool,
) -> None:
    """
    Telethon-native message copy — uses Telegram's internal file references.
    No file download, no re-upload, near-instant delivery.
    Raises on failure so the caller can fall back to Bot API.
    """
    if not text_changed:
        # Copy the message exactly as-is (preserves media, formatting, stickers, etc.)
        await client.send_message(target_id, event.message)
    elif event.message.media:
        # Modified text + original media via internal file reference
        await client.send_message(target_id, message=filtered_text, file=event.message.media)
    elif filtered_text:
        # Pure text with modifications
        await client.send_message(target_id, filtered_text)


async def _slow_forward(
    bot,
    event,
    filtered_text: str,
    target_ids: list[int],
    admin_id: int,
) -> None:
    """
    Fallback: download media ONCE, then re-upload via Bot API to all
    failed targets. Only used when Telethon-native forwarding fails
    (e.g. userbot doesn't have posting rights in the target channel).
    """
    media_path = None
    if event.message.media:
        try:
            temp_dir = os.path.join(os.getcwd(), "scratch", "temp_media")
            os.makedirs(temp_dir, exist_ok=True)
            media_path = await event.message.download_media(file=temp_dir)
        except Exception as e:
            logger.error("Media download failed (admin %s): %s", admin_id, e)

    try:
        for target_id in target_ids:
            try:
                if media_path and os.path.exists(media_path):
                    caption = filtered_text or None
                    if event.message.sticker or media_path.lower().endswith(('.webm', '.tgs', '.webp')):
                        try:
                            with open(media_path, "rb") as f:
                                await bot.send_sticker(chat_id=target_id, sticker=f)
                        except Exception:
                            logger.warning("Dropped invalid sticker for admin %s", admin_id)
                    elif event.message.photo:
                        with open(media_path, "rb") as f:
                            await bot.send_photo(chat_id=target_id, photo=f, caption=caption)
                    elif event.message.video:
                        with open(media_path, "rb") as f:
                            await bot.send_video(chat_id=target_id, video=f, caption=caption)
                    elif event.message.audio:
                        with open(media_path, "rb") as f:
                            await bot.send_audio(chat_id=target_id, audio=f, caption=caption)
                    elif event.message.voice:
                        with open(media_path, "rb") as f:
                            await bot.send_voice(chat_id=target_id, voice=f, caption=caption)
                    elif event.message.video_note:
                        with open(media_path, "rb") as f:
                            await bot.send_video_note(chat_id=target_id, video_note=f)
                    else:
                        with open(media_path, "rb") as f:
                            await bot.send_document(chat_id=target_id, document=f, caption=caption)
                elif filtered_text:
                    await bot.send_message(chat_id=target_id, text=filtered_text)
            except Exception as exc:
                logger.error("Bot API forward to %s failed (admin %s): %s", target_id, admin_id, exc)
    finally:
        if media_path and os.path.exists(media_path):
            try:
                os.remove(media_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle helpers
# ─────────────────────────────────────────────────────────────────────────────

async def restart_userbot_listener(admin_id: int, bot) -> None:
    invalidate_cache(admin_id)
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
