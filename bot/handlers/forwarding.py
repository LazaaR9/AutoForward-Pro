"""
bot/handlers/forwarding.py
Core forwarding engine — listens on source channels and copies messages
to all target channels, applying admin-defined text filters first.

How it works:
1. A MessageHandler catches all channel_post updates (messages posted in channels).
2. We look up which admins have this channel as their source.
3. For each such admin:
   a. Apply their text filters to the message text/caption.
   b. copy_message() to each of their target channels.
      (copy_message avoids the "Forwarded from" header and lets us inject
       a custom caption with filters applied.)

Supported types: text, photo, video, audio, document, sticker, animation,
                 voice, video_note, poll, contact, location, venue.
"""

from __future__ import annotations

import logging

from telegram import Message, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from bot.db.channels import get_admins_by_source_channel, get_target_channels
from bot.utils.filters import apply_filters, should_block_apk

logger = logging.getLogger(__name__)


async def forward_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Entry point for all channel posts.
    Called whenever a message is posted in any channel the bot is a member of.
    """
    msg: Message | None = update.channel_post
    if msg is None:
        return

    source_channel_id = msg.chat.id

    # Find all admins that have this channel as source
    admin_ids = get_admins_by_source_channel(source_channel_id)
    if not admin_ids:
        return  # This channel isn't a registered source for any admin

    from bot.utils import userbot_manager
    for admin_id in admin_ids:
        # Check if bot is stopped for this admin
        is_working = await userbot_manager.is_admin_working(admin_id)
        if not is_working:
            logger.debug("Skipping Bot API forwarding for admin %s since bot is stopped (/stop).", admin_id)
            continue

        # If userbot session is active, let it handle forwarding to prevent double posts
        if userbot_manager.is_userbot_authorized(admin_id):
            logger.debug("Skipping Bot API forwarding for admin %s since userbot is active.", admin_id)
            continue
        await _forward_for_admin(context, msg, admin_id)


async def _forward_for_admin(context, msg: Message, admin_id: int) -> None:
    """Apply filters and forward to all of an admin's target channels."""
    targets = get_target_channels(admin_id)
    if not targets:
        logger.debug("Admin %s has no target channels; skipping forward.", admin_id)
        return

    # Check for APK block
    if msg.document and msg.document.file_name and msg.document.file_name.lower().endswith('.apk'):
        if should_block_apk(admin_id):
            logger.debug("Blocking APK file for admin %s", admin_id)
            return

    # Determine filtered text / caption
    original_text = msg.text or None
    original_caption = msg.caption or None

    filtered_text = apply_filters(admin_id, original_text)
    filtered_caption = apply_filters(admin_id, original_caption)

    for target in targets:
        target_id = target["channel_id"]
        try:
            if msg.text:
                # Pure text message — send as new message so we can inject filtered text
                await context.bot.send_message(
                    chat_id=target_id,
                    text=filtered_text or msg.text,
                    entities=msg.entities if filtered_text == original_text else None,
                    parse_mode=None,
                )
            elif _has_media(msg):
                # Media with optional caption — use copy_message but override caption
                if filtered_caption != original_caption:
                    # Caption was modified by filters; send media separately with new caption
                    await _send_media_with_caption(context.bot, target_id, msg, filtered_caption)
                else:
                    # No filter changes — simple copy preserves formatting
                    await context.bot.copy_message(
                        chat_id=target_id,
                        from_chat_id=msg.chat.id,
                        message_id=msg.message_id,
                    )
            else:
                # Fallback: copy as-is (poll, contact, location, venue, etc.)
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                )

        except Exception as exc:
            logger.error(
                "Failed to forward msg %s from channel %s to %s (admin %s): %s",
                msg.message_id, msg.chat.id, target_id, admin_id, exc,
            )


async def _send_media_with_caption(bot, target_id: int, msg: Message, caption: str | None) -> None:
    """
    Re-send a media message to a target channel with an overridden caption.
    Handles each media type individually to support caption replacement.
    """
    kwargs = {
        "chat_id": target_id,
        "caption": caption,
        "parse_mode": None,
    }

    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id  # Largest resolution
            await bot.send_photo(photo=file_id, **kwargs)
        elif msg.video:
            await bot.send_video(video=msg.video.file_id, **kwargs)
        elif msg.audio:
            await bot.send_audio(audio=msg.audio.file_id, **kwargs)
        elif msg.document:
            await bot.send_document(document=msg.document.file_id, **kwargs)
        elif msg.animation:
            await bot.send_animation(animation=msg.animation.file_id, **kwargs)
        elif msg.voice:
            await bot.send_voice(voice=msg.voice.file_id, **kwargs)
        elif msg.video_note:
            # video_notes don't support captions
            await bot.send_video_note(video_note=msg.video_note.file_id, chat_id=target_id)
        elif msg.sticker:
            # stickers don't support captions
            await bot.send_sticker(sticker=msg.sticker.file_id, chat_id=target_id)
        else:
            # Unknown media — copy as-is
            await bot.copy_message(
                chat_id=target_id,
                from_chat_id=msg.chat.id,
                message_id=msg.message_id,
            )
    except Exception as exc:
        logger.error("_send_media_with_caption failed for target %s: %s", target_id, exc)


def _has_media(msg: Message) -> bool:
    """Return True if the message contains any media type."""
    return bool(
        msg.photo
        or msg.video
        or msg.audio
        or msg.document
        or msg.animation
        or msg.sticker
        or msg.voice
        or msg.video_note
    )


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def register(application) -> None:
    """
    Register the channel post forwarding handler.
    Must be registered LAST so ConversationHandlers take priority for DM commands.
    """
    application.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.UpdateType.CHANNEL_POST,
            forward_channel_post,
        ),
        group=1,  # Lower priority group — runs after all DM conversation handlers
    )
