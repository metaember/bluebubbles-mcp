"""MCP server for BlueBubbles iMessage bridge."""

from __future__ import annotations

import base64
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from bb_mcp.client import BlueBubblesClient, BlueBubblesError

# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SEND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

# ---------------------------------------------------------------------------
# Lifespan: create/destroy the shared BlueBubbles client
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP):
    url = os.environ.get("BLUEBUBBLES_URL")
    password = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not password:
        raise RuntimeError(
            "BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD environment variables are required"
        )
    client = BlueBubblesClient(url, password)
    try:
        yield {"bb": client}
    finally:
        await client.close()


mcp = FastMCP(
    "BlueBubbles",
    instructions="iMessage bridge via BlueBubbles",
    lifespan=lifespan,
)


def _bb(ctx: Context) -> BlueBubblesClient:
    return ctx.request_context.lifespan_context["bb"]


def _fmt(data: Any) -> str:
    """Format API response data as readable JSON."""
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_server_info(ctx: Context) -> str:
    """Get BlueBubbles server info and health status."""
    data = await _bb(ctx).server_info()
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def ping(ctx: Context) -> str:
    """Ping the BlueBubbles server to check connectivity."""
    data = await _bb(ctx).ping()
    return _fmt(data)


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_chats(
    ctx: Context,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """List iMessage conversations, sorted by most recent activity.

    Args:
        limit: Max number of chats to return (default 25).
        offset: Pagination offset.
    """
    data = await _bb(ctx).list_chats(
        limit=limit, offset=offset, with_fields=["lastmessage"]
    )
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def get_chat(ctx: Context, chat_guid: str) -> str:
    """Get details for a specific chat, including participants.

    Args:
        chat_guid: The chat GUID (e.g. 'iMessage;-;+15551234567' or 'iMessage;+;chat123').
    """
    data = await _bb(ctx).get_chat(chat_guid, with_fields=["participants", "lastmessage"])
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def get_chat_messages(
    ctx: Context,
    chat_guid: str,
    limit: int = 25,
    offset: int = 0,
    sort: str = "DESC",
    after: int | None = None,
    before: int | None = None,
) -> str:
    """Get messages from a specific chat.

    Args:
        chat_guid: The chat GUID.
        limit: Max messages to return (default 25).
        offset: Pagination offset.
        sort: 'ASC' or 'DESC' (default DESC = newest first).
        after: Only messages after this epoch-ms timestamp.
        before: Only messages before this epoch-ms timestamp.
    """
    data = await _bb(ctx).get_chat_messages(
        chat_guid, limit=limit, offset=offset, sort=sort, after=after, before=before
    )
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def get_recent_messages(
    ctx: Context,
    minutes: int = 60,
    limit: int = 50,
) -> str:
    """Get recent messages across all chats within a time window.

    Args:
        minutes: How far back to look (default 60 minutes).
        limit: Max messages to return (default 50).
    """
    after = int((time.time() - minutes * 60) * 1000)
    data = await _bb(ctx).search_messages(after=after, limit=limit)
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def get_unread_chats(ctx: Context, message_limit: int = 5) -> str:
    """Get all chats with unread messages, including their latest messages.

    Args:
        message_limit: Number of recent messages to include per unread chat (default 5).
    """
    bb = _bb(ctx)
    chats = await bb.list_chats(limit=100, with_fields=["lastmessage"])
    unread = [c for c in chats if c.get("hasUnreadMessages")]
    results = []
    for chat in unread:
        messages = await bb.get_chat_messages(chat["guid"], limit=message_limit)
        results.append({
            "chat": chat,
            "recent_messages": messages,
        })
    return _fmt(results)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
))
async def mark_chat_read(ctx: Context, chat_guid: str) -> str:
    """Mark a chat as read (sends read receipt visible to the other person).

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).mark_chat_read(chat_guid)
    return "Chat marked as read."


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_chat_unread(ctx: Context, chat_guid: str) -> str:
    """Mark a chat as unread.

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).mark_chat_unread(chat_guid)
    return "Chat marked as unread."


