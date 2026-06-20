"""Detect which BlueBubbles capabilities are available and adapt the toolset.

BlueBubbles can talk to Messages two ways:

* **AppleScript** — automates Messages.app from the outside. Works on any
  install with no extra setup, but only covers the basics: send a text, send an
  attachment.
* **Private API** — injects a helper into Apple's private iMessage frameworks.
  Requires partially disabling SIP and installing the BlueBubbles Helper, and in
  return unlocks tapbacks, edit/unsend, typing indicators, read receipts, group
  management, threaded replies and SMS sending.

This module decides whether the Private API is available — either from an
explicit ``BLUEBUBBLES_PRIVATE_API`` override or by introspecting
``/server/info`` — so the server can (a) pick the right send ``method`` and
(b) hide the tools that would only ever error without it.
"""

from __future__ import annotations

from typing import Any

# Tools that require the BlueBubbles Private API and have *no* AppleScript
# fallback. When the Private API is unavailable these are removed from the
# advertised toolset, so a client never sees a tool that can only 500.
#
# Plain sends (`send_message`, `send_message_to_address`, `send_attachment`) are
# deliberately absent: they degrade to the AppleScript `method` instead of being
# removed. See `bb_mcp.server`.
PRIVATE_API_TOOLS: tuple[str, ...] = (
    "send_reaction",      # tapbacks
    "edit_message",
    "unsend_message",
    "start_typing",
    "stop_typing",
    "mark_chat_read",     # sends a real read receipt
    "mark_chat_unread",   # server gates /unread behind the Private API too
    "rename_group",
    "add_participant",
    "remove_participant",
    "leave_chat",
    "create_group_chat",  # AppleScript can't create groups on Big Sur+
    "check_imessage",     # handle availability lookups go through the Private API
    "check_facetime",
    "send_multipart",     # needs /attachment/upload (Private API)
    "delete_message",     # ChatRouter.deleteChatMessage is Private API
    "set_group_icon",
    "remove_group_icon",
    "find_my_friends",    # server gates findmy/friends (devices is not gated)
)

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def parse_override(value: str | None) -> bool | None:
    """Interpret the ``BLUEBUBBLES_PRIVATE_API`` env var.

    Returns ``True``/``False`` when the value forces a decision, or ``None`` for
    ``auto`` (the default), an empty value, or anything unrecognized — in which
    case the caller should introspect the server instead.
    """
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


# `/server/info` fields that carry the owning account's own handle, best first.
# `detected_imessage` is the iMessage handle when the server reports it;
# `detected_icloud` is the signed-in Apple ID email (present since server v1.1.0).
_MY_ADDRESS_FIELDS = ("detected_imessage", "detected_icloud")


def my_address_from_info(info: dict[str, Any] | None) -> str | None:
    """Extract the owning account's own iMessage address from ``/server/info``."""
    if not info:
        return None
    for field in _MY_ADDRESS_FIELDS:
        value = info.get(field)
        if value:
            return str(value)
    return None


def private_api_from_info(info: dict[str, Any] | None) -> bool:
    """Decide Private API availability from a ``/server/info`` payload.

    Conservative about *removing* tools: we only conclude "unavailable" when the
    server explicitly reports the Private API off (or the helper disconnected).
    If the call failed (``info is None``) or the server didn't report the flag at
    all, we preserve the unrestricted default rather than prune on a guess.
    """
    if not info:
        return True
    if "private_api" not in info:
        return True
    if not info["private_api"]:
        return False
    # `private_api` is enabled in settings; only treat as unavailable if the
    # helper is *explicitly* reported as disconnected. A missing field == trust.
    return info.get("helper_connected") is not False
