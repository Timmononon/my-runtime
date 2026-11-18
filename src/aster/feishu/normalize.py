"""Validate the flat message projection from the official CLI event stream."""
from __future__ import annotations

import re

EVENT_TYPE = "im.message.receive_v1"
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")
_ID_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,92}")
# Bound the complete message passed to task triage. Oversize messages are
# rejected as a whole so no trailing request or correction can be lost.
MAX_TEXT_CHARS = 12000


def remote_id(value, prefix):
    return (isinstance(value, str) and value.startswith(prefix)
            and _ID_SUFFIX.fullmatch(value[len(prefix):]) is not None)


def valid_text(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and bool(value.strip()) and not _CONTROLS.search(value))


def group_message(raw):
    if not isinstance(raw, dict) or raw.get("type") != EVENT_TYPE or raw.get("chat_type") != "group":
        return None
    chat_id, message_id = raw.get("chat_id"), raw.get("message_id")
    if not remote_id(chat_id, "oc_") or not remote_id(message_id, "om_"):
        return None
    if "id" in raw and raw["id"] != message_id:
        return None
    return chat_id, message_id


def _created_at(raw):
    value = raw.get("create_time")
    if type(value) is int:
        return value / 1000 if 0 < value <= 9999999999999999 else None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
        return None
    return int(value) / 1000


def assess(raw, *, bot_open_id, allowed_senders, required_keywords, active_since, received_at):
    created_at = _created_at(raw)
    if created_at is None or not active_since <= created_at <= received_at + 5:
        return "outside_activity_window", None
    if raw.get("message_type") != "text":
        return "non_text", None
    if raw.get("sender_type") != "user" or raw.get("sender_id") == bot_open_id:
        return "bot_sender", None
    if raw.get("sender_id") not in allowed_senders:
        return "sender_not_allowed", None
    mentions = raw.get("mentions")
    if not isinstance(mentions, list) or not any(isinstance(entry, dict) and entry.get("id") == bot_open_id
                                                 for entry in mentions):
        return "bot_not_mentioned", None
    text = raw.get("content")
    if not valid_text(text, MAX_TEXT_CHARS):
        return "invalid_content", None
    folded = text.casefold()
    if required_keywords and not any(term.casefold() in folded for term in required_keywords):
        return "keyword_not_matched", None
    return None, text.strip()
