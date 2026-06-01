"""
bot/handlers/admin.py
Admin-role commands:
  /authorize   — link Telegram account via OTP (required first step)
  /addsource   — set source channel (private or public)
  /addtarget   — add a target channel (multiple allowed)
  /removesource
  /removetarget
  /filter      — add find→replace text filter (links, usernames, any text)
  /myfilters   — list and remove filters
  /mystatus    — show subscription + auth status
  /schedule    — create a scheduled message
  /removeschedule
"""

from __future__ import annotations

import logging
import asyncio
import re
import warnings
from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    FloodWaitError,
)

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from bot.db import channels as channels_db
from bot.db import filters as filters_db
from bot.db import schedules as schedules_db
from bot.db import users as users_db
from bot.utils.roles import require_admin
from bot.utils.scheduler import remove_scheduled_job, schedule_message
from bot.utils import userbot_manager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation states
# ─────────────────────────────────────────────────────────────────────────────
(
    ADD_SOURCE_WAIT,
    ADD_TARGET_WAIT,
    FILTER_TYPE_WAIT,
    FILTER_FIND_WAIT,
    FILTER_REPLACE_WAIT,
    SCHED_CONTENT_WAIT,
    SCHED_TIME_WAIT,
    SCHED_FREQ_WAIT,
    AUTH_PHONE_WAIT,
    AUTH_CODE_WAIT,
    AUTH_2FA_WAIT,
) = range(11)

_CTX_FIND = "filter_find_text"
_CTX_SCHED_CONTENT = "sched_content"
_CTX_SCHED_TIME = "sched_time"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_schedule_time(time_str: str) -> str | None:
    """Parse time string like '14:30', '12:00 PM', '12:00PM IND' into 'HH:MM' UTC."""
    pattern = re.compile(r"^\s*(\d{1,2}):(\d{2})(?:\s*(am|pm))?(?:\s*(utc|ind|ist))?\s*$", re.IGNORECASE)
    match = pattern.match(time_str)
    if not match:
        return None
    
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = (match.group(3) or "").upper()
    tz = (match.group(4) or "").upper()
    
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
        
    if hour > 23 or minute > 59:
        return None
        
    dt = datetime(2000, 1, 1, hour, minute)
    
    if tz in ("IND", "IST"):
        dt = dt - timedelta(hours=5, minutes=30)
        
    return dt.strftime("%H:%M")


async def _resolve_channel(bot, identifier: str) -> tuple[int | None, str | None]:
    """
    Resolve a channel identifier (username, t.me link, or numeric ID) to (channel_id, username).
    Returns (None, None) on failure.
    """
    identifier = identifier.strip()

    if re.match(r"^-?\d+$", identifier):
        channel_id = int(identifier)
        try:
            chat = await bot.get_chat(channel_id)
            return chat.id, chat.username
        except Exception:
            return None, None

    username = identifier
    if "t.me/" in identifier:
        username = identifier.split("t.me/")[-1].split("/")[0]
    username = username.lstrip("@")

    try:
        chat = await bot.get_chat(f"@{username}")
        return chat.id, chat.username
    except Exception:
        return None, None


