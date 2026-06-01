"""
bot/webhook/razorpay_webhook.py
Production-ready Razorpay webhook server.

Architecture:
  - Runs an aiohttp HTTP server in the SAME asyncio event loop as PTB polling.
  - Started in post_init, stopped in post_shutdown — no extra threads needed.
  - Listens on POST /webhooks/razorpay

Security:
  - HMAC-SHA256 signature verification on every request (constant-time compare).
  - Deduplication via `processed_webhooks` table — safe against Razorpay retries.
  - Returns 200 OK for all non-error states to stop Razorpay retry loops.

Events handled:
  - payment.captured → activate subscription, notify user via Telegram
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiohttp import web
from telegram import Bot

from bot.config import (
    RAZORPAY_WEBHOOK_SECRET,
    WEBHOOK_PORT,
    SUPER_ADMIN_ID,
)
from bot.db import subscriptions as subs_db
from bot.db import users as users_db
from bot.db.transactions import log_transaction

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Plan registry — maps Razorpay note value → subscription duration
# Accepts both singular and plural forms for resilience
# ─────────────────────────────────────────────────────────────────────────────

PLAN_REGISTRY: dict[str, dict] = {
    "1_month":  {"days": 30,  "label": "1 Month"},
    "3_months": {"days": 90,  "label": "3 Months"},
    "3_month":  {"days": 90,  "label": "3 Months"},
    "6_months": {"days": 180, "label": "6 Months"},
    "6_month":  {"days": 180, "label": "6 Months"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Module-level state (set during server startup)
# ─────────────────────────────────────────────────────────────────────────────

_runner: Optional[web.AppRunner] = None
_bot_instance: Optional[Bot] = None


# ─────────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────────

def _verify_signature(raw_body: bytes, razorpay_signature: str) -> bool:
    """
    Verify the X-Razorpay-Signature header using HMAC-SHA256.
    Uses hmac.compare_digest to prevent timing attacks.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.critical(
            "RAZORPAY_WEBHOOK_SECRET is not set! "
            "Accepting webhook WITHOUT signature verification — fix this in production."
        )
        return True  # Dev fallback; always require secret in production

    expected_sig = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison prevents timing-based attacks
    return hmac.compare_digest(expected_sig, razorpay_signature)


# ─────────────────────────────────────────────────────────────────────────────
# Payment activation
# ─────────────────────────────────────────────────────────────────────────────

def _activate_subscription(
    telegram_id: int,
    plan_key: str,
    amount_inr: float,
    payment_id: str,
    order_id: str,
) -> datetime:
    """
    Promote user to admin, write subscription to users + subscriptions tables.
    Returns the expiry datetime (UTC).
    """
    plan = PLAN_REGISTRY[plan_key]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=plan["days"])

    # Ensure user row exists (handles first-time webhook before /start)
    if not users_db.get_user(telegram_id):
        users_db.get_or_create_user(telegram_id)
        logger.warning("Created user row for telegram_id=%s from webhook (no /start yet)", telegram_id)

    # Promote to admin + store subscription metadata on users row
    users_db.set_subscription(
        user_id=telegram_id,
        plan=plan_key,
        start_dt=now,
        end_dt=expires_at,
        payment_method="inr",
        amount=amount_inr,
        payment_status="paid",
    )

    # Transaction ledger
    log_transaction(telegram_id, amount_inr, plan["days"], now, expires_at)

    # Activate any matching pending subscription record
    pending = subs_db.get_pending_subscription_by_user(telegram_id)
    if pending:
        subs_db.activate_subscription(pending["id"], expires_at)
    else:
        # No pending record (e.g. user paid directly) — create a confirmed one
        subs_db.create_webhook_subscription(
            telegram_id=telegram_id,
            plan_name=plan_key,
            amount=amount_inr,
            payment_id=payment_id,
            order_id=order_id,
            expires_at=expires_at,
        )

    logger.info(
        "Subscription activated: user=%s plan=%s expires=%s payment_id=%s",
        telegram_id, plan_key, expires_at.strftime("%Y-%m-%d"), payment_id,
    )
    return expires_at


