"""
bot/handlers/referral.py
Handles the /refer command — available to ALL users (free, admin, superadmin).
Shows referral stats, earnings, and the user's unique referral link.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.db import referrals as ref_db
from bot.db import users as users_db
from bot.config import SUPER_ADMIN_ID

logger = logging.getLogger(__name__)

BOT_USERNAME = "Auto_Forwarder_Official_Bot"


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

    # Withdrawal status message
    if earned >= min_withdrawal:
        withdrawal_msg = (
            f"✅ *You can withdraw!*\n"
            f"Contact @{_get_superadmin_username()} to request your payment via UPI."
        )
    else:
        remaining = min_withdrawal - earned
        withdrawal_msg = (
            f"⏳ Earn ₹{remaining:.0f} more to reach the minimum withdrawal of ₹{min_withdrawal:.0f}"
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
        f"📤 Share this link with your friends. When they buy Pro, you earn ₹{payout:.0f}!\n\n"
        f"{withdrawal_msg}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share My Referral Link", url=f"https://t.me/share/url?url={ref_link}&text=Join%20Auto%20Forwarder%20Bot%20and%20get%20real-time%20channel%20forwarding!")]
    ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


def _get_superadmin_username() -> str:
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
    application.add_handler(CommandHandler("refer", refer_command))
