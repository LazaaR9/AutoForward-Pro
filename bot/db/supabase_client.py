"""
bot/db/supabase_client.py
Singleton Supabase client shared across all DB modules.
"""

from supabase import create_client, Client
from bot.config import SUPABASE_URL, SUPABASE_KEY

_client: Client | None = None


def get_client() -> Client:
    """Return the singleton Supabase client, creating it on first call."""
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client
