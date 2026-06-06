"""
bot/handlers/referral.py
Handles the /refer and /withdraw commands — available to ALL users.
Shows referral stats, earnings, referral link, and withdrawal info.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes

from bot.db import referrals as ref_db
from bot.db import users as users_db
from bot.config import SUPER_ADMIN_ID

logger = logging.getLogger(__name__)

BOT_USERNAME = "Auto_Forwarder_Official_Bot"
OWNER_USERNAME = "Savvyop"


# ─────────────────────────────────────────────────────────────────────────────
# /refer
# ─────────────────────────────────────────────────────────────────────────────

async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show referral dashboard for any user."""
    tg_user = update.effective_user
    user_id = tg_user.id

    stats = ref_db.get_referral_stats(user_id)
    payout = ref_db.get_referral_payout_amount()
    min_withdrawal = ref_db.get_min_withdrawal()

    total = stats["total_referrals"]
    pro = stats["pro_referrals"]
    earned = stats["total_earned"]

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    # Withdrawal status
    if earned >= min_withdrawal:
        withdrawal_status = (
            f"✅ *Withdrawal Available!*\n"
            f"You have *₹{earned:.2f}* ready to withdraw.\n"
            f"👉 Contact @{OWNER_USERNAME} with your UPI ID to get paid!"
        )
    else:
        remaining = min_withdrawal - earned
        withdrawal_status = (
            f"⏳ *₹{remaining:.0f} more* to reach the minimum withdrawal of ₹{min_withdrawal:.0f}\n"
            f"Use /withdraw to learn how to cash out."
        )

    text = (
        f"🤝 *Refer & Earn*\n\n"
        f"Earn *₹{payout:.0f}* for every friend who buys a Pro plan!\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 *Your Stats*\n"
        f"👥 Total Referrals: *{total}*\n"
        f"⭐ Pro Conversions: *{pro}*\n"
        f"💰 Total Earned: *₹{earned:.2f}*\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"📤 Share this link. When a friend buys Pro, you earn ₹{payout:.0f}!\n\n"
        f"💳 *Withdrawal:*\n"
        f"{withdrawal_status}\n\n"
        f"📌 Minimum withdrawal: *₹{min_withdrawal:.0f}* via UPI"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📤 Share My Referral Link",
            url=f"https://t.me/share/url?url={ref_link}&text=Join%20Auto%20Forwarder%20Bot%20and%20get%20real-time%20channel%20forwarding!"
        )],
        [InlineKeyboardButton("💸 How to Withdraw?", callback_data="refer_withdraw_info")],
    ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ─────────────────────────────────────────────────────────────────────────────
# /withdraw
# ─────────────────────────────────────────────────────────────────────────────

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show withdrawal instructions."""
    tg_user = update.effective_user
    user_id = tg_user.id

    stats = ref_db.get_referral_stats(user_id)
    min_withdrawal = ref_db.get_min_withdrawal()
    earned = stats["total_earned"]

    if earned >= min_withdrawal:
        status_line = f"✅ *You are eligible to withdraw ₹{earned:.2f}!*"
    else:
        remaining = min_withdrawal - earned
        status_line = f"⏳ You need *₹{remaining:.0f} more* to reach the minimum (₹{min_withdrawal:.0f})"

    text = (
        f"💸 *Withdraw Your Earnings*\n\n"
        f"{status_line}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 *How to Withdraw:*\n\n"
        f"1️⃣ Make sure your balance is ₹{min_withdrawal:.0f} or more\n"
        f"2️⃣ Contact *@{OWNER_USERNAME}* on Telegram\n"
        f"3️⃣ Send your *UPI ID* and *User ID* (`{user_id}`)\n"
        f"4️⃣ Payment will be sent within *24 hours* ✅\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Your current balance: *₹{earned:.2f}*\n"
        f"📌 Minimum withdrawal: *₹{min_withdrawal:.0f}*\n\n"
        f"📣 Keep referring to earn more! Use /refer to get your link."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Contact @{OWNER_USERNAME}", url=f"https://t.me/{OWNER_USERNAME}")],
        [InlineKeyboardButton("🔗 My Referral Link", callback_data="refer_show_link")],
    ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ─────────────────────────────────────────────────────────────────────────────
# Callback for inline buttons
# ─────────────────────────────────────────────────────────────────────────────

async def refer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "refer_withdraw_info":
        await withdraw_command(update, context)
    elif query.data == "refer_show_link":
        user_id = query.from_user.id
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        await query.message.reply_text(
            f"🔗 *Your Referral Link:*\n`{ref_link}`",
            parse_mode="Markdown",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    from telegram.ext import CallbackQueryHandler
    application.add_handler(CommandHandler("refer", refer_command))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CallbackQueryHandler(refer_callback, pattern=r"^refer_"))
