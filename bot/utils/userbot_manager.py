"""
bot/utils/userbot_manager.py
Manages dynamic Telethon userbot sessions running in the background.

Performance architecture (v3 — real-time):
  ┌──────────────────────────────────────────────────────────────────────┐
  │  ROOT CAUSES FIXED:                                                  │
  │  1. Supabase sync client BLOCKS the asyncio event loop — every DB   │
  │     call (get_targets, get_filters) freezes Telethon for 50-200ms,  │
  │     preventing it from receiving subsequent messages in real time.   │
  │  2. Entity resolution on every send_message() adds latency.         │
  │  3. No connection health monitoring — silent disconnects go          │
  │     undetected, causing batch delivery when reconnected.            │
  │  4. events.NewMessage without chats= filter processes ALL updates.  │
  └──────────────────────────────────────────────────────────────────────┘

  Solution:
  • In-memory cache with background refresh (DB is NEVER called in the
    hot path — zero event-loop blocking).
  • Entity pre-resolution at startup.
  • chats= filter on event handler for efficient event dispatch.
  • asyncio.create_task() so each message processes independently.
  • Telethon fast path with 10s timeout + Bot API fallback.
  • Connection health monitor (periodic ping every 2 minutes).
  • Sync Supabase calls wrapped in run_in_executor when cache misses.
"""

from __future__ import annotations

import logging
import os
import re
import time
import asyncio
from datetime import datetime, timezone
from functools import partial

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
_tasks:   dict[int, list] = {}          # admin_id -> list of background asyncio tasks
_session_dir = os.path.expanduser("~/.tg_bot_sessions")
os.makedirs(_session_dir, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache — DB is NEVER called in the message-handling hot path.
# Sync Supabase calls are offloaded to a thread via run_in_executor
# so they never block the Telethon event loop.
# ─────────────────────────────────────────────────────────────────────────────
_target_cache: dict[int, dict] = {}   # admin_id -> {"data": list, "ts": float}
_filter_cache: dict[int, dict] = {}   # admin_id -> {"data": list, "ts": float}
_CACHE_TTL = 300  # 5 minutes

from collections import OrderedDict
_processed_messages: dict[int, OrderedDict] = {} # admin_id -> OrderedDict(msg_id -> bool)
_PROCESSED_MAX_SIZE = 100


async def _get_cached_targets(admin_id: int) -> list[dict]:
    """Return target channels from cache, refreshing in a thread if stale."""
    cached = _target_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    # Offload the synchronous Supabase call to a thread
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, channels_db.get_target_channels, admin_id)
    _target_cache[admin_id] = {"data": data, "ts": time.time()}
    return data


async def _get_cached_filters(admin_id: int) -> list[dict]:
    """Return filter rules from cache, refreshing in a thread if stale."""
    cached = _filter_cache.get(admin_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    from bot.db.filters import get_filters
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, get_filters, admin_id)
    _filter_cache[admin_id] = {"data": data, "ts": time.time()}
    return data


def invalidate_cache(admin_id: int) -> None:
    """Call when an admin changes targets, filters, or source channel."""
    _target_cache.pop(admin_id, None)
    _filter_cache.pop(admin_id, None)


