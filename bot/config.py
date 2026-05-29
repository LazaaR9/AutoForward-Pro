"""
config.py — Central configuration loader.
Reads all secrets from .env and exposes them as typed constants.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot ──────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# ── Supabase ─────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]

# ── Role IDs ─────────────────────────────────────────────────────────────────
SUPER_ADMIN_ID: int = int(os.environ["SUPER_ADMIN_ID"])

# ── Trial duration ────────────────────────────────────────────────────────────
TRIAL_DAYS: int = 3

# ── Telethon API Credentials ──────────────────────────────────────────────────
TELEGRAM_API_ID: int = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
