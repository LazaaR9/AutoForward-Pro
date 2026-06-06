"""
bot/handlers/user.py
Handlers for regular User-role commands: /start and /plan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler

from bot.config import SUPER_ADMIN_ID, TRIAL_DAYS
from bot.db import users as users_db
from bot.db.content import get_content
from bot.db import referrals as ref_db
from bot.utils import userbot_manager

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register user, handle referral deep link, show status."""
    tg_user = update.effective_user
    user = users_db.get_or_create_user(tg_user.id, tg_user.username)
    role = user["role"]

    # ── Handle referral deep link: /start ref_<referrer_id> ──────────────
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                # Only record if this is a new user (no existing referral) and not self-referral
                if referrer_id != tg_user.id and not user.get("referred_by"):
                    ref_db.record_referral(referrer_id, tg_user.id)
                    users_db.save_referred_by(tg_user.id, referrer_id)
            except (ValueError, Exception) as e:
                logger.debug("Referral parse error: %s", e)

    if role == "superadmin":
        await update.message.reply_text(
            "👑 *Welcome back, Super Admin!*\n\n"
            "━━━━━━━━━━━━━━━\n"
            "👥 *Admin Management*\n"
            "/addadmin — Promote user to admin\n"
            "/removeadmin — Demote an admin\n"
            "/alladmins — List all admins\n"
            "/allchannels — List all channels\n"
            "/admin\\_subscriptions — View subscriptions\n"
            "/grant\\_premium — Grant premium access\n"
            "/revoke\\_premium — Revoke premium access\n\n"
            "💰 *Payments & Income*\n"
            "/addincome — Log a manual payment\n"
            "/stats — View system statistics\n\n"
            "📢 *Broadcast & Content*\n"
            "/broadcast — Send message to all users\n"
            "/update — Edit bot content (welcome msg etc.)\n\n"
            "🤝 *Referral System*\n"
            "/referralstats — Top referrers leaderboard\n"
            "/editreferral — Adjust a user's referral balance\n"
            "/refer — Your own referral dashboard\n\n"
            "━━━━━━━━━━━━━━━\n"
            "Type /cancel to abort any active conversation.",
            parse_mode="Markdown",
        )
        return

    if role == "admin":
        sub_end_str = user.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)
        days_left = max(0, (sub_end - datetime.now(timezone.utc)).days) if sub_end else 0

        # Check if userbot is authorized
        if not userbot_manager.is_userbot_authorized(tg_user.id):
            await update.message.reply_text(
                f"🔑 *Welcome, Admin!*\n\n"
                f"📅 Subscription expires: `{sub_end.strftime('%Y-%m-%d') if sub_end else 'N/A'}`\n"
                f"⏳ Days remaining: *{days_left}*\n\n"
                f"⚠️ *Telegram Account Not Linked*\n"
                f"Your account is not linked yet. Please authorize first to start forwarding.\n\n"
                f"👉 /authorize — Link your Telegram account\n\n"
                f"💡 Need help? Send /help",
                parse_mode="Markdown",
            )
            return

        # Fully authorized admin — show all commands in organized sections
        await update.message.reply_text(
            f"🔑 *Welcome back, Admin!*\n\n"
            f"📅 Subscription: `{sub_end.strftime('%Y-%m-%d') if sub_end else 'N/A'}` | *{days_left} days left*\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📡 *Forwarding*\n"
            f"/addsource — Set source channel\n"
            f"/removesource — Remove source channel\n"
            f"/addtarget — Add a target channel\n"
            f"/removetarget — Remove a target channel\n\n"
            f"🔍 *Filters*\n"
            f"/filter — Add text/keyword filter\n"
            f"/myfilters — View & remove filters\n\n"
            f"⏰ *Scheduling*\n"
            f"/schedule — Schedule a message\n"
            f"/removeschedule — Remove a schedule\n\n"
            f"👤 *Account*\n"
            f"/mystatus — View subscription status\n"
            f"/plan — Check plan details\n"
            f"/pro — Upgrade / renew plan\n\n"
            f"🤝 *Referral & Earnings*\n"
            f"/refer — Referral dashboard & link\n"
            f"/withdraw — Cash out your earnings\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 Need help? Send /help",
            parse_mode="Markdown",
        )
        return

    # Regular user — show greeting, all admin commands, and premium call-to-action
    sa_uname = _get_superadmin_username()
    first_name = tg_user.first_name or "User"
    default_text = (
        f"<b>Welcome {first_name} to the Auto Forward Bot!</b>\n\n"
        f">> <b>Start Here:</b> Send /help to see step-by-step image tutorials on how to use the bot!\n\n"
        f"<b>Available Commands:</b>\n"
        f"/authorize — Link your Telegram account (Required)\n"
        f"/addsource — Set source channel\n"
        f"/addtarget — Add target channels\n"
        f"/filter — Add text & link filters\n"
        f"/myfilters — View/remove filters\n"
        f"/schedule — Schedule automated messages\n"
        f"/removeschedule — Remove a schedule\n"
        f"/mystatus — View your status\n"
        f"/plan — Check plan status\n"
        f"/refer — Refer & Earn 💰\n\n"
        f"<b>Paid Plan Needed:</b>\n"
        f"To activate real-time channel forwarding, please use /pro to purchase a plan and activate your account!"
    )
    
    text = get_content("welcome_msg", default_text).replace("{first_name}", first_name)
    
    image_path = IMAGES_DIR / "welcome.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption=text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# /plan
# ─────────────────────────────────────────────────────────────────────────────

async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current plan and trial/subscription status."""
    tg_user = update.effective_user
    user = users_db.get_or_create_user(tg_user.id, tg_user.username)
    role = user["role"]

    if role == "superadmin":
        await update.message.reply_text("👑 You are the *Super Admin* — unlimited access.", parse_mode="Markdown")
        return

    if role == "admin":
        sub_end_str = user.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        active = sub_end and sub_end > now
        days_left = max(0, (sub_end - now).days) if sub_end else 0

        status_icon = "✅" if active else "❌"
        await update.message.reply_text(
            f"📋 *Your Plan*\n\n"
            f"Role: *Admin*\n"
            f"Status: {status_icon} {'Active' if active else 'Expired'}\n"
            f"Expires: `{sub_end.strftime('%Y-%m-%d %H:%M UTC') if sub_end else 'N/A'}`\n"
            f"Days left: *{days_left}*",
            parse_mode="Markdown",
        )
        return

    # Free user
    trial_start = user.get("trial_start")
    days_left = users_db.get_trial_days_remaining(trial_start) if trial_start else 0
    active = days_left > 0

    status_icon = "✅" if active else "❌"
    await update.message.reply_text(
        f"📋 *Your Plan*\n\n"
        f"Role: *Free Trial*\n"
        f"Status: {status_icon} {'Active' if active else 'Expired'}\n"
        f"Days remaining: *{days_left}* / {TRIAL_DAYS}\n\n"
        + (
            f"Please use /pro to view subscription plans and upgrade." if not active else
            f"Your trial is active. Please use /pro to view subscription plans and upgrade to a paid plan."
        ),
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_superadmin_username() -> str:
    """Try to fetch the superadmin's username from DB, fallback to 'superadmin'."""
    try:
        user = users_db.get_user(SUPER_ADMIN_ID)
        if user and user.get("username"):
            return user["username"]
    except Exception:
        pass
    return "superadmin"


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("plan", plan_command))
    # /pro is now registered in bot/handlers/payment.py