def _adjust_entities_for_replacement(
    text: str,
    entities: list,
    pattern_or_find: str | re.Pattern,
    replace_str: str,
    is_regex: bool = False,
) -> tuple[str, list]:
    """
    Replaces pattern_or_find with replace_str in text and adjusts entity offsets and lengths.
    Handles multiple matches of pattern_or_find.
    """
    from telethon import types
    matches = []
    if is_regex:
        if isinstance(pattern_or_find, str):
            regex = re.compile(pattern_or_find)
        else:
            regex = pattern_or_find
        for match in regex.finditer(text):
            matches.append((match.start(), match.end()))
    else:
        find_len = len(pattern_or_find)
        if find_len > 0:
            start = 0
            while True:
                pos = text.find(pattern_or_find, start)
                if pos == -1:
                    break
                matches.append((pos, pos + find_len))
                start = pos + find_len

    if not matches:
        return text, entities

    current_entities = []
    for e in entities:
        if isinstance(e, types.MessageEntityBold):
            current_entities.append(types.MessageEntityBold(e.offset, e.length))
        elif isinstance(e, types.MessageEntityItalic):
            current_entities.append(types.MessageEntityItalic(e.offset, e.length))
        elif isinstance(e, types.MessageEntityCode):
            current_entities.append(types.MessageEntityCode(e.offset, e.length))
        elif isinstance(e, types.MessageEntityCustomEmoji):
            current_entities.append(types.MessageEntityCustomEmoji(e.offset, e.length, e.document_id))
        elif isinstance(e, types.MessageEntityTextUrl):
            current_entities.append(types.MessageEntityTextUrl(e.offset, e.length, e.url))
        elif isinstance(e, types.MessageEntityMention):
            current_entities.append(types.MessageEntityMention(e.offset, e.length))
        elif isinstance(e, types.MessageEntityUrl):
            current_entities.append(types.MessageEntityUrl(e.offset, e.length))
        else:
            try:
                current_entities.append(e.__class__(e.offset, e.length))
            except Exception:
                current_entities.append(e)

    matches.sort(key=lambda x: x[0])

    adjusted_entities = []
    for entity in current_entities:
        e_start = entity.offset
        e_end = entity.offset + entity.length

        new_e_start = e_start
        new_e_end = e_end

        for start, end in matches:
            diff = len(replace_str) - (end - start)

            if end <= e_start:
                new_e_start += diff
            elif start <= e_start < end:
                new_e_start += (start - e_start)

            if end <= e_end:
                new_e_end += diff
            elif start <= e_end < end:
                new_e_end += (start + len(replace_str) - e_end)

        new_len = new_e_end - new_e_start
        if new_len > 0:
            entity.offset = new_e_start
            entity.length = new_len
            adjusted_entities.append(entity)

    new_text_parts = []
    last_end = 0
    for start, end in matches:
        new_text_parts.append(text[last_end:start])
        new_text_parts.append(replace_str)
        last_end = end
    new_text_parts.append(text[last_end:])
    new_text = "".join(new_text_parts)

    return new_text, adjusted_entities


def _apply_cached_filters_with_entities(
    rules: list[dict],
    text: str | None,
    entities: list | None,
) -> tuple[str | None, list]:
    """Apply filter rules on text and adjust entity offsets simultaneously."""
    if text is None:
        return None, []

    if entities is None:
        entities = []

    # Block rules first
    for rule in rules:
        if rule["find_text"] == "<BLOCK>":
            if rule["replace_text"].lower() in text.lower():
                return None, []  # Signal to block this message

    # Regex wildcard rules
    for rule in rules:
        find = rule["find_text"]
        replace = rule["replace_text"]
        if find == "<ALL_LINKS>":
            text, entities = _adjust_entities_for_replacement(
                text, entities, r'https?://\S+|www\.\S+|t\.me/\S+', replace, is_regex=True
            )
        elif find == "<ALL_USERNAMES>":
            text, entities = _adjust_entities_for_replacement(
                text, entities, r'@[a-zA-Z0-9_]+', replace, is_regex=True
            )

    # Exact match rules
    for rule in rules:
        find = rule["find_text"]
        if find not in ("<ALL_LINKS>", "<ALL_USERNAMES>", "<BLOCK>", "<BLOCK_APK>"):
            text, entities = _adjust_entities_for_replacement(
                text, entities, find, rule["replace_text"], is_regex=False
            )

    return text, entities


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
# Resolved entity cache — avoids slow get_input_entity on every send
from typing import Any
_resolved_entities: dict[int, Any] = {}  # channel_id -> InputPeer


async def _resolve_target(client: TelegramClient, channel_id: int) -> bool:
    """Pre-resolve a target entity and cache it. Returns True on success."""
    if channel_id in _resolved_entities:
        return True
    try:
        entity = await client.get_entity(channel_id)
        _resolved_entities[channel_id] = entity
        return True
    except Exception as e:
        logger.warning("Failed to pre-resolve target %s: %s", channel_id, e)
        return False


