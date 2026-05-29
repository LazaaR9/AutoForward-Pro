"""
bot/db/transactions.py
Database operations for the `transactions` table.
"""

from __future__ import annotations

from datetime import datetime

from bot.db.supabase_client import get_client


def log_transaction(
    admin_id: int,
    amount: float,
    duration_days: int,
    promoted_at: datetime,
    expires_at: datetime,
) -> dict:
    """Insert a transaction record and return it."""
    db = get_client()
    res = db.table("transactions").insert({
        "admin_id": admin_id,
        "amount": amount,
        "duration_days": duration_days,
        "promoted_at": promoted_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }).execute()
    return res.data[0] if res.data else {}


def get_total_income() -> float:
    """Sum all transaction amounts."""
    db = get_client()
    res = db.table("transactions").select("amount").execute()
    return sum(float(row["amount"]) for row in (res.data or []))


def get_total_transactions() -> int:
    """Return total number of transactions."""
    db = get_client()
    res = db.table("transactions").select("id", count="exact").execute()
    return res.count or 0


def get_admin_transactions(admin_id: int) -> list[dict]:
    """Return all transactions for a specific admin."""
    db = get_client()
    res = (
        db.table("transactions")
        .select("*")
        .eq("admin_id", admin_id)
        .order("promoted_at", desc=True)
        .execute()
    )
    return res.data or []
