"""
bot/main.py
Bot entry point — wires everything together and starts polling.

Startup sequence:
1. Acquire PID lock (kills any stale duplicate instance automatically)
2. Build the PTB Application
3. Register all handlers (superadmin → admin → user → forwarding)
4. On post_init: start APScheduler, bootstrap DB schedules, register expiry checker
5. Run polling
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys

# Ensure the parent directory is in sys.path to allow absolute imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram.ext import Application

from bot.config import BOT_TOKEN
from bot.handlers import admin, forwarding, superadmin, user, payment, help
from bot.utils import userbot_manager
from bot.utils.scheduler import (
    bootstrap_schedules,
    start_expiry_checker,
    start_scheduler,
)
from bot.webhook.razorpay_webhook import start_webhook_server, stop_webhook_server

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # Suppress noisy HTTP logs
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PID lock — prevents two bot instances from running simultaneously.
# A second "Conflict: terminated by other getUpdates request" can never occur.
# ─────────────────────────────────────────────────────────────────────────────
_PID_FILE = os.path.join(os.path.dirname(__file__), "..", ".bot.pid")


def _acquire_pid_lock() -> None:
    """Write our PID to .bot.pid, killing any stale previous instance first."""
    pid_file = os.path.abspath(_PID_FILE)
    if os.path.exists(pid_file):
        try:
            old_pid = int(open(pid_file).read().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.warning("Killed stale bot instance PID %s", old_pid)
            import time; time.sleep(1)  # Give it a moment to die
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # PID file stale or process already dead
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(pid_file) and os.remove(pid_file))


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Post-init hook (must be defined before build_application)
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """
    Called by PTB after the Application is initialized but before polling starts.
    Safe to use async here because the event loop is already running.
    In PTB v21+, this is registered via .post_init() on the builder.
    """
    bot = application.bot

    # Start APScheduler (uses the running asyncio event loop)
    start_scheduler()

    # Reload all saved scheduled messages from DB into APScheduler
    bootstrap_schedules(bot)

    # Register hourly subscription expiry checker
    start_expiry_checker(bot)

    # Start all active background Telethon sessions
    await userbot_manager.start_all_userbots(bot)

    # Start Razorpay webhook HTTP server (same event loop as the bot)
    await start_webhook_server(bot)

    # Log bot info
    me = await bot.get_me()
    logger.info("Bot started: @%s (ID: %s)", me.username, me.id)
    logger.info("All handlers registered. Polling...")


async def post_shutdown(application: Application) -> None:
    """Called during application shutdown. Gracefully stop all background services."""
    logger.info("Stopping all background userbot sessions...")
    await userbot_manager.stop_all_userbots()
    await stop_webhook_server()


# ─────────────────────────────────────────────────────────────────────────────
# Global Error Handler
# ─────────────────────────────────────────────────────────────────────────────

async def global_error_handler(update, context) -> None:
    """Log errors globally and prevent the bot from crashing."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    # If it's a network timeout, we can silently ignore it as PTB will retry
    from telegram.error import TimedOut, NetworkError
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning("Network timeout occurred, ignoring to keep bot alive.")


def build_application() -> Application:
    # PTB v21+: post_init is passed into the builder chain, not set after build()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register global error handler to catch timeouts
    app.add_error_handler(global_error_handler)

    # Register handlers in priority order:
    # 1. Super Admin  (most restrictive — checked first by PTB)
    superadmin.register(app)

    # 2. Admin handlers
    admin.register(app)

    # 3. User handlers (/start, /plan)
    user.register(app)

    # 4. Payment handlers (/pro, plan selection, INR, USDT)
    payment.register(app)

    # 4.5. Help handlers
    help.register(app)

    # 5. Forwarding (group=1 — lower priority, channel posts only)
    forwarding.register(app)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    _acquire_pid_lock()  # Kill any stale duplicate instance before starting
    app = build_application()

    logger.info("Starting Telegram forwarding bot...")
    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "callback_query",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
