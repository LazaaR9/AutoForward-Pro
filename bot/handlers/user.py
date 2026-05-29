"""
bot/handlers/user.py
Handlers for regular User-role commands: /start and /plan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import SUPER_ADMIN_ID, TRIAL_DAYS
from bot.db import users as users_db
from bot.utils import userbot_manager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register user, start trial, show status."""
    tg_user = update.effective_user
    user = users_db.get_or_create_user(tg_user.id, tg_user.username)
    role = user["role"]

    if role == "superadmin":
        await update.message.reply_text(
            "👑 *Welcome back, Super Admin!*\n\n"
            "Use the admin panel commands to manage the bot.\n\n"
            "📋 Commands:\n"
            "/stats — View statistics\n"
            "/alladmins — List all admins\n"
            "/allchannels — List all channels\n"
            "/addadmin — Promote a user to admin\n"
            "/removeadmin — Demote an admin\n"
            "/addincome — Log a payment",
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
                f"To start using the forwarding features, you must first authorize your Telegram account.\n\n"
                f"👉 Please run /authorize to link your account and start forwarding!",
                parse_mode="Markdown",
            )
            return

        # Fully authorized admin — show all commands EXCEPT /authorize
        await update.message.reply_text(
            f"🔑 *Welcome back, Admin!*\n\n"
            f"📅 Subscription expires: `{sub_end.strftime('%Y-%m-%d') if sub_end else 'N/A'}`\n"
            f"⏳ Days remaining: *{days_left}*\n\n"
            f"📋 Your commands:\n"
            f"/addsource — Set source channel\n"
            f"/removesource — Remove source channel\n"
            f"/addtarget — Add a target channel\n"
            f"/removetarget — Remove a target channel\n"
            f"/filter — Add text filter\n"
            f"/myfilters — View/remove filters\n"
            f"/schedule — Schedule a message\n"
            f"/removeschedule — Remove a schedule\n"
            f"/mystatus — View subscription",
            parse_mode="Markdown",
        )
        return

    # Regular user — show greeting, all admin commands, and premium call-to-action
    sa_uname = _get_superadmin_username()
    await update.message.reply_text(
        f"👋 *Welcome to the Telegram Forwarding Bot!*\n\n"
        f"📋 *Available Admin Commands:*\n"
        f"/authorize — Link your Telegram account (Required)\n"
        f"/addsource — Set source channel\n"
        f"/removesource — Remove source channel\n"
        f"/addtarget — Add a target channel\n"
        f"/removetarget — Remove a target channel\n"
        f"/filter — Add text filter\n"
        f"/myfilters — View/remove filters\n"
        f"/schedule — Schedule a message\n"
        f"/removeschedule — Remove a schedule\n"
        f"/mystatus — View subscription\n"
        f"/plan — Check plan status\n\n"
        f"⭐ *Paid Plan Needed:*\n"
        f"To activate real-time channel forwarding, please contact @{sa_uname} to purchase a plan and activate your account!",
        parse_mode="Markdown",
    )


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
            f"Contact the Super Admin to upgrade." if not active else
            f"Your trial is active. Contact the Super Admin to upgrade to a paid plan."
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
