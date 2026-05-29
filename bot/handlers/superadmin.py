"""
bot/handlers/superadmin.py
Super Admin only ConversationHandlers and commands:
  /stats         — System statistics
  /alladmins     — List all admins
  /allchannels   — List all channels
  /addadmin      — Promote user to admin
  /removeadmin   — Demote admin immediately
  /addincome     — Log a manual payment
"""

from __future__ import annotations

import logging
import warnings
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.db import channels as channels_db
from bot.db import transactions as tx_db
from bot.db import users as users_db
from bot.db.transactions import log_transaction
from bot.utils.roles import require_superadmin

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Conversation states
# ─────────────────────────────────────────────────────────────────────────────
(
    ADDADMIN_USERID_WAIT,
    ADDADMIN_DURATION_WAIT,
    REMOVEADMIN_WAIT,
    ADDINCOME_ADMINID_WAIT,
    ADDINCOME_AMOUNT_WAIT,
) = range(5)

# Context keys
_CTX_TARGET_USER = "target_user_id"
_CTX_INCOME_ADMIN = "income_admin_id"


# ─────────────────────────────────────────────────────────────────────────────
# /stats
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    all_admins = users_db.get_all_admins()
    now = datetime.now(timezone.utc)

    active_admins = 0
    for a in all_admins:
        sub_end_str = a.get("subscription_end")
        if sub_end_str:
            sub_end = datetime.fromisoformat(sub_end_str)
            if sub_end.tzinfo is None:
                sub_end = sub_end.replace(tzinfo=timezone.utc)
            if sub_end > now:
                active_admins += 1

    total_channels = channels_db.get_all_channels_count()
    total_income = tx_db.get_total_income()
    total_tx = tx_db.get_total_transactions()

    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👑 Total admins: *{len(all_admins)}*\n"
        f"✅ Active admins: *{active_admins}*\n"
        f"📡 Total channels: *{total_channels}*\n"
        f"💰 Total income: *${total_income:,.2f}*\n"
        f"🧾 Total transactions: *{total_tx}*",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /alladmins
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def alladmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admins = users_db.get_all_admins()

    if not admins:
        await update.message.reply_text("📭 No admins found.")
        return

    now = datetime.now(timezone.utc)
    lines = ["👥 *All Admins*\n"]

    for i, admin in enumerate(admins, start=1):
        username = f"@{admin['username']}" if admin.get("username") else "—"
        sub_end_str = admin.get("subscription_end")
        if sub_end_str:
            sub_end = datetime.fromisoformat(sub_end_str)
            if sub_end.tzinfo is None:
                sub_end = sub_end.replace(tzinfo=timezone.utc)
            days_left = max(0, (sub_end - now).days)
            status = "✅" if sub_end > now else "❌"
            expiry_str = sub_end.strftime("%Y-%m-%d")
        else:
            status = "❌"
            expiry_str = "N/A"
            days_left = 0

        lines.append(
            f"{i}. {username} (`{admin['user_id']}`)\n"
            f"   {status} Expires: `{expiry_str}` ({days_left}d left)"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /allchannels
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def allchannels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sources = channels_db.get_all_source_channels()
    targets = channels_db.get_all_target_channels()

    if not sources and not targets:
        await update.message.reply_text("📭 No channels configured yet.")
        return

    lines = ["📡 *All Channels*\n"]

    if sources:
        lines.append("*Source Channels:*")
        for s in sources:
            display = f"@{s['channel_username']}" if s.get("channel_username") else str(s["channel_id"])
            lines.append(f"• {display} — admin `{s['added_by']}`")
    else:
        lines.append("*Source Channels:* None")

    lines.append("")

    if targets:
        lines.append("*Target Channels:*")
        for t in targets:
            display = f"@{t['channel_username']}" if t.get("channel_username") else str(t["channel_id"])
            lines.append(f"• {display} — admin `{t['admin_id']}`")
    else:
        lines.append("*Target Channels:* None")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /addadmin conversation
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def addadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "➕ *Add Admin*\n\n"
        "Step 1/2 — Send me the *Telegram User ID* of the user to promote.\n"
        "(They must have started the bot at least once.)\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return ADDADMIN_USERID_WAIT


async def addadmin_userid_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric Telegram User ID. Try again.")
        return ADDADMIN_USERID_WAIT

    target_id = int(text)
    user = users_db.get_user(target_id)

    if user is None:
        await update.message.reply_text(
            f"❌ User `{target_id}` not found in the database.\n"
            "Make sure they have sent /start to the bot first.",
            parse_mode="Markdown",
        )
        return ADDADMIN_USERID_WAIT

    context.user_data[_CTX_TARGET_USER] = target_id
    username = f"@{user['username']}" if user.get("username") else f"ID {target_id}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("30 Days", callback_data="addadmin_dur:30"),
            InlineKeyboardButton("60 Days", callback_data="addadmin_dur:60"),
        ]
    ])
    await update.message.reply_text(
        f"✅ User found: *{username}*\n\n"
        "Step 2/2 — Select subscription duration:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ADDADMIN_DURATION_WAIT


async def addadmin_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    duration_days = int(query.data.split(":", 1)[1])
    target_id = context.user_data.pop(_CTX_TARGET_USER, None)

    if target_id is None:
        await query.edit_message_text("❌ Session expired. Please run /addadmin again.")
        return ConversationHandler.END

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=duration_days)

    # Promote
    users_db.set_role(target_id, "admin")
    users_db.set_subscription_end(target_id, expires_at)
    log_transaction(target_id, 0.0, duration_days, now, expires_at)

    # Notify the promoted user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 *Congratulations! You have been promoted to Admin!*\n\n"
                f"📅 Subscription: *{duration_days} days*\n"
                f"⏳ Expires: `{expires_at.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"Use /start to see your admin commands."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify user %s: %s", target_id, exc)

    user = users_db.get_user(target_id)
    username = f"@{user['username']}" if user and user.get("username") else f"ID {target_id}"

    await query.edit_message_text(
        f"✅ *{username}* has been promoted to Admin!\n\n"
        f"Duration: *{duration_days} days*\n"
        f"Expires: `{expires_at.strftime('%Y-%m-%d %H:%M UTC')}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /removeadmin conversation
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def removeadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "➖ *Remove Admin*\n\n"
        "Send me the *Telegram User ID* of the admin to demote.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return REMOVEADMIN_WAIT


async def removeadmin_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID. Try again.")
        return REMOVEADMIN_WAIT

    target_id = int(text)
    user = users_db.get_user(target_id)

    if user is None:
        await update.message.reply_text(f"❌ User `{target_id}` not found.", parse_mode="Markdown")
        return REMOVEADMIN_WAIT

    if user["role"] != "admin":
        await update.message.reply_text(f"ℹ️ User `{target_id}` is not an admin.", parse_mode="Markdown")
        return ConversationHandler.END

    users_db.set_role(target_id, "user")

    # Notify the demoted user
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "⚠️ *Your admin access has been revoked by the Super Admin.*\n\n"
                "Contact the Super Admin if you believe this is a mistake."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify demoted admin %s: %s", target_id, exc)

    username = f"@{user['username']}" if user.get("username") else f"ID {target_id}"
    await update.message.reply_text(
        f"✅ *{username}* has been demoted to User.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /addincome conversation
# ─────────────────────────────────────────────────────────────────────────────

@require_superadmin
async def addincome_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "💰 *Log Payment*\n\n"
        "Step 1/2 — Send me the *Admin's Telegram User ID*.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return ADDINCOME_ADMINID_WAIT


async def addincome_adminid_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID. Try again.")
        return ADDINCOME_ADMINID_WAIT

    admin_id = int(text)
    user = users_db.get_user(admin_id)
    if user is None:
        await update.message.reply_text(f"❌ User `{admin_id}` not found.", parse_mode="Markdown")
        return ADDINCOME_ADMINID_WAIT

    context.user_data[_CTX_INCOME_ADMIN] = admin_id
    username = f"@{user['username']}" if user.get("username") else f"ID {admin_id}"
    await update.message.reply_text(
        f"✅ Admin: *{username}*\n\n"
        "Step 2/2 — Send me the *payment amount* (numeric, e.g. `50` or `29.99`).",
        parse_mode="Markdown",
    )
    return ADDINCOME_AMOUNT_WAIT


async def addincome_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().replace(",", ".")
    admin_id = context.user_data.pop(_CTX_INCOME_ADMIN, None)

    if admin_id is None:
        await update.message.reply_text("❌ Session expired. Please run /addincome again.")
        return ConversationHandler.END

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Send a positive number like `50` or `29.99`.")
        return ADDINCOME_AMOUNT_WAIT

    user = users_db.get_user(admin_id)
    sub_end_str = user.get("subscription_end") if user else None
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(sub_end_str) if sub_end_str else now

    log_transaction(admin_id, amount, 0, now, expires_at)

    username = f"@{user['username']}" if user and user.get("username") else f"ID {admin_id}"
    await update.message.reply_text(
        f"✅ *Payment logged!*\n\n"
        f"Admin: *{username}*\n"
        f"Amount: *${amount:,.2f}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# /cancel (shared)
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

    # Standalone commands
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("alladmins", alladmins_command))
    application.add_handler(CommandHandler("allchannels", allchannels_command))

    # /addadmin conversation (has CallbackQueryHandler in state — per_message=False is correct)
    with warnings.catch_warnings():
        import telegram.warnings
        warnings.simplefilter("ignore", telegram.warnings.PTBUserWarning)
        application.add_handler(ConversationHandler(
            entry_points=[CommandHandler("addadmin", addadmin_start)],
            states={
                ADDADMIN_USERID_WAIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, addadmin_userid_receive),
                ],
                ADDADMIN_DURATION_WAIT: [
                    CallbackQueryHandler(addadmin_duration_callback, pattern=r"^addadmin_dur:"),
                ],
            },
            fallbacks=[cancel_handler],
            name="addadmin",
            per_message=False,
        ))

    # /removeadmin conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("removeadmin", removeadmin_start)],
        states={
            REMOVEADMIN_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, removeadmin_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="removeadmin",
    ))

    # /addincome conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addincome", addincome_start)],
        states={
            ADDINCOME_ADMINID_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addincome_adminid_receive),
            ],
            ADDINCOME_AMOUNT_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addincome_amount_receive),
            ],
        },
        fallbacks=[cancel_handler],
        name="addincome",
    ))