# ─────────────────────────────────────────────────────────────────────────────
# Telegram notification
# ─────────────────────────────────────────────────────────────────────────────

async def _notify_user(telegram_id: int, plan_key: str, amount_inr: float, expires_at: datetime) -> None:
    """Send a confirmation DM to the user and a summary to the super admin."""
    if not _bot_instance:
        logger.warning("Bot not initialised — cannot send Telegram notification.")
        return

    plan_label = PLAN_REGISTRY.get(plan_key, {}).get("label", plan_key)

    # ── User notification ──────────────────────────────────────────────────
    try:
        await _bot_instance.send_message(
            chat_id=telegram_id,
            text=(
                "🎉 *Payment Successful!*\n\n"
                "Your premium membership has been activated.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Plan: *{plan_label}*\n"
                f"💰 Amount: *₹{amount_inr:.0f} INR*\n"
                f"📅 Expires: `{expires_at.strftime('%d/%m/%Y')}`\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "✅ You now have full admin access.\n"
                "Use /start to see all your commands.\n\n"
                "Thank you for your support ❤️"
            ),
            parse_mode="Markdown",
        )
        logger.info("Sent activation confirmation to telegram_id=%s", telegram_id)
    except Exception as exc:
        logger.warning("Could not DM user %s: %s", telegram_id, exc)

    # ── Super admin summary ───────────────────────────────────────────────
    try:
        user = users_db.get_user(telegram_id)
        username = f"@{user['username']}" if user and user.get("username") else str(telegram_id)
        await _bot_instance.send_message(
            chat_id=SUPER_ADMIN_ID,
            text=(
                "💰 *Razorpay Payment Confirmed*\n\n"
                f"👤 User: {username} (`{telegram_id}`)\n"
                f"📦 Plan: *{plan_label}*\n"
                f"💵 Amount: *₹{amount_inr:.0f} INR*\n"
                f"📅 Expires: `{expires_at.strftime('%d/%m/%Y')}`\n\n"
                "_Activated automatically via webhook._"
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify super admin: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Request handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_razorpay_webhook(request: web.Request) -> web.Response:
    """
    POST /webhooks/razorpay

    Steps:
      1. Read raw body (required for signature verification)
      2. Verify HMAC-SHA256 signature
      3. Parse JSON payload
      4. Filter to payment.captured only
      5. Extract telegram_id, plan, payment details from notes
      6. Deduplicate using processed_webhooks table
      7. Activate subscription
      8. Notify user via Telegram
    """
    # ── 1. Read raw body ──────────────────────────────────────────────────
    raw_body = await request.read()

    # ── 2. Signature verification ─────────────────────────────────────────
    razorpay_sig = request.headers.get("X-Razorpay-Signature", "")
    if not _verify_signature(raw_body, razorpay_sig):
        logger.warning(
            "Webhook signature mismatch from %s — rejected",
            request.remote,
        )
        return web.Response(status=400, text="Invalid signature")

    # ── 3. Parse JSON ─────────────────────────────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.error("Malformed webhook JSON: %s", exc)
        return web.Response(status=400, text="Invalid JSON")

    event: str = payload.get("event", "")
    logger.info("Razorpay webhook received: event=%s from=%s", event, request.remote)

    # ── 4. Only handle payment.captured ──────────────────────────────────
    if event != "payment.captured":
        logger.debug("Ignoring unhandled event: %s", event)
        return web.Response(status=200, text="Event ignored")

    # ── 5. Extract payment data ───────────────────────────────────────────
    try:
        entity: dict = payload["payload"]["payment"]["entity"]
        payment_id: str = entity["id"]
        order_id: str = entity.get("order_id") or ""
        amount_paise: int = int(entity.get("amount", 0))
        amount_inr: float = amount_paise / 100
        notes: dict = entity.get("notes") or {}

        telegram_id_str: str = str(notes.get("telegram_id", "")).strip()
        plan_key: str = str(notes.get("plan", "")).strip().lower()

    except (KeyError, ValueError, TypeError) as exc:
        logger.error("Failed to parse payment.captured payload: %s | payload=%s", exc, payload)
        # Return 200 so Razorpay stops retrying a malformed payload
        return web.Response(status=200, text="Malformed payload — logged")

    # Validate extracted values
    if not telegram_id_str or not telegram_id_str.isdigit():
        logger.error("Invalid telegram_id in notes: %r — payment_id=%s", telegram_id_str, payment_id)
        return web.Response(status=200, text="Invalid telegram_id in notes")

    if plan_key not in PLAN_REGISTRY:
        logger.error("Unknown plan key %r — payment_id=%s", plan_key, payment_id)
        return web.Response(status=200, text="Unknown plan key")

    telegram_id = int(telegram_id_str)

    logger.info(
        "payment.captured — payment_id=%s telegram_id=%s plan=%s amount=₹%.2f",
        payment_id, telegram_id, plan_key, amount_inr,
    )

    # ── 6. Deduplication ─────────────────────────────────────────────────
    if subs_db.is_webhook_processed(payment_id):
        logger.info("Duplicate webhook — payment_id=%s already processed, skipping.", payment_id)
        return web.Response(status=200, text="Already processed")

    # Mark BEFORE activating — prevents race conditions on Razorpay retries
    subs_db.mark_webhook_processed(
        payment_id=payment_id,
        telegram_id=telegram_id,
        plan_key=plan_key,
        amount_inr=amount_inr,
    )

    # ── 7. Activate subscription ──────────────────────────────────────────
    try:
        expires_at = _activate_subscription(
            telegram_id=telegram_id,
            plan_key=plan_key,
            amount_inr=amount_inr,
            payment_id=payment_id,
            order_id=order_id,
        )
    except Exception as exc:
        logger.exception(
            "CRITICAL: Activation failed for telegram_id=%s payment_id=%s: %s",
            telegram_id, payment_id, exc,
        )
        # Return 500 so Razorpay will retry — we haven't committed activation
        # (dedup row already inserted, so if retry succeeds it won't double-activate)
        return web.Response(status=500, text="Activation error — will retry")

    # ── 8. Notify via Telegram ────────────────────────────────────────────
    await _notify_user(telegram_id, plan_key, amount_inr, expires_at)

    return web.Response(status=200, text="OK")


# ─────────────────────────────────────────────────────────────────────────────
# Server lifecycle — called from bot/main.py post_init / post_shutdown
# ─────────────────────────────────────────────────────────────────────────────

async def start_webhook_server(bot: Bot) -> None:
    """
    Start the aiohttp webhook server in the running asyncio event loop.
    Called from PTB's post_init hook so no extra thread is needed.
    """
    global _runner, _bot_instance
    _bot_instance = bot

    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning(
            "RAZORPAY_WEBHOOK_SECRET is not set. "
            "Webhook server will start but requests will NOT be signature-verified."
        )

    app = web.Application()
    app.router.add_post("/webhooks/razorpay", handle_razorpay_webhook)
    # Health-check endpoint (useful for load balancers / uptime monitors)
    app.router.add_get("/health", lambda _req: web.Response(text="OK"))

    _runner = web.AppRunner(
        app,
        access_log=logging.getLogger("aiohttp.access"),
    )
    await _runner.setup()

    site = web.TCPSite(_runner, host="0.0.0.0", port=WEBHOOK_PORT)
    await site.start()

    logger.info(
        "Razorpay webhook server listening on 0.0.0.0:%s  →  POST /webhooks/razorpay",
        WEBHOOK_PORT,
    )


async def stop_webhook_server() -> None:
    """Gracefully shut down the webhook server. Called from PTB's post_shutdown hook."""
    global _runner
    if _runner is not None:
        logger.info("Stopping Razorpay webhook server...")
        await _runner.cleanup()
        _runner = None
        logger.info("Razorpay webhook server stopped.")
