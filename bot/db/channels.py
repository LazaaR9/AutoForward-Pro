"""
bot/db/channels.py
Database operations for source_channels and target_channels tables.
"""

from __future__ import annotations

from typing import Optional

from bot.db.supabase_client import get_client


# ─────────────────────────────────────────────────────────────────────────────
# Source Channels
# ─────────────────────────────────────────────────────────────────────────────

def get_source_channel(admin_id: int) -> Optional[dict]:
    """Return the source channel for an admin, or None."""
    db = get_client()
    res = db.table("source_channels").select("*").eq("added_by", admin_id).execute()
    return res.data[0] if res.data else None


def set_source_channel(admin_id: int, channel_id: int, channel_username: Optional[str]) -> None:
    """
    Upsert the source channel for an admin.
    Each admin can have exactly one source channel (enforced by UNIQUE constraint).
    """
    db = get_client()

    existing = get_source_channel(admin_id)
    payload = {
        "channel_id": channel_id,
        "channel_username": channel_username,
        "added_by": admin_id,
    }

    if existing:
        db.table("source_channels").update(payload).eq("added_by", admin_id).execute()
    else:
        db.table("source_channels").insert(payload).execute()


def remove_source_channel(admin_id: int) -> bool:
    """Remove the source channel for an admin. Returns True if something was deleted."""
    db = get_client()
    existing = get_source_channel(admin_id)
    if not existing:
        return False
    db.table("source_channels").delete().eq("added_by", admin_id).execute()
    return True


def get_all_source_channels() -> list[dict]:
    """Return all source channels (for Super Admin /allchannels)."""
    db = get_client()
    res = db.table("source_channels").select("*").execute()
    return res.data or []


# ─────────────────────────────────────────────────────────────────────────────
# Target Channels
# ─────────────────────────────────────────────────────────────────────────────

def get_target_channels(admin_id: int) -> list[dict]:
    """Return all target channels for an admin."""
    db = get_client()
    res = db.table("target_channels").select("*").eq("admin_id", admin_id).execute()
    return res.data or []


def add_target_channel(admin_id: int, channel_id: int, channel_username: Optional[str]) -> bool:
    """
    Add a target channel for an admin.
    Returns False if already exists (duplicate silently ignored).
    """
    db = get_client()
    # Check duplicate
    res = (
        db.table("target_channels")
        .select("id")
        .eq("admin_id", admin_id)
        .eq("channel_id", channel_id)
        .execute()
    )
    if res.data:
        return False

    db.table("target_channels").insert({
        "channel_id": channel_id,
        "channel_username": channel_username,
        "admin_id": admin_id,
    }).execute()
    return True


def remove_target_channel(admin_id: int, channel_id: int) -> bool:
    """Remove a specific target channel for an admin. Returns True if deleted."""
    db = get_client()
    res = (
        db.table("target_channels")
        .select("id")
        .eq("admin_id", admin_id)
        .eq("channel_id", channel_id)
        .execute()
    )
    if not res.data:
        return False
    db.table("target_channels").delete().eq("admin_id", admin_id).eq("channel_id", channel_id).execute()
    return True


def get_all_target_channels() -> list[dict]:
    """Return all target channels across all admins (for Super Admin /allchannels)."""
    db = get_client()
    res = db.table("target_channels").select("*").execute()
    return res.data or []


def get_all_channels_count() -> int:
    """Total distinct channels (source + target) for /stats."""
    db = get_client()
    src = db.table("source_channels").select("id", count="exact").execute().count or 0
    tgt = db.table("target_channels").select("id", count="exact").execute().count or 0
    return src + tgt


# ─────────────────────────────────────────────────────────────────────────────
# Cross-admin lookup: map source channel_id → list of admin_ids
# ─────────────────────────────────────────────────────────────────────────────

def get_admins_by_source_channel(channel_id: int) -> list[int]:
    """
    Given a channel_id, return all admin_ids that have this channel as their source.
    Used by the forwarding handler to dispatch messages.
    """
    db = get_client()
    res = (
        db.table("source_channels")
        .select("added_by")
        .eq("channel_id", channel_id)
        .execute()
    )
    return [row["added_by"] for row in (res.data or [])]
