"""
bot/db/content.py
Helper functions for getting and setting dynamic text content.
"""
import logging
from bot.db.supabase_client import get_client

logger = logging.getLogger(__name__)

# In-memory cache for fast retrieval (so we don't hit the DB on every message)
_CONTENT_CACHE = {}

def get_content(key: str, default_value: str) -> str:
    """
    Get text content for a specific key.
    Checks memory cache first, then Supabase. If not found, returns default_value.
    """
    if key in _CONTENT_CACHE:
        return _CONTENT_CACHE[key]
        
    client = get_client()
    try:
        response = client.table("bot_content").select("content_value").eq("content_key", key).execute()
        if response.data:
            val = response.data[0]["content_value"]
            _CONTENT_CACHE[key] = val
            return val
    except Exception as e:
        logger.error(f"Error fetching bot_content for {key}: {e}")
        
    return default_value

def set_content(key: str, new_value: str) -> bool:
    """
    Save new text content to Supabase and update the local cache.
    """
    client = get_client()
    try:
        client.table("bot_content").upsert({"content_key": key, "content_value": new_value}).execute()
        _CONTENT_CACHE[key] = new_value
        return True
    except Exception as e:
        logger.error(f"Error updating bot_content for {key}: {e}")
        return False