async def _cleanup_auth(context: ContextTypes.DEFAULT_TYPE, admin_id: int) -> None:
    """Disconnect and clean up any pending auth client."""
    client = context.user_data.pop("auth_client", None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    context.user_data.pop("auth_phone", None)
    context.user_data.pop("auth_code_hash", None)


# ─────────────────────────────────────────────────────────────────────────────
# /authorize — OTP login
# ─────────────────────────────────────────────────────────────────────────────

async def authorize_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        await update.message.reply_text(
            "❌ *Bot Not Configured*\n\n"
            "`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are missing from the server `.env` file.\n"
            "Please ask the Super Admin to add them.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if userbot_manager.is_userbot_authorized(admin_id):
        await update.message.reply_text(
            "✅ *Already Authorized!*\n\n"
            "Your Telegram account is already linked and actively monitoring your source channel.\n\n"
            "Use /addsource to set a source channel or /addtarget to add target channels.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 *Link Your Telegram Account*\n\n"
        "To monitor private and public channels, I need to link your personal Telegram account.\n\n"
        "📱 *Step 1 of 2:* Send your phone number in international format:\n"
        "`+91xxxxxxxxxx` or `+1xxxxxxxxxx`\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return AUTH_PHONE_WAIT



async def auth_phone_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    admin_id = update.message.from_user.id

    if not re.match(r"^\+\d{7,15}$", phone):
        await update.message.reply_text(
            "❌ Invalid format. Please send your phone number with country code:\n"
            "`+91xxxxxxxxxx`\n\nTry again or /cancel.",
            parse_mode="Markdown",
        )
        return AUTH_PHONE_WAIT

    connecting_msg = await update.message.reply_text("⏳ Connecting to Telegram...")

    try:
        userbot_manager.clear_pending_auth(admin_id)
        userbot_manager.remove_session(admin_id)

        # connect and pre-switch DC
        from telethon.errors import FloodWaitError
        client = await userbot_manager.prepare_auth_client(phone)

        sent = await client.send_code_request(phone)

        # Store exactly the same client that sent the request
        userbot_manager.set_pending_auth(admin_id, client, phone, sent.phone_code_hash)

        await connecting_msg.edit_text(
            "📨 *Verification Code Sent!*\n\n"
            "Enter the code Telegram sent to your app or SMS.\n\n"
            "🚨 *CRITICAL:* Do NOT send the code as plain digits! Telegram will detect it as a phishing attempt and expire the code immediately.\n\n"
            "✅ Send it with hyphens between each number, e.g., `1-2-3-4-5`.\n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return AUTH_CODE_WAIT

    except FloodWaitError as e:
        await connecting_msg.edit_text(
            f"⏳ *Telegram Rate Limit*\n\n"
            f"Too many login attempts. Please wait *{e.seconds} seconds* and try /authorize again.",
            parse_mode="Markdown",
        )
        userbot_manager.clear_pending_auth(admin_id)
        return ConversationHandler.END

    except Exception as exc:
        logger.error("Failed to send code for admin %s: %s", admin_id, exc)
        await connecting_msg.edit_text(
            f"❌ *Failed to send verification code*\n\n`{exc}`\n\n"
            "Please check your phone number and try /authorize again.",
            parse_mode="Markdown",
        )
        userbot_manager.clear_pending_auth(admin_id)
        return ConversationHandler.END


async def auth_code_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    code = re.sub(r"\D", "", raw)
    admin_id = update.message.from_user.id

    pending = userbot_manager.get_pending_auth(admin_id)
    if not pending:
        await update.message.reply_text("❌ Session expired. Please run /authorize again.")
        return ConversationHandler.END

    client = pending["client"]
    phone = pending["phone"]
    code_hash = pending["code_hash"]

    if not code or len(code) < 4:
        await update.message.reply_text(
            "❌ That doesn't look like a valid code. Please send the numeric code from Telegram.\n\n"
            "Try again or /cancel.",
        )
        return AUTH_CODE_WAIT

    try:
        from telethon.errors import (
            PhoneCodeInvalidError, PhoneCodeExpiredError, SessionPasswordNeededError
        )

        logger.info("Attempting sign_in for admin %s with code hash %s", admin_id, code_hash[:8])
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)

        # Success
        session_string = client.session.save()
        userbot_manager.save_session_string(admin_id, session_string)
        userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            "✅ *Authorization Successful!*\n\n"
            "Your Telegram account is now linked.\n\n"
            "👉 Please click /start to see all features and commands!",
            parse_mode="Markdown"
        )
        await userbot_manager.start_userbot(admin_id, context.bot)
        return ConversationHandler.END

    except PhoneCodeInvalidError:
        logger.warning("Wrong code entered by admin %s", admin_id)
        await update.message.reply_text(
            "❌ *Wrong Code*\n\n"
            "That code is incorrect. Please check and try again, or /cancel.",
            parse_mode="Markdown",
        )
        return AUTH_CODE_WAIT

    except PhoneCodeExpiredError:
        logger.warning("Code expired for admin %s — code_hash=%s", admin_id, code_hash[:8])
        userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            "⏰ *Code Expired*\n\n"
            "The verification code has expired (Telegram's server rejected it).\n"
            "Run /authorize again to get a fresh code.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 *Two-Step Verification Required*\n\n"
            "Your account has a 2FA password enabled.\n"
            "Please send your *2FA password* now:\n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return AUTH_2FA_WAIT

    except Exception as e:
        logger.error("sign_in failed: %s | type: %s", e, type(e).__name__)
        userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            f"❌ *Login Failed*\n\n`{type(e).__name__}: {e}`\n\nRun /authorize again to try with a fresh code.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END


async def auth_2fa_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    admin_id = update.message.from_user.id

    pending = userbot_manager.get_pending_auth(admin_id)
    if not pending:
        await update.message.reply_text("❌ Session expired. Please run /authorize again.")
        return ConversationHandler.END

    client = pending["client"]

    try:
        await client.sign_in(password=password)

        session_string = client.session.save()
        userbot_manager.save_session_string(admin_id, session_string)
        userbot_manager.clear_pending_auth(admin_id)
        await update.message.reply_text(
            "🎉 *Authorization Successful!* (2FA)\n\n"
            "Your Telegram account is now linked.\n\n"
            "📡 /addsource — Set a source channel\n"
            "🎯 /addtarget — Add target channels\n"
            "🔧 /filter — Set text filters",
            parse_mode="Markdown",
        )
        await userbot_manager.start_userbot(admin_id, context.bot)
        return ConversationHandler.END

    except Exception as exc:
        logger.error("2FA sign-in failed for admin %s: %s", admin_id, exc)
        await update.message.reply_text(
            f"❌ *Wrong 2FA Password*\n\n`{exc}`\n\nTry again or /cancel.",
            parse_mode="Markdown",
        )
        return AUTH_2FA_WAIT


async def auth_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    userbot_manager.clear_pending_auth(admin_id)
    await update.message.reply_text("❌ Authorization cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Guard helper — use in every command that needs auth
# ─────────────────────────────────────────────────────────────────────────────

async def _check_authorized(update: Update, admin_id: int) -> bool:
    """Reply with prompt and return False if userbot not authorized."""
    if not userbot_manager.is_userbot_authorized(admin_id):
        await update.message.reply_text(
            "🔑 *Authorization Required*\n\n"
            "Run /authorize first to link your Telegram account before using this feature.",
            parse_mode="Markdown",
        )
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# /addsource
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def addsource_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    existing = channels_db.get_source_channel(admin_id)
    existing_note = ""
    if existing:
        display = f"@{existing['channel_username']}" if existing.get("channel_username") else str(existing["channel_id"])
        existing_note = f"\n\n📌 Current source: `{display}` _(will be replaced)_"

    await update.message.reply_text(
        "📡 *Set Source Channel*\n\n"
        "Send the source channel as:\n"
        "• `@username` or `https://t.me/username`\n"
        "• Numeric ID: `-1001234567890`\n"
        "• Or *forward any message* from the channel\n\n"
        "✅ Works for both *public* and *private* channels "
        "(your linked account must be a member of private ones)."
        f"{existing_note}\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return ADD_SOURCE_WAIT


async def addsource_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    admin_id = msg.from_user.id

    # Try forwarded message first
    channel_id, username = None, None
    if msg.forward_origin:
        origin = msg.forward_origin
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat:
            channel_id = fwd_chat.id
            username = fwd_chat.username

    if channel_id is None:
        text = msg.text or ""
        channel_id, username = await _resolve_channel(context.bot, text)

    if channel_id is None:
        # Try via userbot (for private channels the bot can't see)
        client = userbot_manager._clients.get(admin_id)
        if client and msg.text:
            try:
                entity = await client.get_entity(msg.text.strip())
                channel_id = entity.id
                username = getattr(entity, "username", None)
                # Normalize to negative format
                if channel_id > 0:
                    channel_id = int(f"-100{channel_id}")
            except Exception:
                pass

    if channel_id is None:
        await msg.reply_text(
            "❌ Couldn't find that channel.\n\n"
            "Make sure:\n"
            "• The channel username/link is correct\n"
            "• For private channels: your linked account is a member\n\n"
            "Try again or /cancel.",
        )
        return ADD_SOURCE_WAIT

    channels_db.set_source_channel(admin_id, channel_id, username)
    await userbot_manager.restart_userbot_listener(admin_id, context.bot)

    display = f"@{username}" if username else str(channel_id)
    await msg.reply_text(
        f"✅ *Source channel set!*\n\n"
        f"📡 Now monitoring: `{display}`\n"
        f"ID: `{channel_id}`\n\n"
        f"The bot will forward new messages from this channel to your target channels.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /addtarget
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def addtarget_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    targets = channels_db.get_target_channels(admin_id)
    count = len(targets)
    count_note = f"\n\nYou currently have *{count}* target channel(s)." if count else ""

    await update.message.reply_text(
        "🎯 *Add Target Channel*\n\n"
        "Send the target channel as:\n"
        "• `@username` or `https://t.me/username`\n"
        "• Numeric ID: `-1001234567890`\n"
        "• Or *forward any message* from the channel\n\n"
        "⚠️ The bot must be an *admin* of the target channel to post messages."
        f"{count_note}\n\n"
        "You can add multiple targets — just run /addtarget again.\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
    )
    return ADD_TARGET_WAIT


async def addtarget_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    admin_id = msg.from_user.id

    channel_id, username = None, None
    if msg.forward_origin:
        origin = msg.forward_origin
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat:
            channel_id = fwd_chat.id
            username = fwd_chat.username

    if channel_id is None:
        text = msg.text or ""
        channel_id, username = await _resolve_channel(context.bot, text)

    if channel_id is None:
        await msg.reply_text(
            "❌ Couldn't find that channel.\n\n"
            "Make sure the bot is an *admin* of the target channel, then try again or /cancel.",
        )
        return ADD_TARGET_WAIT

    added = channels_db.add_target_channel(admin_id, channel_id, username)
    display = f"@{username}" if username else str(channel_id)

    if not added:
        await msg.reply_text(
            f"ℹ️ `{display}` is already in your target list.",
            parse_mode="Markdown",
        )
    else:
        targets = channels_db.get_target_channels(admin_id)
        await msg.reply_text(
            f"✅ *Target channel added!*\n\n"
            f"🎯 `{display}` (ID: `{channel_id}`)\n\n"
            f"Total targets: *{len(targets)}*\n\n"
            f"Run /addtarget again to add more.",
            parse_mode="Markdown",
        )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /removesource
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def removesource_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    source = channels_db.get_source_channel(admin_id)
    if not source:
        await update.message.reply_text("📭 You don't have a source channel set.")
        return

    display = f"@{source['channel_username']}" if source.get("channel_username") else str(source["channel_id"])
    channels_db.remove_source_channel(admin_id)
    await userbot_manager.stop_userbot(admin_id)
    await update.message.reply_text(
        f"✅ Source channel `{display}` removed.\n"
        f"The background listener has been stopped.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /removetarget
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def removetarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    targets = channels_db.get_target_channels(admin_id)
    if not targets:
        await update.message.reply_text("📭 You have no target channels set. Use /addtarget to add one.")
        return

    text = "🎯 *Your Target Channels*\n\nTap a button to remove it:\n\n"
    keyboard = []
    for i, t in enumerate(targets, 1):
        display = f"@{t['channel_username']}" if t.get("channel_username") else str(t["channel_id"])
        text += f"{i}. `{display}`\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 Remove {display}", callback_data=f"rmtarget:{t['channel_id']}")
        ])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def removetarget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    channel_id = int(query.data.split(":", 1)[1])

    deleted = channels_db.remove_target_channel(admin_id, channel_id)
    if deleted:
        targets = channels_db.get_target_channels(admin_id)
        await query.edit_message_text(
            f"✅ Target channel removed.\n"
            f"Remaining targets: *{len(targets)}*",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("❌ Channel not found (already removed?).")


# ─────────────────────────────────────────────────────────────────────────────
# /filter — add find→replace rule
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🔗 Replace Any Link", callback_data="ftype:all_links")],
        [InlineKeyboardButton("👤 Replace Any Username", callback_data="ftype:all_usernames")],
        [InlineKeyboardButton("🎯 Replace Specific Text/Link", callback_data="ftype:specific")],
        [InlineKeyboardButton("🚫 Block Message by Keyword", callback_data="ftype:block")]
    ]
    
    await update.message.reply_text(
        "🔧 *Add Text Filter*\n\n"
        "Filters automatically replace text, links, or usernames in messages before forwarding.\n\n"
        "What would you like to do?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return FILTER_TYPE_WAIT

async def filter_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ftype = query.data.split(":")[1]
    
    if ftype == "all_links":
        context.user_data[_CTX_FIND] = "<ALL_LINKS>"
        await query.edit_message_text(
            "🔗 *Replace Any Link*\n\n"
            "Send the *default link* you want to replace ALL links with.\n"
            "_(Send a single space or leave blank to delete occurrences.)_\n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return FILTER_REPLACE_WAIT
        
    elif ftype == "all_usernames":
        context.user_data[_CTX_FIND] = "<ALL_USERNAMES>"
        await query.edit_message_text(
            "👤 *Replace Any Username*\n\n"
            "Send the *default username* you want to replace ALL usernames with.\n"
            "_(Send a single space or leave blank to delete occurrences.)_\n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return FILTER_REPLACE_WAIT
        
    elif ftype == "block":
        context.user_data[_CTX_FIND] = "<BLOCK>"
        await query.edit_message_text(
            "🚫 *Block Message by Keyword*\n\n"
            "Send the *keyword*. If a message contains this keyword, it will NOT be forwarded.\n"
            "_(Case-insensitive match)_ \n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return FILTER_REPLACE_WAIT

    else:
        await query.edit_message_text(
            "🎯 *Replace Specific Text/Link/Username*\n\n"
            "📝 *Step 1:* Send the exact text/link/username you want to *find*.\n\n"
            "Type /cancel to abort.",
            parse_mode="Markdown",
        )
        return FILTER_FIND_WAIT


async def filter_find_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    find_text = update.message.text.strip()
    if not find_text:
        await update.message.reply_text("❌ Find text cannot be empty. Try again or /cancel.")
        return FILTER_FIND_WAIT

    context.user_data[_CTX_FIND] = find_text
    await update.message.reply_text(
        f"✅ Find text: `{find_text}`\n\n"
        f"📝 *Step 2:* Now send the *replacement text*.\n"
        f"_(Send a single space or leave blank to delete occurrences.)_",
        parse_mode="Markdown",
    )
    return FILTER_REPLACE_WAIT


async def filter_replace_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    replace_text = update.message.text or ""
    find_text = context.user_data.pop(_CTX_FIND, None)
    admin_id = update.message.from_user.id

    if not find_text:
        await update.message.reply_text("❌ Session expired. Please start /filter again.")
        return ConversationHandler.END

    filters_db.add_filter(admin_id, find_text, replace_text.strip())

    label = f"`{replace_text.strip()}`" if replace_text.strip() else "_(deleted)_"
    display_find = "Any Link" if find_text == "<ALL_LINKS>" else "Any Username" if find_text == "<ALL_USERNAMES>" else "Block Keyword" if find_text == "<BLOCK>" else f"`{find_text}`"
    
    current = filters_db.get_filters(admin_id)
    await update.message.reply_text(
        f"✅ *Filter saved!*\n\n"
        f"Find: {display_find}\n"
        f"Replace with: {label}\n\n"
        f"Total filters: *{len(current)}* — use /myfilters to view all.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /myfilters
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def myfilters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    rule_list = filters_db.get_filters(admin_id)
    if not rule_list:
        await update.message.reply_text(
            "📭 No filters set yet.\n\nUse /filter to add a text replacement rule."
        )
        return

    text = "🔧 *Your Text Filters*\n\n"
    keyboard = []
    for i, f in enumerate(rule_list, 1):
        replace = f["replace_text"] or "_(delete)_"
        display_find = "Any Link" if f['find_text'] == "<ALL_LINKS>" else "Any Username" if f['find_text'] == "<ALL_USERNAMES>" else "Block Keyword" if f['find_text'] == "<BLOCK>" else f"`{f['find_text']}`"
        text += f"{i}. {display_find} → `{replace}`\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 Remove #{i}", callback_data=f"rmfilter:{f['id']}")
        ])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def myfilters_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    filter_id = query.data.split(":", 1)[1]
    deleted = filters_db.remove_filter(filter_id)
    if deleted:
        await query.edit_message_text("✅ Filter removed.")
    else:
        await query.edit_message_text("❌ Filter not found (already removed?).")


# ─────────────────────────────────────────────────────────────────────────────
# /mystatus
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def mystatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = users_db.get_user(user_id)

    if not user:
        await update.message.reply_text("❌ User not found. Try /start first.")
        return

    sub_end_str = user.get("subscription_end")
    if not sub_end_str:
        await update.message.reply_text("❌ No subscription found. Contact the Super Admin.")
        return

    sub_end = datetime.fromisoformat(sub_end_str)
    if sub_end.tzinfo is None:
        sub_end = sub_end.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    days_left = max(0, (sub_end - now).days)
    is_active = sub_end > now

    source = channels_db.get_source_channel(user_id)
    targets = channels_db.get_target_channels(user_id)
    rule_list = filters_db.get_filters(user_id)
    ub_active = userbot_manager.is_userbot_authorized(user_id)

    source_str = (
        f"@{source['channel_username']}" if source and source.get("channel_username")
        else str(source["channel_id"]) if source
        else "❌ Not set"
    )
    ub_str = "✅ Linked & Active" if ub_active else "❌ Not linked — run /authorize"

    await update.message.reply_text(
        f"📊 *Your Status*\n\n"
        f"👤 Role: *Admin*\n"
        f"📅 Subscription: {'✅ Active' if is_active else '❌ Expired'}\n"
        f"📆 Expires: `{sub_end.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"⏳ Days left: *{days_left}*\n\n"
        f"🔐 Account: {ub_str}\n"
        f"📡 Source: `{source_str}`\n"
        f"🎯 Targets: *{len(targets)}* channel(s)\n"
        f"🔧 Filters: *{len(rule_list)}* rule(s)",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /schedule
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return ConversationHandler.END

    await update.message.reply_text(
        "⏰ *Schedule a Message*\n\n"
        "Step 1/3 — Send the *message content* to schedule.",
        parse_mode="Markdown",
    )
    return SCHED_CONTENT_WAIT


async def sched_content_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    content = update.message.text
    if not content:
        await update.message.reply_text("❌ Message content cannot be empty. Try again.")
        return SCHED_CONTENT_WAIT

    context.user_data[_CTX_SCHED_CONTENT] = content
    await update.message.reply_text(
        "✅ Content saved!\n\n"
        "Step 2/3 — Send the *time* to post.\n"
        "Examples:\n"
        "• `14:30` (UTC)\n"
        "• `12:00 PM` (UTC)\n"
        "• `12:00 PM IND` (Indian Time)\n"
        "• `14:30 IST`",
        parse_mode="Markdown",
    )
    return SCHED_TIME_WAIT


async def sched_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = (update.message.text or "").strip()
    parsed_utc = parse_schedule_time(time_str)
    if not parsed_utc:
        await update.message.reply_text(
            "❌ Invalid format. Use HH:MM or HH:MM PM [IND], e.g. `14:30` or `12:00 PM IND`\nTry again.",
            parse_mode="Markdown",
        )
        return SCHED_TIME_WAIT

    context.user_data[_CTX_SCHED_TIME] = parsed_utc
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔂 Daily", callback_data="schedfreq:daily"),
            InlineKeyboardButton("1️⃣ One-time", callback_data="schedfreq:once"),
        ]
    ])
    await update.message.reply_text(
        f"✅ Time set: *{parsed_utc} UTC*\n\nStep 3/3 — How often?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return SCHED_FREQ_WAIT


async def sched_freq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    frequency = query.data.split(":", 1)[1]
    admin_id = query.from_user.id

    content = context.user_data.pop(_CTX_SCHED_CONTENT, None)
    post_time = context.user_data.pop(_CTX_SCHED_TIME, None)

    if not content or not post_time:
        await query.edit_message_text("❌ Session expired. Please start /schedule again.")
        return ConversationHandler.END

    record = schedules_db.add_schedule(admin_id, content, post_time, frequency)
    schedule_id = record.get("id")
    if schedule_id:
        schedule_message(
            bot=context.bot,
            schedule_id=schedule_id,
            admin_id=admin_id,
            content=content,
            post_time=post_time,
            frequency=frequency,
        )

    freq_label = "Daily 🔂" if frequency == "daily" else "One-time 1️⃣"
    await query.edit_message_text(
        f"✅ *Scheduled!*\n\n"
        f"Time: `{post_time} UTC`\n"
        f"Frequency: {freq_label}\n\n"
        f"Preview: _{content[:100]}{'...' if len(content) > 100 else ''}_",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /removeschedule
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def removeschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = update.effective_user.id
    if not await _check_authorized(update, admin_id):
        return

    scheds = schedules_db.get_schedules(admin_id, active_only=True)
    if not scheds:
        await update.message.reply_text("📭 You have no active scheduled messages.")
        return

    text = "⏰ *Your Scheduled Messages*\n\n"
    keyboard = []
    for i, s in enumerate(scheds, 1):
        freq = "🔂 Daily" if s["frequency"] == "daily" else "1️⃣ Once"
        text += f"{i}. `{s['post_time']}` UTC — {freq}\n   _{s['content'][:50]}..._\n\n"
        keyboard.append([
            InlineKeyboardButton(f"🗑 Remove #{i}", callback_data=f"rmsched:{s['id']}")
        ])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def removeschedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    schedule_id = query.data.split(":", 1)[1]
    deactivated = schedules_db.deactivate_schedule(schedule_id)
    if deactivated:
        remove_scheduled_job(schedule_id)
        await query.edit_message_text("✅ Scheduled message removed.")
    else:
        await query.edit_message_text("❌ Schedule not found (already removed?).")


# ─────────────────────────────────────────────────────────────────────────────
# /cancel (shared fallback)
# ─────────────────────────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    cancel_handler = CommandHandler("cancel", cancel_command)

    # /authorize — OTP login
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("authorize", authorize_start)],
        states={
            AUTH_PHONE_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_phone_receive),
            ],
            AUTH_CODE_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_code_receive),
            ],
            AUTH_2FA_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_2fa_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", auth_cancel)],
        name="authorize",
        per_user=True,
        per_chat=True,
    ))

    # /addsource
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addsource", addsource_start)],
        states={
            ADD_SOURCE_WAIT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, addsource_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="addsource",
    ))

    # /addtarget
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addtarget", addtarget_start)],
        states={
            ADD_TARGET_WAIT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, addtarget_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="addtarget",
    ))

    # /filter
    with warnings.catch_warnings():
        import telegram.warnings
        warnings.simplefilter("ignore", telegram.warnings.PTBUserWarning)
        application.add_handler(ConversationHandler(
            entry_points=[CommandHandler("filter", filter_start)],
            states={
                FILTER_TYPE_WAIT: [
                    CallbackQueryHandler(filter_type_callback, pattern=r"^ftype:"),
                ],
                FILTER_FIND_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, filter_find_receive),
                ],
                FILTER_REPLACE_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, filter_replace_receive),
                ],
            },
            fallbacks=[cancel_handler],
            name="filter",
        ))

    # /schedule
    with warnings.catch_warnings():
        import telegram.warnings
        warnings.simplefilter("ignore", telegram.warnings.PTBUserWarning)
        application.add_handler(ConversationHandler(
            entry_points=[CommandHandler("schedule", schedule_start)],
            states={
                SCHED_CONTENT_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sched_content_receive),
                ],
                SCHED_TIME_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sched_time_receive),
                ],
                SCHED_FREQ_WAIT: [
                    CallbackQueryHandler(sched_freq_callback, pattern=r"^schedfreq:"),
                ],
            },
            fallbacks=[cancel_handler],
            name="schedule",
            per_message=False,
        ))

    # Standalone commands
    application.add_handler(CommandHandler("removesource", removesource_command))
    application.add_handler(CommandHandler("removetarget", removetarget_command))
    application.add_handler(CommandHandler("myfilters", myfilters_command))
    application.add_handler(CommandHandler("removeschedule", removeschedule_command))
    application.add_handler(CommandHandler("mystatus", mystatus_command))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(myfilters_callback, pattern=r"^rmfilter:"))
    application.add_handler(CallbackQueryHandler(removeschedule_callback, pattern=r"^rmsched:"))
    application.add_handler(CallbackQueryHandler(removetarget_callback, pattern=r"^rmtarget:"))
