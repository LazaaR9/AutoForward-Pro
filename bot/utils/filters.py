"""
bot/utils/filters.py
Text filter application logic — applies admin-defined find/replace rules
to message text or media captions before forwarding.
"""

from __future__ import annotations

import re
from bot.db.filters import get_filters


def apply_filters(admin_id: int, text: str | None) -> str | None:
    """
    Apply all text replacement filters for `admin_id` to `text`.
    Returns the modified text, or None if original was None.
    """
    if text is None:
        return None

    rules = get_filters(admin_id)
    
    # Process regex wildcard rules first
    for rule in rules:
        find = rule["find_text"]
        replace = rule["replace_text"]
        if find == "<ALL_LINKS>":
            # Match standard URLs, www, and t.me links
            text = re.sub(r'https?://\S+|www\.\S+|t\.me/\S+', replace, text)
        elif find == "<ALL_USERNAMES>":
            # Match Telegram usernames starting with @
            text = re.sub(r'@[a-zA-Z0-9_]+', replace, text)

    # Process standard exact match rules
    for rule in rules:
        find = rule["find_text"]
        if find not in ("<ALL_LINKS>", "<ALL_USERNAMES>"):
            text = text.replace(find, rule["replace_text"])

    return text
