"""
bot/utils/filters.py
Text filter application logic — applies admin-defined find/replace rules
to message text or media captions before forwarding.
"""

from __future__ import annotations

from bot.db.filters import get_filters


def apply_filters(admin_id: int, text: str | None) -> str | None:
    """
    Apply all text replacement filters for `admin_id` to `text`.
    Returns the modified text, or None if original was None.
    """
    if text is None:
        return None

    rules = get_filters(admin_id)
    for rule in rules:
        text = text.replace(rule["find_text"], rule["replace_text"])

    return text
