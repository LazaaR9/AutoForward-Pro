"""
bot/handlers/payment.py
Handles the full /pro premium subscription flow:
  - Plan selection (1 Month, 3 Months, 6 Months)
  - Payment method selection (INR via Razorpay / USDT crypto)
  - Razorpay payment link generation + payment link ID stored in DB
  - "I've Paid" → auto-checks Razorpay API → if confirmed, activates subscription instantly
  - USDT → mandatory manual review by super admin
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, SUPER_ADMIN_ID
from bot.db import subscriptions as subs_db
from bot.db import users as users_db
from bot.db import referrals as ref_db
from bot.db.transactions import log_transaction

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Plan definitions
# ─────────────────────────────────────────────────────────────────────────────

PLANS = {
    "1_month": {
        "label": "1 Month",
        "inr": 88,
        "usdt": 2,
        "days": 30,
        "per_day_inr": "₹2.9/day",
    },
    "3_months": {
        "label": "3 Months",
        "inr": 250,
        "usdt": 5,
        "days": 90,
        "per_day_inr": "₹2.7/day",
    },
    "6_months": {
        "label": "6 Months",
        "inr": 500,
        "usdt": 10,
        "days": 180,
        "per_day_inr": "₹2.7/day",
    },
}

# USDT wallet addresses
USDT_BEP20 = "0x565f64c9edc74e60ae4ca24c816b25d10dd9bdf6"
USDT_TRC20 = "TMjuAXzEfvLuzxQv9CrtPWy4m6jA2QxvHf"
BINANCE_ID = "212753448"
SUPPORT_USERNAME = "@savvyop"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _razorpay_available() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def _get_razorpay_client():
    """Return a configured Razorpay client or None."""
    if not _razorpay_available():
        return None
    try:
        import razorpay  # type: ignore
        return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    except Exception as exc:
        logger.error("Could not create Razorpay client: %s", exc)
        return None


def _create_razorpay_link(plan_key: str, user_id: int) -> tuple[Optional[str], Optional[str]]:
    """
    Create a Razorpay Payment Link.
    Returns (short_url, payment_link_id) or (None, None) on failure.
    """
    client = _get_razorpay_client()
    if not client:
        return None, None
    try:
        plan = PLANS[plan_key]
        data = {
            "amount": plan["inr"] * 100,  # paise
            "currency": "INR",
            "description": f"Premium Membership – {plan['label']}",
            "notes": {
                "telegram_id": str(user_id),
                "plan": plan_key,
            },
        }
        resp = client.payment_link.create(data)
        return resp.get("short_url"), resp.get("id")
    except Exception as exc:
        logger.error("Razorpay link creation failed: %s", exc)
        return None, None


def _check_razorpay_payment(payment_link_id: str) -> bool:
    """
    Check if a Razorpay Payment Link has been paid.
    Returns True if status == 'paid'.
    """
    client = _get_razorpay_client()
    if not client or not payment_link_id:
        return False
    try:
        resp = client.payment_link.fetch(payment_link_id)
        status = resp.get("status", "")
        logger.info("Razorpay payment link %s status: %s", payment_link_id, status)
        return status == "paid"
    except Exception as exc:
        logger.error("Razorpay payment check failed for %s: %s", payment_link_id, exc)
        return False


def _activate_user(user_id: int, plan_key: str, amount: float, method: str) -> datetime:
    """
    Make the user an admin with the correct subscription dates.
    Returns the expiry datetime.
    """
    plan = PLANS[plan_key]
    now = _now()
    expires_at = now + timedelta(days=plan["days"])

    users_db.set_subscription(
        user_id=user_id,
        plan=plan_key,
        start_dt=now,
        end_dt=expires_at,
        payment_method=method,
        amount=amount,
        payment_status="paid",
    )
    log_transaction(user_id, amount, plan["days"], now, expires_at)

    # Credit referral earnings if this user was referred by someone
    earned = ref_db.credit_referral(user_id)
    if earned:
        logger.info("Referral credit: user %s converted to Pro, referrer earned ₹%.2f", user_id, earned)

    return expires_at


def _build_plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, plan in PLANS.items():
        emoji = "1️⃣" if key == "1_month" else "3️⃣" if key == "3_months" else "6️⃣"
        rows.append([
            InlineKeyboardButton(
                f"{emoji} {plan['label']} — ₹{plan['inr']} INR  |  USDT {plan['usdt']}",
                callback_data=f"plan_select:{key}",
            )
        ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="plan_cancel")])
    return InlineKeyboardMarkup(rows)


def _build_payment_method_keyboard(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 INR Payment (UPI / Razorpay)", callback_data=f"pay_inr:{plan_key}")],
        [InlineKeyboardButton("₮ USDT Payment (Crypto)", callback_data=f"pay_usdt:{plan_key}")],
        [InlineKeyboardButton("⬅️ Back to Plans", callback_data="plan_back")],
    ])


_PLANS_TEXT = (
    "💎 *Premium Membership*\n\n"
    "Unlock real-time channel forwarding, text filters,\n"
    "scheduled messages and much more!\n\n"
    "📋 *Choose your plan:*\n\n"
    "1️⃣ *1 Month* — ₹88 INR\n"
    "_(≈ ₹2.9/day • USDT 2)_\n\n"
    "3️⃣ *3 Months* — ₹250 INR\n"
    "_(≈ ₹2.7/day • USDT 5)_\n\n"
    "6️⃣ *6 Months* — ₹500 INR\n"
    "_(≈ ₹2.7/day • USDT 10)_"
)


# ─────────────────────────────────────────────────────────────────────────────
# /pro command
# ─────────────────────────────────────────────────────────────────────────────

async def pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the premium membership plan selection UI."""
    tg_user = update.effective_user
    user = users_db.get_or_create_user(tg_user.id, tg_user.username)

    if user["role"] in ("admin", "superadmin"):
        sub_end_str = user.get("subscription_end")
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)
        days_left = max(0, (sub_end - _now()).days) if sub_end else 0
        plan_name = (user.get("subscription_plan") or "—").replace("_", " ").title()

        await update.message.reply_text(
            f"✅ *You already have an active premium membership!*\n\n"
            f"📦 Plan: *{plan_name}*\n"
            f"📅 Expires: `{sub_end.strftime('%d/%m/%Y') if sub_end else 'N/A'}`\n"
            f"⏳ Days remaining: *{days_left}*\n\n"
            f"💬 To renew or for any issues, contact {SUPPORT_USERNAME}",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        _PLANS_TEXT,
        parse_mode="Markdown",
        reply_markup=_build_plans_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: plan selected / back / cancel
# ─────────────────────────────────────────────────────────────────────────────

async def plan_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    plan_key = query.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    context.user_data["selected_plan"] = plan_key

    await query.edit_message_text(
        f"💎 *Premium Membership*\n\n"
        f"📦 Selected plan: *{plan['label']}*\n"
        f"💰 INR: *₹{plan['inr']}* ({plan['per_day_inr']})\n"
        f"₮ USDT: *{plan['usdt']} USDT*\n\n"
        f"Choose payment method:",
        parse_mode="Markdown",
        reply_markup=_build_payment_method_keyboard(plan_key),
    )


async def plan_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("selected_plan", None)
    await query.edit_message_text(
        _PLANS_TEXT,
        parse_mode="Markdown",
        reply_markup=_build_plans_keyboard(),
    )


async def plan_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("selected_plan", None)
    await query.edit_message_text(
        "ℹ️ No problem! Use /pro anytime to view premium plans and upgrade.\n\n"
        f"💬 Have questions? Contact {SUPPORT_USERNAME}",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: INR payment — generate Razorpay link, store link ID in DB
# ─────────────────────────────────────────────────────────────────────────────

async def pay_inr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("⏳ Generating payment link...")

    plan_key = query.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    user_id = query.from_user.id

    payment_url, payment_link_id = _create_razorpay_link(plan_key, user_id)

    # Store pending record — with razorpay link ID if available
    subs_db.create_subscription_record(
        telegram_id=user_id,
        plan_name=plan_key,
        amount=plan["inr"],
        payment_method="inr",
        razorpay_order_id=payment_link_id,  # None if no Razorpay keys
    )

    if payment_url and payment_link_id:
        # Razorpay available — show payment link + auto-verify button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay Now — ₹{plan['inr']}", url=payment_url)],
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid_check:{plan_key}:{payment_link_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"plan_select:{plan_key}")],
        ])
        await query.edit_message_text(
            f"💳 *INR Payment — {plan['label']}*\n\n"
            f"Amount: *₹{plan['inr']}*\n\n"
            f"1️⃣ Tap *Pay Now* to complete your payment securely via Razorpay\n"
            f"2️⃣ Come back here and tap *I've Paid*\n\n"
            f"✅ Your subscription will be activated *automatically* once payment is confirmed.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 Having trouble? Contact {SUPPORT_USERNAME}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        # No Razorpay keys — fallback to manual UPI contact flow
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Contact {SUPPORT_USERNAME} for UPI", url="https://t.me/savvyop")],
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid_check:{plan_key}:none")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"plan_select:{plan_key}")],
        ])
        await query.edit_message_text(
            f"💳 *INR Payment — {plan['label']}*\n\n"
            f"Amount: *₹{plan['inr']}*\n\n"
            f"🇮🇳 *How to pay via UPI:*\n"
            f"Contact {SUPPORT_USERNAME} to receive the UPI QR / ID.\n\n"
            f"After completing your payment:\n"
            f"1️⃣ Send a screenshot to {SUPPORT_USERNAME}\n"
            f"2️⃣ Tap *I've Paid* below\n\n"
            f"_Activation within a few minutes after verification._",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: USDT payment
# ─────────────────────────────────────────────────────────────────────────────

async def pay_usdt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    plan_key = query.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    user_id = query.from_user.id
    username = query.from_user.username or str(user_id)

    # Store pending record for USDT
    subs_db.create_subscription_record(
        telegram_id=user_id,
        plan_name=plan_key,
        amount=plan["usdt"],
        payment_method="usdt",
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Contact {SUPPORT_USERNAME}", url="https://t.me/savvyop")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"plan_select:{plan_key}")],
    ])

    await query.edit_message_text(
        f"₮ *USDT Payment — {plan['label']}*\n\n"
        f"Amount: *{plan['usdt']} USDT*\n\n"
        f"Send payment to one of the following addresses:\n\n"
        f"🔷 *BEP20 (BSC Network):*\n"
        f"`{USDT_BEP20}`\n\n"
        f"🔴 *TRC20 (TRON Network):*\n"
        f"`{USDT_TRC20}`\n\n"
        f"🟡 *Binance Pay ID:*\n"
        f"`{BINANCE_ID}`\n\n"
        f"_Tap any address above to copy it_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📩 *After payment, contact {SUPPORT_USERNAME} with:*\n"
        f"• Transaction Hash / TxID\n"
        f"• Plan: *{plan['label']}*\n"
        f"• Your username: @{username}\n\n"
        f"_Your membership will be activated after verification_ ⏱",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback: "I've Paid" — auto-verify via Razorpay API, then activate
# ─────────────────────────────────────────────────────────────────────────────

async def paid_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Smart payment verification:
    - If Razorpay link ID present → check Razorpay API directly
    - If confirmed paid → activate user immediately (no admin needed)
    - If not paid → prompt to pay / contact support
    - If no Razorpay keys (UPI fallback) → store pending, notify admin
    """
    query = update.callback_query
    await query.answer("🔍 Checking your payment...")

    parts = query.data.split(":", 2)
    plan_key = parts[1]
    payment_link_id = parts[2] if len(parts) > 2 else "none"

    if plan_key not in PLANS:
        await query.answer("❌ Invalid plan.", show_alert=True)
        return

    plan = PLANS[plan_key]
    user_id = query.from_user.id
    username = query.from_user.username or str(user_id)

    # ── Case 1: Razorpay available with a real link ID ─────────────────────
    if payment_link_id and payment_link_id != "none" and _razorpay_available():
        is_paid = _check_razorpay_payment(payment_link_id)

        if is_paid:
            # ✅ Payment confirmed — auto-activate
            expires_at = _activate_user(
                user_id=user_id,
                plan_key=plan_key,
                amount=float(plan["inr"]),
                method="inr",
            )

            # Update DB subscription record
            pending = subs_db.get_pending_subscription_by_user(user_id)
            if pending:
                subs_db.activate_subscription(pending["id"], expires_at)

            await query.edit_message_text(
                f"🎉 *Payment Successful!*\n\n"
                f"Your premium membership has been activated.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Plan: *{plan['label']}*\n"
                f"📅 Expires: `{expires_at.strftime('%d/%m/%Y')}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Use /start to see all your admin commands.\n\n"
                f"Thank you for your support ❤️",
                parse_mode="Markdown",
            )
            logger.info("Auto-activated user %s on plan %s via Razorpay.", user_id, plan_key)
            return

        else:
            # ❌ Not paid yet
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Pay Now — ₹{plan['inr']}", callback_data=f"pay_inr:{plan_key}")],
                [InlineKeyboardButton(f"💬 Help — Contact {SUPPORT_USERNAME}", url="https://t.me/savvyop")],
            ])
            await query.edit_message_text(
                f"❌ *Payment Not Confirmed*\n\n"
                f"We could not find a completed payment for your account.\n\n"
                f"*Possible reasons:*\n"
                f"• Payment is still processing (wait 1–2 min and try again)\n"
                f"• Payment was not completed\n"
                f"• You paid from a different session\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"If you've already paid and still see this message,\n"
                f"please contact {SUPPORT_USERNAME} with your payment screenshot.\n"
                f"We'll activate your account manually. 🙏",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return

    # ── Case 2: No Razorpay / UPI manual flow ──────────────────────────────
    # Check DB for an existing paid subscription record
    pending = subs_db.get_pending_subscription_by_user(user_id)

    if pending and pending.get("payment_status") == "paid":
        # Already activated by admin previously — just confirm
        user = users_db.get_user(user_id)
        sub_end_str = user.get("subscription_end") if user else None
        sub_end = datetime.fromisoformat(sub_end_str) if sub_end_str else None
        if sub_end and sub_end.tzinfo is None:
            sub_end = sub_end.replace(tzinfo=timezone.utc)

        await query.edit_message_text(
            f"✅ *You are already subscribed!*\n\n"
            f"📦 Plan: *{plan['label']}*\n"
            f"📅 Expires: `{sub_end.strftime('%d/%m/%Y') if sub_end else 'N/A'}`\n\n"
            f"Use /start to see all your commands.",
            parse_mode="Markdown",
        )
        return

    # No record confirmed — notify admin and tell user to wait
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Contact {SUPPORT_USERNAME}", url="https://t.me/savvyop")],
    ])
    await query.edit_message_text(
        f"📨 *Payment Notification Sent!*\n\n"
        f"📦 Plan: *{plan['label']}*\n"
        f"💰 Amount: *₹{plan['inr']} INR*\n\n"
        f"The admin has been notified and will activate your account shortly.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ *Please wait* — activation usually takes a few minutes.\n\n"
        f"💬 Having trouble? Contact {SUPPORT_USERNAME}\n"
        f"_Send your payment screenshot for faster verification._\n\n"
        f"Thank you for your support ❤️",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    # Notify super admin with one-tap grant command
    try:
        await context.bot.send_message(
            chat_id=SUPER_ADMIN_ID,
            text=(
                f"🔔 *New INR Payment — Manual Verification Needed*\n\n"
                f"👤 User: @{username} (`{user_id}`)\n"
                f"📦 Plan: *{plan['label']}*\n"
                f"💰 Amount: *₹{plan['inr']}*\n\n"
                f"After verifying payment, run:\n"
                f"`/grant_premium {user_id} {plan['days']}`"
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify super admin: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    application.add_handler(CommandHandler("pro", pro_command))
    application.add_handler(CallbackQueryHandler(plan_select_callback, pattern=r"^plan_select:"))
    application.add_handler(CallbackQueryHandler(plan_back_callback, pattern=r"^plan_back$"))
    application.add_handler(CallbackQueryHandler(plan_cancel_callback, pattern=r"^plan_cancel$"))
    application.add_handler(CallbackQueryHandler(pay_inr_callback, pattern=r"^pay_inr:"))
    application.add_handler(CallbackQueryHandler(pay_usdt_callback, pattern=r"^pay_usdt:"))
    application.add_handler(CallbackQueryHandler(paid_check_callback, pattern=r"^paid_check:"))
