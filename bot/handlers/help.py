"""
bot/handlers/help.py
Help menu and step-by-step tutorial handlers.
"""

import logging
import os
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler

logger = logging.getLogger(__name__)

# Dynamically resolve the absolute path to the images folder
BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"

# Global dictionary to cache uploaded photo file_ids for instant performance
_PHOTO_CACHE = {}

def get_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("[1] Link Account", callback_data="help_cmd:/howtoauth")],
        [InlineKeyboardButton("[2] Set Forwarding", callback_data="help_cmd:/howtoaddforwarding")],
        [InlineKeyboardButton("[3] Set Filters", callback_data="help_cmd:/howtosetfilter")],
        [InlineKeyboardButton("[4] Schedule Messages", callback_data="help_cmd:/howtoschedule")],
        [InlineKeyboardButton("[5] Premium Info", callback_data="help_cmd:/howtopro")],
    ])

async def _send_help_screen(update: Update, image_name: str, text: str) -> None:
    query = update.callback_query
    msg = update.effective_message
    keyboard = get_help_keyboard()
    image_path = IMAGES_DIR / image_name
    
    file_id = _PHOTO_CACHE.get(image_name)

    try:
        if file_id:
            # Fast path: use cached file_id
            if query and msg and msg.photo:
                # We are in a callback query from a previous photo message, so edit the media!
                await query.edit_message_media(
                    media=InputMediaPhoto(media=file_id, caption=text, parse_mode="Markdown"),
                    reply_markup=keyboard
                )
            else:
                await msg.reply_photo(photo=file_id, caption=text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            # Slow path: need to read from disk and upload
            if image_path.exists():
                with open(image_path, "rb") as f:
                    if query and msg and msg.photo:
                        res = await query.edit_message_media(
                            media=InputMediaPhoto(media=f, caption=text, parse_mode="Markdown"),
                            reply_markup=keyboard
                        )
                        if res and hasattr(res, 'photo') and res.photo:
                            _PHOTO_CACHE[image_name] = res.photo[-1].file_id
                    else:
                        res = await msg.reply_photo(photo=f, caption=text, parse_mode="Markdown", reply_markup=keyboard)
                        if res and hasattr(res, 'photo') and res.photo:
                            _PHOTO_CACHE[image_name] = res.photo[-1].file_id
            else:
                # Fallback if image doesn't exist on disk
                if query:
                    await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=keyboard)
                else:
                    await msg.reply_text(text=text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Failed to send/edit help screen: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*Help & Tutorials*\n\n"
        "Click the buttons below to see how it works:"
    )
    await _send_help_screen(update, "welcome.png", text)

async def howtoauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*How to Link Your Account*\n\n"
        "[1] Send the /authorize command.\n"
        "[2] Reply with your Telegram Phone Number (include country code, e.g. +91...).\n"
        "[3] Telegram will send you a 5-digit login code in your official Telegram app.\n"
        "[4] *IMPORTANT:* Send the code with a hyphen (e.g., `12-345`) to bypass Telegram's anti-bot security!\n"
        "[5] If you have 2-Step Verification enabled, it will ask for your password.\n\n"
        "- Once done, your account is linked and ready to forward!"
    )
    await _send_help_screen(update, "auth.png", text)

async def howtoaddforwarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*How to Set Up Forwarding*\n\n"
        "[1] Send the /addsource command and provide the channel username or ID from where you want to copy messages.\n"
        "[2] Send the /addtarget command and provide the channel username or ID to where you want the messages forwarded.\n"
        "[3] You can add multiple targets by repeating /addtarget.\n\n"
        "- The bot will now instantly copy any new message from your Source to all your Targets!"
    )
    await _send_help_screen(update, "set_forwarding.png", text)

async def howtosetfilter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*How to Set Filters*\n\n"
        "Use /filter to change text or block messages entirely.\n\n"
        "[1] *Replace Links:* Choose 'Replace Any Link', provide your new link. Every link in the source message will become your link.\n"
        "[2] *Replace Usernames:* Choose 'Replace Any Username', provide your username (e.g., `@MyChannel`). All usernames will be replaced.\n"
        "[3] *Replace Specific Text:* Choose 'Replace Custom Text', provide the exact text to find, then the text to replace it with.\n"
        "[4] *Block Words:* Choose 'Block Message if Contains', provide a word. If the source message has this word, it will *not* be forwarded.\n\n"
        "Use /myfilters to see or delete your active rules."
    )
    await _send_help_screen(update, "filter.png", text)

async def howtoschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*How to Schedule Messages*\n\n"
        "[1] Send /schedule.\n"
        "[2] The bot will ask for the message content. Send text, links, or media.\n"
        "[3] It will ask for the Time. You can use formats like `14:30`, `02:30 PM`, or even `02:30 PM IND` to use Indian time!\n"
        "[4] Choose if it should repeat 'Daily' or just run 'One-time'.\n\n"
        "Use /removeschedule to cancel scheduled messages."
    )
    await _send_help_screen(update, "schedule.png", text)

async def howtopro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*How to Get Premium*\n\n"
        "[1] Send /pro at any time to view the plans.\n"
        "[2] Select your preferred plan (1 Month, 3 Months, or 6 Months).\n"
        "[3] Choose your payment method (INR via UPI/Razorpay or USDT Crypto).\n"
        "[4] Complete the payment using the provided links or addresses.\n"
        "[5] Click \"I've Paid\" and contact support with your screenshot if required.\n\n"
        "Premium unlocks all advanced features!"
    )
    await _send_help_screen(update, "pro.png", text)

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
