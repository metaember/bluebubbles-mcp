"""Compact message projection and sender filtering.

BlueBubbles returns very large raw message objects (delivery flags, itemType,
group-action metadata, attributedBody, …). For an assistant reading many
messages that's a lot of wasted tokens, so reads project down to a compact set
by default; pass ``extended=True`` on a tool to get the full raw objects.

``from_address`` filtering keys off a message's *true sender* — which is the
local user when ``isFromMe`` is set, otherwise the message's ``handle`` (the
sender in group chats; the other party in a 1:1). That's computed here rather
than via a server-side query so it's correct for both "from me" and "from X".
"""

from __future__ import annotations

from typing import Any, Callable

_COMPACT_MESSAGE_FIELDS = (
    "guid",
    "text",
    "subject",
    "isFromMe",
    "dateCreated",
    "dateDelivered",
    "dateRead",
)
_COMPACT_ATTACHMENT_FIELDS = ("guid", "transferName", "mimeType", "totalBytes")
_COMPACT_CHAT_FIELDS = ("guid", "displayName")


def _is_message(node: dict[str, Any]) -> bool:
    """Heuristic: a BlueBubbles message object always has these two keys."""
    return "guid" in node and "isFromMe" in node


def compact_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw message to the fields an assistant actually needs."""
    out: dict[str, Any] = {
        k: msg[k] for k in _COMPACT_MESSAGE_FIELDS if msg.get(k) is not None
    }
    handle = msg.get("handle")
    if isinstance(handle, dict) and handle.get("address"):
        compact_handle = {"address": handle["address"]}
        if handle.get("contactName"):  # added by contact enrichment
            compact_handle["contactName"] = handle["contactName"]
        out["handle"] = compact_handle
    attachments = msg.get("attachments")
    if attachments:
        out["attachments"] = [
            {k: a[k] for k in _COMPACT_ATTACHMENT_FIELDS if isinstance(a, dict) and a.get(k) is not None}
            for a in attachments
        ]
    chats = msg.get("chats")
    if chats:  # keep which chat a search result belongs to
        out["chats"] = [
            {k: c[k] for k in _COMPACT_CHAT_FIELDS if isinstance(c, dict) and c.get(k)}
            for c in chats
        ]
    for key in ("associatedMessageGuid", "associatedMessageType"):
        if msg.get(key) is not None:  # tapback / reply linkage
            out[key] = msg[key]
    return out


def project(node: Any, extended: bool) -> Any:
    """Walk a response and compact every message object, unless ``extended``.

    Non-message structures (chats, the get_unread_chats wrapper) are preserved;
    only the message objects within them are compacted, including a chat's
    embedded ``lastMessage``.
    """
    if extended:
        return node
    if isinstance(node, list):
        return [project(n, extended) for n in node]
    if isinstance(node, dict):
        if _is_message(node):
            return compact_message(node)
        return {k: project(v, extended) for k, v in node.items()}
    return node


def message_sender(msg: dict[str, Any], my_address: str | None) -> str | None:
    """The address that sent ``msg`` (the local user if ``isFromMe``)."""
    if msg.get("isFromMe"):
        return my_address
    handle = msg.get("handle")
    if isinstance(handle, dict):
        return handle.get("address")
    return None


def filter_by_sender(
    messages: list[Any],
    target_normalized: str,
    my_address: str | None,
    normalize: Callable[[str], str],
) -> list[Any]:
    """Keep only messages whose sender normalizes to ``target_normalized``."""
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sender = message_sender(msg, my_address)
        if sender and normalize(sender) == target_normalized:
            out.append(msg)
    return out
