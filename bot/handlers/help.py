"""
bot/handlers/help.py
Help menu and step-by-step tutorial handlers.
"""

import logging
import os
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler

logger = logging.getLogger(__name__)

# Dynamically resolve the absolute path to the images folder
BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📚 *Help & Tutorials*\n\n"
        "Click the buttons below to see how it works:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Link Account", callback_data="help_cmd:/howtoauth")],
        [InlineKeyboardButton("📡 Set Forwarding", callback_data="help_cmd:/howtoaddforwarding")],
        [InlineKeyboardButton("🔧 Set Filters", callback_data="help_cmd:/howtosetfilter")],
        [InlineKeyboardButton("⏰ Schedule Messages", callback_data="help_cmd:/howtoschedule")],
        [InlineKeyboardButton("💎 Premium Info", callback_data="help_cmd:/howtopro")],
    ])
    
    image_path = IMAGES_DIR / "welcome.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.effective_message.reply_photo(
                photo=photo,
                caption=text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def howtoauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🔑 *How to Link Your Account*\n\n"
        "1️⃣ Send the /authorize command.\n"
        "2️⃣ Reply with your Telegram Phone Number (include country code, e.g. +91...).\n"
        "3️⃣ Telegram will send you a 5-digit login code in your official Telegram app.\n"
        "4️⃣ *IMPORTANT:* Send the code with a hyphen (e.g., `12-345`) to bypass Telegram's anti-bot security!\n"
        "5️⃣ If you have 2-Step Verification enabled, it will ask for your password.\n\n"
        "✅ Once done, your account is linked and ready to forward!"
    )
    image_path = IMAGES_DIR / "auth.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.effective_message.reply_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown")

async def howtoaddforwarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📡 *How to Set Up Forwarding*\n\n"
        "1️⃣ Send the /addsource command and provide the channel username or ID from where you want to copy messages.\n"
        "2️⃣ Send the /addtarget command and provide the channel username or ID to where you want the messages forwarded.\n"
        "3️⃣ You can add multiple targets by repeating /addtarget.\n\n"
        "✅ The bot will now instantly copy any new message from your Source to all your Targets!"
    )
    image_path = IMAGES_DIR / "set_forwarding.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.effective_message.reply_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown")

async def howtosetfilter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🔧 *How to Set Filters*\n\n"
        "Use /filter to change text or block messages entirely.\n\n"
        "1️⃣ *Replace Links:* Choose `🔗 Replace Any Link`, provide your new link. Every link in the source message will become your link.\n"
        "2️⃣ *Replace Usernames:* Choose `👤 Replace Any Username`, provide your username (e.g., `@MyChannel`). All usernames will be replaced.\n"
        "3️⃣ *Replace Specific Text:* Choose `✏️ Replace Custom Text`, provide the exact text to find, then the text to replace it with.\n"
        "4️⃣ *Block Words:* Choose `🚫 Block Message if Contains`, provide a word. If the source message has this word, it will *not* be forwarded.\n\n"
        "Use /myfilters to see or delete your active rules."
    )
    image_path = IMAGES_DIR / "filter.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.effective_message.reply_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown")

async def howtoschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "⏰ *How to Schedule Messages*\n\n"
        "1️⃣ Send /schedule.\n"
        "2️⃣ The bot will ask for the message content. Send text, links, or media.\n"
        "3️⃣ It will ask for the Time. You can use formats like `14:30`, `02:30 PM`, or even `02:30 PM IND` to use Indian time!\n"
        "4️⃣ Choose if it should repeat `🔂 Daily` or just run `1️⃣ One-time`.\n\n"
        "Use /removeschedule to cancel scheduled messages."
    )
    image_path = IMAGES_DIR / "schedule.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.effective_message.reply_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown")

async def howtopro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "💎 *How to Get Premium*\n\n"
        "1️⃣ Send /pro at any time to view the plans.\n"
        "2️⃣ Select your preferred plan (1 Month, 3 Months, or 6 Months).\n"
        "3️⃣ Choose your payment method (INR via UPI/Razorpay or USDT Crypto).\n"
        "4️⃣ Complete the payment using the provided links or addresses.\n"
        "5️⃣ Click `✅ I've Paid` and contact support with your screenshot if required.\n\n"
        "Premium unlocks all advanced features!"
    )
    image_path = IMAGES_DIR / "pro.png"
    if image_path.exists():
        with open(image_path, "rb") as photo:
            await update.effective_message.reply_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown")

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    cmd = query.data.split(":", 1)[1]
    
    if cmd == "/howtoauth":
        await howtoauth_command(update, context)
    elif cmd == "/howtoaddforwarding":
        await howtoaddforwarding_command(update, context)
    elif cmd == "/howtosetfilter":
        await howtosetfilter_command(update, context)
    elif cmd == "/howtoschedule":
        await howtoschedule_command(update, context)
    elif cmd == "/howtopro":
        await howtopro_command(update, context)

def register(application) -> None:
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("howtoauth", howtoauth_command))
    application.add_handler(CommandHandler("howtoaddforwarding", howtoaddforwarding_command))
    application.add_handler(CommandHandler("howtosetfilter", howtosetfilter_command))
    application.add_handler(CommandHandler("howtoschedule", howtoschedule_command))
    application.add_handler(CommandHandler("howtopro", howtopro_command))
    application.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help_cmd:"))