@mcp.tool(annotations=SEND)
async def start_typing(ctx: Context, chat_guid: str) -> str:
    """Show a typing indicator in a chat (visible to the other person).

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).start_typing(chat_guid)
    return "Typing indicator started."


@mcp.tool(annotations=SEND)
async def stop_typing(ctx: Context, chat_guid: str) -> str:
    """Stop the typing indicator in a chat.

    Args:
        chat_guid: The chat GUID.
    """
    await _bb(ctx).stop_typing(chat_guid)
    return "Typing indicator stopped."


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_chat(ctx: Context, chat_guid: str) -> str:
    """Delete an entire chat conversation. This is irreversible.

    Args:
        chat_guid: The chat GUID to delete.
    """
    await _bb(ctx).delete_chat(chat_guid)
    return "Chat deleted."


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@mcp.tool(annotations=SEND)
async def send_message(
    ctx: Context,
    chat_guid: str,
    message: str,
    reply_to_guid: str | None = None,
) -> str:
    """Send a text message to an existing chat.

    Args:
        chat_guid: The chat GUID to send to.
        message: The message text.
        reply_to_guid: Optional message GUID to reply to (creates a thread).
    """
    data = await _bb(ctx).send_message(chat_guid, message, reply_to_guid=reply_to_guid)
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def send_message_to_address(
    ctx: Context,
    address: str,
    message: str,
    service: str = "iMessage",
) -> str:
    """Send a message to a phone number or email, creating a new chat if needed.

    Args:
        address: Phone number (e.g. '+15551234567') or email address.
        message: The message text.
        service: 'iMessage' or 'SMS' (default iMessage).
    """
    data = await _bb(ctx).send_message_to_address(address, message, service=service)
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def send_reaction(
    ctx: Context,
    chat_guid: str,
    message_guid: str,
    reaction: str,
) -> str:
    """Send a tapback reaction to a message.

    Args:
        chat_guid: The chat GUID containing the message.
        message_guid: The GUID of the message to react to.
        reaction: One of: love, like, dislike, laugh, emphasize, question.
                  Prefix with '-' to remove (e.g. '-love').
    """
    data = await _bb(ctx).send_reaction(chat_guid, message_guid, reaction)
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def edit_message(
    ctx: Context,
    message_guid: str,
    new_text: str,
) -> str:
    """Edit a previously sent message.

    Args:
        message_guid: GUID of the message to edit.
        new_text: The new message text.
    """
    data = await _bb(ctx).edit_message(message_guid, new_text)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def unsend_message(ctx: Context, message_guid: str) -> str:
    """Unsend (retract) a previously sent message.

    Args:
        message_guid: GUID of the message to unsend.
    """
    data = await _bb(ctx).unsend_message(message_guid)
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def search_messages(
    ctx: Context,
    query: str | None = None,
    chat_guid: str | None = None,
    limit: int = 25,
    offset: int = 0,
    after: int | None = None,
    before: int | None = None,
) -> str:
    """Search messages by text content and/or filter by chat and time range.

    Args:
        query: Text to search for in message bodies.
        chat_guid: Limit search to a specific chat.
        limit: Max results (default 25).
        offset: Pagination offset.
        after: Only messages after this epoch-ms timestamp.
        before: Only messages before this epoch-ms timestamp.
    """
    data = await _bb(ctx).search_messages(
        query=query, chat_guid=chat_guid, limit=limit, offset=offset,
        after=after, before=before,
    )
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def get_message(ctx: Context, message_guid: str) -> str:
    """Get a single message by its GUID, including chat and attachment info.

    Args:
        message_guid: The message GUID.
    """
    data = await _bb(ctx).get_message(message_guid)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_contacts(ctx: Context) -> str:
    """Get all contacts from the server."""
    data = await _bb(ctx).get_contacts()
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def lookup_contact(ctx: Context, addresses: list[str]) -> str:
    """Look up contacts by phone numbers or email addresses.

    Args:
        addresses: List of phone numbers or emails to look up.
    """
    data = await _bb(ctx).query_contacts(addresses)
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def check_imessage(ctx: Context, address: str) -> str:
    """Check if a phone number or email is registered for iMessage.

    Args:
        address: Phone number or email to check.
    """
    data = await _bb(ctx).check_imessage_availability(address)
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def check_facetime(ctx: Context, address: str) -> str:
    """Check if a phone number or email is registered for FaceTime.

    Args:
        address: Phone number or email to check.
    """
    data = await _bb(ctx).check_facetime_availability(address)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Group chat management
# ---------------------------------------------------------------------------


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def rename_group(ctx: Context, chat_guid: str, name: str) -> str:
    """Rename a group chat.

    Args:
        chat_guid: The group chat GUID.
        name: New display name for the group.
    """
    data = await _bb(ctx).rename_group(chat_guid, name)
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def add_participant(ctx: Context, chat_guid: str, address: str) -> str:
    """Add a participant to a group chat.

    Args:
        chat_guid: The group chat GUID.
        address: Phone number or email of the person to add.
    """
    data = await _bb(ctx).add_participant(chat_guid, address)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_participant(ctx: Context, chat_guid: str, address: str) -> str:
    """Remove a participant from a group chat.

    Args:
        chat_guid: The group chat GUID.
        address: Phone number or email of the person to remove.
    """
    data = await _bb(ctx).remove_participant(chat_guid, address)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def leave_chat(ctx: Context, chat_guid: str) -> str:
    """Leave a group chat.

    Args:
        chat_guid: The group chat GUID to leave.
    """
    data = await _bb(ctx).leave_chat(chat_guid)
    return "Left the group chat."


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_scheduled_messages(ctx: Context) -> str:
    """List all scheduled (future) messages."""
    data = await _bb(ctx).list_scheduled_messages()
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def schedule_message(
    ctx: Context,
    chat_guid: str,
    message: str,
    scheduled_for: int,
) -> str:
    """Schedule a message to be sent at a future time.

    Args:
        chat_guid: The chat GUID to send to.
        message: The message text.
        scheduled_for: When to send, as epoch milliseconds.
    """
    data = await _bb(ctx).create_scheduled_message(chat_guid, message, scheduled_for)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_scheduled_message(ctx: Context, schedule_id: int) -> str:
    """Delete a scheduled message.

    Args:
        schedule_id: The ID of the scheduled message to cancel.
    """
    await _bb(ctx).delete_scheduled_message(schedule_id)
    return "Scheduled message deleted."


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_attachment_info(ctx: Context, attachment_guid: str) -> str:
    """Get metadata for an attachment (filename, mime type, size, etc.).

    Args:
        attachment_guid: The attachment GUID.
    """
    data = await _bb(ctx).get_attachment(attachment_guid)
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def download_attachment(ctx: Context, attachment_guid: str) -> str:
    """Download an attachment and return it as base64-encoded data.

    Args:
        attachment_guid: The attachment GUID.
    """
    data = await _bb(ctx).download_attachment(attachment_guid)
    meta = await _bb(ctx).get_attachment(attachment_guid)
    return _fmt({
        "filename": meta.get("transferName"),
        "mime_type": meta.get("mimeType"),
        "size_bytes": len(data),
        "data_base64": base64.b64encode(data).decode(),
    })


@mcp.tool(annotations=SEND)
async def send_attachment(
    ctx: Context,
    chat_guid: str,
    data_base64: str,
    filename: str,
    mime_type: str = "application/octet-stream",
) -> str:
    """Send a file attachment to a chat.

    Args:
        chat_guid: The chat GUID to send to.
        data_base64: The file contents as a base64-encoded string.
        filename: The filename (e.g. 'photo.jpg').
        mime_type: MIME type (e.g. 'image/jpeg'). Defaults to 'application/octet-stream'.
    """
    file_data = base64.b64decode(data_base64)
    data = await _bb(ctx).send_attachment(chat_guid, file_data, filename, mime_type)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
