"""
bot/handlers/help.py
Help menu and step-by-step tutorial handlers with Premium custom emojis.
"""

import logging
import os
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from bot.db.content import get_content

logger = logging.getLogger(__name__)

# Dynamically resolve the absolute path to the images folder
BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"

# Global dictionary to cache uploaded photo file_ids for instant performance
_PHOTO_CACHE = {}

# Premium Custom Emojis (HTML)
HSHAKE = '<tg-emoji emoji-id="5456371000239212004">🤝</tg-emoji>'
HEART = '<tg-emoji emoji-id="5285439518130857782">❤️</tg-emoji>'
THUMB = '<tg-emoji emoji-id="5413482938585063042">👍</tg-emoji>'
DIAMOND = '<tg-emoji emoji-id="5796205953913196373">💎</tg-emoji>'
CHECK = '<tg-emoji emoji-id="5217497254381754877">✅</tg-emoji>'
ARROW = '<tg-emoji emoji-id="5215720576735255650">➡️</tg-emoji>'

def get_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Link Account", callback_data="help_cmd:/howtoauth")],
        [InlineKeyboardButton("Set Forwarding", callback_data="help_cmd:/howtoaddforwarding")],
        [InlineKeyboardButton("Set Filters", callback_data="help_cmd:/howtosetfilter")],
        [InlineKeyboardButton("Schedule Messages", callback_data="help_cmd:/howtoschedule")],
        [InlineKeyboardButton("Premium Info", callback_data="help_cmd:/howtopro")],
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
                    media=InputMediaPhoto(media=file_id, caption=text, parse_mode="HTML"),
                    reply_markup=keyboard
                )
            else:
                await msg.reply_photo(photo=file_id, caption=text, parse_mode="HTML", reply_markup=keyboard)
        else:
            # Slow path: need to read from disk and upload
            if image_path.exists():
                with open(image_path, "rb") as f:
                    if query and msg and msg.photo:
                        res = await query.edit_message_media(
                            media=InputMediaPhoto(media=f, caption=text, parse_mode="HTML"),
                            reply_markup=keyboard
                        )
                        if res and hasattr(res, 'photo') and res.photo:
                            _PHOTO_CACHE[image_name] = res.photo[-1].file_id
                    else:
                        res = await msg.reply_photo(photo=f, caption=text, parse_mode="HTML", reply_markup=keyboard)
                        if res and hasattr(res, 'photo') and res.photo:
                            _PHOTO_CACHE[image_name] = res.photo[-1].file_id
            else:
                # Fallback if image doesn't exist on disk
                if query:
                    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard)
                else:
                    await msg.reply_text(text=text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Failed to send/edit help screen: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_text = (
        f"<b>Help & Tutorials</b>\n\n"
        f"Click the buttons below to see how it works {ARROW}"
    )
    text = get_content("help_msg", default_text)
    await _send_help_screen(update, "welcome.png", text)

async def howtoauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_text = (
        f"<b>How to Link Your Account</b>\n\n"
        f"{ARROW} Send the /authorize command.\n"
        f"{ARROW} Reply with your Telegram Phone Number (include country code, e.g. +91...).\n"
        f"{ARROW} Telegram will send you a 5-digit login code in your official Telegram app.\n"
        f"{ARROW} <b>IMPORTANT:</b> Send the code with a hyphen (e.g., <code>12-345</code>) to bypass Telegram's anti-bot security!\n"
        f"{ARROW} If you have 2-Step Verification enabled, it will ask for your password.\n\n"
        f"{CHECK} Once done, your account is linked and ready to forward!"
    )
    text = get_content("howtoauth", default_text)
    await _send_help_screen(update, "auth.png", text)

async def howtoaddforwarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_text = (
        f"<b>How to Set Up Forwarding</b>\n\n"
        f"{ARROW} Send the /addsource command and provide the channel username or ID from where you want to copy messages.\n"
        f"{ARROW} Send the /addtarget command and provide the channel username or ID to where you want the messages forwarded.\n"
        f"{ARROW} You can add multiple targets by repeating /addtarget.\n\n"
        f"{CHECK} The bot will now instantly copy any new message from your Source to all your Targets!"
    )
    text = get_content("howtoaddforwarding", default_text)
    await _send_help_screen(update, "set_forwarding.png", text)

async def howtosetfilter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_text = (
        f"<b>How to Set Filters</b>\n\n"
        f"Use /filter to change text or block messages entirely.\n\n"
        f"{ARROW} <b>Replace Links:</b> Choose 'Replace Any Link', provide your new link. Every link in the source message will become your link.\n"
        f"{ARROW} <b>Replace Usernames:</b> Choose 'Replace Any Username', provide your username (e.g., <code>@MyChannel</code>). All usernames will be replaced.\n"
        f"{ARROW} <b>Replace Specific Text:</b> Choose 'Replace Custom Text', provide the exact text to find, then the text to replace it with.\n"
        f"{ARROW} <b>Block Words:</b> Choose 'Block Message if Contains', provide a word. If the source message has this word, it will <b>not</b> be forwarded.\n\n"
        f"{THUMB} Use /myfilters to see or delete your active rules."
    )
    text = get_content("howtosetfilter", default_text)
    await _send_help_screen(update, "filter.png", text)

async def howtoschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_text = (
        f"<b>How to Schedule Messages</b>\n\n"
        f"{ARROW} Send /schedule.\n"
        f"{ARROW} The bot will ask for the message content. Send text, links, or media.\n"
        f"{ARROW} It will ask for the Time. You can use formats like <code>14:30</code>, <code>02:30 PM</code>, or even <code>02:30 PM IND</code> to use Indian time!\n"
        f"{ARROW} Choose if it should repeat 'Daily' or just run 'One-time'.\n\n"
        f"{CHECK} Use /removeschedule to cancel scheduled messages."
    )
    text = get_content("howtoschedule", default_text)
    await _send_help_screen(update, "schedule.png", text)

async def howtopro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_text = (
        f"<b>How to Get Premium</b>\n\n"
        f"{DIAMOND} Send /pro at any time to view the plans.\n"
        f"{ARROW} Select your preferred plan (1 Month, 3 Months, or 6 Months).\n"
        f"{ARROW} Choose your payment method (INR via UPI/Razorpay or USDT Crypto).\n"
        f"{ARROW} Complete the payment using the provided links or addresses.\n"
        f"{CHECK} Click \"I've Paid\" and contact support with your screenshot if required.\n\n"
        f"{HSHAKE} Premium unlocks all advanced features {HEART}"
    )
    text = get_content("howtopro", default_text)
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