async def _get_target_entity(client: TelegramClient, target_id: int) -> Any:
    """Retrieve target entity from cache, or resolve it dynamically on demand."""
    entity = _resolved_entities.get(target_id)
    if entity is not None:
        return entity
    try:
        entity = await client.get_entity(target_id)
        _resolved_entities[target_id] = entity
        return entity
    except Exception as e:
        logger.warning("Failed to dynamically resolve target entity %s: %s", target_id, e)
        return target_id


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
            catch_up=False,  # CRITICAL: prevents batch replay of missed messages
        )
        await client.connect()

        if not await client.is_user_authorized():
            logger.warning("Session unauthorized for admin %s. Removing.", admin_id)
            await client.disconnect()
            remove_session(admin_id)
            return None

        source = channels_db.get_source_channel(admin_id)
        if source:
            source_id = source["channel_id"]
            source_username = source.get("channel_username")
            
            # If it's a public channel and we have the username, ensure we are joined
            # because Telegram does not push NewMessage events to non-members of public channels!
            if source_username:
                try:
                    from telethon.tl.functions.channels import JoinChannelRequest
                    await client(JoinChannelRequest(source_username.replace("@", "")))
                    logger.info("Auto-joined public source channel %s for admin %s", source_username, admin_id)
                except Exception as e:
                    logger.debug("Auto-join skipped/failed for %s: %s", source_username, e)

            # Validate source channel access
            try:
                await client.get_input_entity(source_id)
            except Exception:
                logger.error("Admin %s userbot cannot access source channel %s", admin_id, source_id)
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text="⚠️ *Userbot Access Error*\n\nYour userbot account is **not a member** of your configured source channel. It cannot forward messages until you add it to the channel.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            # Pre-warm caches so the first message doesn't hit DB
            loop = asyncio.get_running_loop()
            targets = await loop.run_in_executor(None, channels_db.get_target_channels, admin_id)
            _target_cache[admin_id] = {"data": targets, "ts": time.time()}

            from bot.db.filters import get_filters
            rules = await loop.run_in_executor(None, get_filters, admin_id)
            _filter_cache[admin_id] = {"data": rules, "ts": time.time()}

            # Pre-resolve target entities so send_message doesn't need to resolve each time
            for t in targets:
                tid = t["channel_id"]
                ok = await _resolve_target(client, tid)
                if ok:
                    logger.info("  ✓ Pre-resolved target %s for admin %s", tid, admin_id)
                else:
                    logger.warning("  ✗ Cannot pre-resolve target %s for admin %s (will use Bot API fallback)", tid, admin_id)

            # Register event handler with chats= filter for efficient dispatch
            _register_forwarding_listener(client, admin_id, source_id, bot)
            logger.info("Userbot listening on source %s for admin %s", source_id, admin_id)

            # Start connection health monitor and fallback poller — track for clean shutdown
            t1 = asyncio.create_task(_connection_monitor(client, admin_id, bot))
            t2 = asyncio.create_task(_channel_poller(client, bot, admin_id, source_id))
            _tasks.setdefault(admin_id, []).extend([t1, t2])

        _clients[admin_id] = client
        return client

    except Exception as exc:
        error_str = str(exc)
        # Detect permanently invalidated session (used from 2 IPs simultaneously)
        if "authorization key" in error_str.lower() or "AuthKeyDuplicated" in error_str:
            logger.error(
                "DEAD SESSION for admin %s — session was used from two IPs. "
                "Auto-removing and alerting admin to re-authorize.", admin_id
            )
            # Wipe the dead session so the bot doesn't keep retrying it
            remove_session(admin_id)
            # Alert the admin via Telegram so they know to re-link
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🔴 *Session Expired — Action Required*\n\n"
                        "Your Telegram account session was invalidated because it was used "
                        "from two different locations at the same time.\n\n"
                        "The bot has automatically removed the old session.\n\n"
                        "➡️ Please send /authorize to re-link your account and resume forwarding."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        else:
            logger.error("Failed to start userbot for admin %s: %s", admin_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Connection health monitor — detects silent disconnects
# ─────────────────────────────────────────────────────────────────────────────

async def _connection_monitor(client: TelegramClient, admin_id: int, bot) -> None:
    """Periodically verify the Telethon connection is alive."""
    while admin_id in _clients and _clients[admin_id] is client:
        await asyncio.sleep(120)  # Check every 2 minutes
        try:
            if not client.is_connected():
                logger.warning("⚠️ Telethon disconnected for admin %s — reconnecting...", admin_id)
                await client.connect()
                if await client.is_user_authorized():
                    logger.info("✅ Telethon reconnected for admin %s", admin_id)
                else:
                    logger.error("❌ Session revoked for admin %s after reconnect", admin_id)
                    break
            else:
                # Ping to verify the connection is truly alive (not just TCP-open)
                await client.get_me()
        except Exception as e:
            logger.error("Connection monitor error for admin %s: %s", admin_id, e)


async def _channel_poller(client: TelegramClient, bot, admin_id: int, source_channel_id: int) -> None:
    """
    Background fallback poller. Telegram often drops push events for large public channels.
    This manually fetches the latest messages every 15s to catch anything the listener missed.
    """
    from telethon.errors import FloodWaitError
    await asyncio.sleep(5)  # Let event loop stabilize on startup
    while admin_id in _clients and _clients[admin_id] is client:
        try:
            # Fetch latest 5 messages
            async for message in client.iter_messages(source_channel_id, limit=5):
                # Check if already processed
                if admin_id in _processed_messages and message.id in _processed_messages[admin_id]:
                    continue
                    
                # Construct a dummy event object for _process_and_forward
                class DummyEvent:
                    pass
                event = DummyEvent()
                event.message = message
                event.chat_id = source_channel_id
                
                logger.info("Poller caught missed message %s for admin %s", message.id, admin_id)
                asyncio.create_task(_process_and_forward(client, bot, event, admin_id))
                
            await asyncio.sleep(15)
        except FloodWaitError as e:
            logger.warning("Poller flood wait for admin %s. Sleeping %s seconds", admin_id, e.seconds)
            await asyncio.sleep(e.seconds)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug("Poller error for admin %s: %s", admin_id, e)
            await asyncio.sleep(15)


# ─────────────────────────────────────────────────────────────────────────────
# Forwarding engine (v3) — real-time, non-blocking, zero event-loop blocking
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_id(cid) -> int:
    """Strip the -100 prefix so Telethon and DB channel IDs can be compared."""
    s = str(cid)
    if s.startswith("-100"): return int(s[4:])
    if s.startswith("-"):    return int(s[1:])
    return int(s)


def _register_forwarding_listener(
    client: TelegramClient,
    admin_id: int,
    source_channel_id: int,
    bot,
) -> None:
    """
    Register a Telethon event handler. We use a manual ID check because
    Telethon's chats= filter can fail silently if the -100 prefix mismatches.
    Each message is dispatched to a background task immediately.
    """

    @client.on(events.NewMessage())
    async def handler(event):
        incoming_id = _normalize_id(event.chat_id)
        expected_id = _normalize_id(source_channel_id)
        
        # Log every single incoming message to debug the filter
        logger.debug(f"Received msg from {event.chat_id} (normalized: {incoming_id}). Expected source: {source_channel_id} (normalized: {expected_id})")
        
        if incoming_id != expected_id:
            return
        # Fire-and-forget: handler returns instantly, forwarding runs in background
        asyncio.create_task(_process_and_forward(client, bot, event, admin_id))


async def _process_and_forward(
    client: TelegramClient, bot, event, admin_id: int,
) -> None:
    """Core forwarding pipeline for a single incoming message."""
    try:
        # ── Deduplication check (prevents double-forward from listener + poller) ──
        msg_id = getattr(event.message, "id", None)
        if msg_id is not None:
            if admin_id not in _processed_messages:
                _processed_messages[admin_id] = OrderedDict()
            if msg_id in _processed_messages[admin_id]:
                return
            _processed_messages[admin_id][msg_id] = True
            if len(_processed_messages[admin_id]) > _PROCESSED_MAX_SIZE:
                _processed_messages[admin_id].popitem(last=False)

        # ── Skip stale messages (prevents backlog flood on restart) ──────
        if event.message.date:
            now = datetime.now(timezone.utc)
            msg_date = event.message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            age = (now - msg_date).total_seconds()
            if age > 30:
                logger.warning("Skipping stale message (%.0fs old) for admin %s", age, admin_id)
                return

        # ── Get targets and filters from cache (NEVER blocks event loop) ─
        targets = await _get_cached_targets(admin_id)
        if not targets:
            return

        rules = await _get_cached_filters(admin_id)
        original_text = event.message.message or ""
        original_entities = event.message.entities or []
        filtered_text, filtered_entities = _apply_cached_filters_with_entities(
            rules, original_text, original_entities
        )

        if filtered_text is None:
            return  # Blocked by keyword filter

        # ── APK block check ──────────────────────────────────────────────
        if event.message.file and event.message.file.name:
            if event.message.file.name.lower().endswith('.apk'):
                if any(r["find_text"] == "<BLOCK_APK>" for r in rules):
                    return

        text_changed = filtered_text != original_text

        logger.info(
            "⚡ Forwarding msg from source (admin %s) → %d target(s) | media=%s | text_changed=%s",
            admin_id, len(targets), bool(event.message.media), text_changed,
        )

        # ── Phase 1: Telethon-native fast path (instant, zero bandwidth) ─
        fast_results = await asyncio.gather(
            *[_fast_forward(client, t["channel_id"], event, filtered_text, filtered_entities, text_changed)
              for t in targets],
            return_exceptions=True,
        )

        # ── Phase 2: Bot API fallback for failed targets ─────────────────
        failed_ids = []
        for i, r in enumerate(fast_results):
            if isinstance(r, Exception):
                failed_ids.append(targets[i]["channel_id"])
                logger.warning(
                    "Fast-path failed for target %s (admin %s): %s",
                    targets[i]["channel_id"], admin_id, r,
                )

        if failed_ids:
            logger.info(
                "Using Bot API fallback for %d target(s) (admin %s)",
                len(failed_ids), admin_id,
            )
            await _slow_forward(bot, event, filtered_text, failed_ids, admin_id)
        else:
            logger.info("✅ All %d target(s) forwarded via fast-path (admin %s)", len(targets), admin_id)

    except Exception as exc:
        logger.error("_process_and_forward error (admin %s): %s", admin_id, exc, exc_info=True)


async def _fast_forward(
    client: TelegramClient,
    target_id: int,
    event,
    filtered_text: str,
    entities: list,
    text_changed: bool,
) -> None:
    """
    Telethon-native message delivery.
    Sends message as:
    1. A cloned message via `client.send_message(entity, event.message)` if text did NOT change.
       This preserves premium animated stickers, custom emojis, formatting, and media perfectly
       without any "Forwarded from" header, and uses server-side references (instant).
    2. A new message (with filtered_text and aligned entities) if text DID change.
    """
    async def _send():
        entity = await _get_target_entity(client, target_id)
        
        # Scenario A: Text has NOT changed. We clone the original message object directly.
        # This keeps premium stickers, custom emojis, formatting, and file references 100% intact.
        if not text_changed:
            try:
                await client.send_message(entity, event.message)
            except Exception as exc:
                logger.info("Failed to clone message %s directly: %s. Retrying via download/upload.", event.message.id, exc)
                # Fallback: if it fails (restricted channel, media reference expired), we download and upload.
                if event.message.media:
                    temp_dir = os.path.join(os.getcwd(), "scratch", "temp_media")
                    os.makedirs(temp_dir, exist_ok=True)
                    media_path = await event.message.download_media(file=temp_dir)
                    try:
                        await client.send_file(
                            entity,
                            file=media_path,
                            caption=filtered_text or None,
                            formatting_entities=entities,
                        )
                    except Exception as upload_exc:
                        logger.error("Clone fallback upload failed for target %s: %s", target_id, upload_exc)
                        raise upload_exc
                    finally:
                        if media_path and os.path.exists(media_path):
                            try:
                                os.remove(media_path)
                            except Exception:
                                pass
                elif filtered_text:
                    await client.send_message(
                        entity,
                        message=filtered_text,
                        formatting_entities=entities,
                    )
        
        # Scenario B: Text has changed. We must send a new message with the modified text/entities.
        else:
            if event.message.media:
                try:
                    # Try reusing server-side media reference first
                    await client.send_file(
                        entity,
                        file=event.message.media,
                        caption=filtered_text or None,
                        formatting_entities=entities,
                    )
                except Exception as exc:
                    # If that fails (restricted channel, expired reference), download and upload
                    logger.info("Failed to copy media reference for %s: %s. Retrying via download/upload.", target_id, exc)
                    temp_dir = os.path.join(os.getcwd(), "scratch", "temp_media")
                    os.makedirs(temp_dir, exist_ok=True)
                    media_path = await event.message.download_media(file=temp_dir)
                    try:
                        await client.send_file(
                            entity,
                            file=media_path,
                            caption=filtered_text or None,
                            formatting_entities=entities,
                        )
                    except Exception as upload_exc:
                        logger.error("Download and upload failed for target %s: %s", target_id, upload_exc)
                        raise upload_exc
                    finally:
                        if media_path and os.path.exists(media_path):
                            try:
                                os.remove(media_path)
                            except Exception:
                                pass
            elif filtered_text:
                await client.send_message(
                    entity,
                    message=filtered_text,
                    formatting_entities=entities,
                )

    await asyncio.wait_for(_send(), timeout=30.0)



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
    # Clear pre-resolved entities for this admin's targets
    try:
        targets = channels_db.get_target_channels(admin_id)
        for t in targets:
            _resolved_entities.pop(t["channel_id"], None)
    except Exception:
        pass
    await stop_userbot(admin_id)
    await start_userbot(admin_id, bot)


async def stop_userbot(admin_id: int) -> None:
    # Cancel all background tasks first so they don't get "destroyed while pending"
    for task in _tasks.pop(admin_id, []):
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

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
