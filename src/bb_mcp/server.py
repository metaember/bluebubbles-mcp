"""MCP server for BlueBubbles iMessage bridge."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Mapping

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from bb_mcp.capabilities import (
    PRIVATE_API_TOOLS,
    my_address_from_info,
    parse_override,
    private_api_from_info,
)
from bb_mcp.client import BlueBubblesClient, BlueBubblesError
from bb_mcp.contacts import (
    ContactResolver,
    collect_addresses,
    contact_addresses,
    contact_display_name,
    inject_names,
)
from bb_mcp.freshness import (
    DEFAULT_MAX_AGENTS,
    DEFAULT_TTL_SECONDS,
    FreshnessError,
    FreshnessTracker,
    newest_message_ts,
)
from bb_mcp.policy import DEFAULT_REGION, Allowlist, Guard
from bb_mcp.projection import filter_by_sender, project

logger = logging.getLogger(__name__)

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


async def _server_info(client: BlueBubblesClient) -> dict[str, Any] | None:
    """Read ``/server/info`` once at startup, tolerating failure.

    Drives both Private API detection and owner-address discovery. A failure
    (server down / not yet ready) is never fatal — callers fall back to safe
    defaults so a transient blip doesn't take the MCP server down.
    """
    try:
        return await client.server_info()
    except Exception as exc:  # noqa: BLE001 — never let startup introspection crash
        logger.warning(
            "Could not read /server/info (%s); using defaults. "
            "Set BLUEBUBBLES_PRIVATE_API / BLUEBUBBLES_MY_ADDRESS to override.",
            exc,
        )
        return None


@asynccontextmanager
async def lifespan(server: FastMCP):
    url = os.environ.get("BLUEBUBBLES_URL")
    password = os.environ.get("BLUEBUBBLES_PASSWORD")
    if not url or not password:
        raise RuntimeError(
            "BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD environment variables are required"
        )
    client = BlueBubblesClient(url, password)
    allowlist = Allowlist.from_env(os.environ, "BLUEBUBBLES_WRITE_ALLOWLIST")
    guard = Guard(allowlist, client)
    region = os.environ.get("BLUEBUBBLES_ALLOWLIST_REGION", DEFAULT_REGION)
    contacts = ContactResolver(client, region=region)
    resolve_names = parse_override(os.environ.get("BLUEBUBBLES_RESOLVE_NAMES")) is not False
    freshness, freshness_identity = _build_freshness(os.environ)

    info = await _server_info(client)
    forced = parse_override(os.environ.get("BLUEBUBBLES_PRIVATE_API"))
    private_api = forced if forced is not None else private_api_from_info(info)
    my_address = os.environ.get("BLUEBUBBLES_MY_ADDRESS") or my_address_from_info(info)

    if not private_api:
        logger.warning(
            "BlueBubbles Private API unavailable — sends use AppleScript and these "
            "tools are hidden: %s",
            ", ".join(PRIVATE_API_TOOLS),
        )
        for name in PRIVATE_API_TOOLS:
            try:
                server.remove_tool(name)
            except ToolError:
                pass  # already removed (e.g. lifespan re-entered) or unknown name

    try:
        yield {
            "bb": client,
            "guard": guard,
            "private_api": private_api,
            "my_address": my_address,
            "contacts": contacts,
            "resolve_names": resolve_names,
            "freshness": freshness,
            "freshness_identity": freshness_identity,
        }
    finally:
        await client.close()


def _build_freshness(env: Mapping[str, str]) -> tuple[FreshnessTracker | None, str]:
    """Build the per-agent freshness guard and its identity mode.

    Returns ``(tracker, identity)``; ``tracker`` is ``None`` when disabled.

    **On by default** — the guard makes an agent read a chat before it can reply
    into it, which is good hygiene for any deployment, not just a multi-agent one.
    Disable with ``BLUEBUBBLES_FRESHNESS=off``.

    Identity (``BLUEBUBBLES_FRESHNESS_IDENTITY``) decides how agents are told
    apart — the only knob that depends on deployment topology:

    - ``session`` (default): the MCP transport session. Works with no extra setup
      for stdio (one agent) and direct HTTP (one session per connecting client).
      Requires stateful HTTP over HTTP (FastMCP's default).
    - ``meta``: a per-agent ``agentId`` the caller stamps into request ``_meta``.
      Required behind a pooling proxy/airlock, where every agent shares one
      transport session so ``session`` can't tell them apart. See
      ``docs/freshness-guard.md``.
    """
    if parse_override(env.get("BLUEBUBBLES_FRESHNESS")) is False:
        return None, "off"
    identity = env.get("BLUEBUBBLES_FRESHNESS_IDENTITY", "session").strip().lower()
    if identity not in ("session", "meta"):
        identity = "session"
    ttl = float(env.get("BLUEBUBBLES_WATERMARK_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    max_agents = int(env.get("BLUEBUBBLES_WATERMARK_MAX_AGENTS", DEFAULT_MAX_AGENTS))
    logger.info(
        "Freshness guard enabled (identity=%s, ttl=%ss, max_agents=%s); sends "
        "require a fresh read of the chat.",
        identity,
        ttl,
        max_agents,
    )
    return FreshnessTracker(ttl_seconds=ttl, max_agents=max_agents), identity


mcp = FastMCP(
    "BlueBubbles",
    instructions="iMessage bridge via BlueBubbles",
    lifespan=lifespan,
    # host/port only matter for HTTP transport; see main(). Defaults to loopback
    # so HTTP mode isn't exposed by accident; the Docker image sets 0.0.0.0.
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)


def _bb(ctx: Context) -> BlueBubblesClient:
    return ctx.request_context.lifespan_context["bb"]


def _guard(ctx: Context) -> Guard:
    return ctx.request_context.lifespan_context["guard"]


def _private_api(ctx: Context) -> bool:
    return ctx.request_context.lifespan_context["private_api"]


def _send_method(ctx: Context) -> str:
    """The BlueBubbles send method to use given detected capabilities."""
    return "private-api" if _private_api(ctx) else "apple-script"


def _contacts(ctx: Context) -> ContactResolver:
    return ctx.request_context.lifespan_context["contacts"]


def _freshness(ctx: Context) -> FreshnessTracker | None:
    return ctx.request_context.lifespan_context.get("freshness")


def _freshness_identity(ctx: Context) -> str:
    return ctx.request_context.lifespan_context.get("freshness_identity", "session")


def _agent_id(ctx: Context) -> str:
    """A stable key identifying the calling agent, per the configured identity mode.

    - ``session``: derived from the MCP transport session, which is per-connection
      (per-agent) for stdio and direct HTTP. No caller cooperation needed.
    - ``meta``: the ``agentId`` the caller (a pooling airlock) stamped into request
      ``_meta``. Fail closed — a missing or blank id is rejected rather than
      silently shared across agents.

    Only called when the guard is enabled.
    """
    if _freshness_identity(ctx) == "meta":
        meta = ctx.request_context.meta
        agent_id = getattr(meta, "agentId", None) if meta is not None else None
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise FreshnessError(
                "This server is configured to identify agents by _meta.agentId, "
                "but none was supplied on this request."
            )
        return f"meta:{agent_id}"
    return f"session:{id(ctx.request_context.session)}"


def _record_watermark(ctx: Context, chat_guid: str, raw_messages: Any) -> None:
    """Record what this agent just saw for ``chat_guid`` (no-op if guard disabled).

    Operates on the raw BlueBubbles message list (before projection), so the
    newest-message timestamp reflects exactly what was fetched.
    """
    tracker = _freshness(ctx)
    if tracker is None:
        return
    tracker.record(_agent_id(ctx), chat_guid, newest_message_ts(raw_messages))


async def _record_sent(ctx: Context, chat_guid: str, sent: Any) -> None:
    """Advance this agent's watermark to its own just-sent message (no-op if the
    guard is disabled), so sending doesn't make the agent's *next* send look stale.

    Uses the send response's timestamp; only if it lacks one does it fall back to a
    re-fetch (rare).
    """
    tracker = _freshness(ctx)
    if tracker is None:
        return
    ts = sent.get("dateCreated") if isinstance(sent, dict) else None
    if ts is None:
        ts = await _latest_message_ts(ctx, chat_guid)
    tracker.record(_agent_id(ctx), chat_guid, ts)


async def _latest_message_ts(ctx: Context, chat_guid: str) -> int | None:
    """Fetch the chat's newest message timestamp right now (one API round-trip)."""
    recent = await _bb(ctx).get_chat_messages(chat_guid, limit=10, sort="DESC")
    return newest_message_ts(recent)


async def _check_freshness(ctx: Context, chat_guid: str) -> None:
    """Block a send unless this agent recently read ``chat_guid`` and nothing new
    has arrived since — from any sender (no-op if the guard is disabled)."""
    tracker = _freshness(ctx)
    if tracker is None:
        return
    expected = tracker.last_seen(_agent_id(ctx), chat_guid)
    live = await _latest_message_ts(ctx, chat_guid)
    if live is not None and (expected is None or live > expected):
        raise FreshnessError(
            "The conversation moved since you last read it — new messages have "
            "arrived. Call get_chat_messages to see them, then re-plan in light of "
            "them: your reply may need revising, or may no longer be warranted."
        )


async def _enrich(ctx: Context, data: Any) -> Any:
    """Annotate handle addresses in a response with their contact names.

    A no-op when name resolution is disabled or the payload has no handles. Never
    raises — enrichment is best-effort and must not break the underlying read.
    """
    if not ctx.request_context.lifespan_context.get("resolve_names"):
        return data
    addresses = collect_addresses(data)
    if not addresses:
        return data
    try:
        names = await _contacts(ctx).names_for(addresses)
    except Exception:  # noqa: BLE001
        return data
    return inject_names(data, names) if names else data


def _my_address(ctx: Context) -> str | None:
    return ctx.request_context.lifespan_context.get("my_address")


def _resolve_from(ctx: Context, from_address: str) -> str:
    """Resolve a from_address filter, translating 'me' to the user's address."""
    if from_address.strip().lower() == "me":
        me = _my_address(ctx)
        if not me:
            raise ValueError(
                "Can't resolve 'me': the server didn't report your address. "
                "Set BLUEBUBBLES_MY_ADDRESS, or pass an explicit phone/email."
            )
        return me
    return from_address


async def _present(ctx: Context, data: Any, *, extended: bool) -> str:
    """Enrich with contact names, then compact (unless extended), then format."""
    return _fmt(project(await _enrich(ctx, data), extended))


async def _present_messages(
    ctx: Context,
    messages: Any,
    *,
    extended: bool,
    from_address: str | None,
    limit: int,
) -> str:
    """Like _present, but for a flat message list — also applies sender filter."""
    messages = await _enrich(ctx, messages)
    if from_address and isinstance(messages, list):
        resolver = _contacts(ctx)
        target = resolver.normalize(_resolve_from(ctx, from_address))
        messages = filter_by_sender(
            messages, target, _my_address(ctx), resolver.normalize
        )[:limit]
    return _fmt(project(messages, extended))


def _fetch_limit(limit: int, from_address: str | None) -> int:
    """Widen the fetch when filtering by sender so results aren't starved."""
    return min(max(limit, 200), 1000) if from_address else limit


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


@mcp.tool(annotations=READ_ONLY)
async def get_my_address(ctx: Context) -> str:
    """Get the user's own iMessage address (the phone/email that owns this server).

    Use this to tell which messages the user sent themselves — e.g. a message
    whose handle matches this address is from the user, not the other person.
    Returns the BLUEBUBBLES_MY_ADDRESS override if set, otherwise the address
    detected from the BlueBubbles server.
    """
    me = ctx.request_context.lifespan_context.get("my_address")
    if not me:
        raise ValueError(
            "Couldn't determine your address: the BlueBubbles server didn't report "
            "one. Set BLUEBUBBLES_MY_ADDRESS to specify it."
        )
    return me


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_chats(
    ctx: Context,
    limit: int = 25,
    offset: int = 0,
    extended: bool = False,
) -> str:
    """List iMessage conversations, sorted by most recent activity.

    Args:
        limit: Max number of chats to return (default 25).
        offset: Pagination offset.
        extended: Return full raw fields instead of the compact set (default False).
    """
    data = await _bb(ctx).list_chats(
        limit=limit, offset=offset, with_fields=["lastmessage"]
    )
    return await _present(ctx, data, extended=extended)


@mcp.tool(annotations=READ_ONLY)
async def get_chat(ctx: Context, chat_guid: str, extended: bool = False) -> str:
    """Get details for a specific chat, including participants.

    Args:
        chat_guid: The chat GUID (e.g. 'iMessage;-;+15551234567' or 'iMessage;+;chat123').
        extended: Return full raw fields instead of the compact set (default False).
    """
    data = await _bb(ctx).get_chat(chat_guid, with_fields=["participants", "lastmessage"])
    return await _present(ctx, data, extended=extended)


@mcp.tool(annotations=READ_ONLY)
async def get_chat_messages(
    ctx: Context,
    chat_guid: str,
    limit: int = 25,
    offset: int = 0,
    sort: str = "DESC",
    after: int | None = None,
    before: int | None = None,
    from_address: str | None = None,
    extended: bool = False,
) -> str:
    """Get messages from a specific chat.

    Args:
        chat_guid: The chat GUID.
        limit: Max messages to return (default 25).
        offset: Pagination offset.
        sort: 'ASC' or 'DESC' (default DESC = newest first).
        after: Only messages after this epoch-ms timestamp.
        before: Only messages before this epoch-ms timestamp.
        from_address: Only messages sent by this phone/email. Pass 'me' for the
            user's own messages.
        extended: Return full raw fields instead of the compact set (default False).
    """
    data = await _bb(ctx).get_chat_messages(
        chat_guid,
        limit=_fetch_limit(limit, from_address),
        offset=offset,
        sort=sort,
        after=after,
        before=before,
    )
    _record_watermark(ctx, chat_guid, data)
    return await _present_messages(
        ctx, data, extended=extended, from_address=from_address, limit=limit
    )


@mcp.tool(annotations=READ_ONLY)
async def get_recent_messages(
    ctx: Context,
    minutes: int = 60,
    limit: int = 50,
    from_address: str | None = None,
    extended: bool = False,
) -> str:
    """Get recent messages across all chats within a time window.

    Args:
        minutes: How far back to look (default 60 minutes).
        limit: Max messages to return (default 50).
        from_address: Only messages sent by this phone/email. Pass 'me' for the
            user's own messages.
        extended: Return full raw fields instead of the compact set (default False).
    """
    after = int((time.time() - minutes * 60) * 1000)
    data = await _bb(ctx).search_messages(
        after=after, limit=_fetch_limit(limit, from_address)
    )
    return await _present_messages(
        ctx, data, extended=extended, from_address=from_address, limit=limit
    )


@mcp.tool(annotations=READ_ONLY)
async def get_unread_chats(
    ctx: Context, message_limit: int = 5, extended: bool = False
) -> str:
    """Get all chats with unread messages, including their latest messages.

    Args:
        message_limit: Number of recent messages to include per unread chat (default 5).
        extended: Return full raw fields instead of the compact set (default False).
    """
    bb = _bb(ctx)
    chats = await bb.list_chats(limit=100, with_fields=["lastmessage"])
    unread = [c for c in chats if c.get("hasUnreadMessages")]
    results = []
    for chat in unread:
        messages = await bb.get_chat_messages(chat["guid"], limit=message_limit)
        _record_watermark(ctx, chat["guid"], messages)
        results.append({
            "chat": chat,
            "recent_messages": messages,
        })
    return await _present(ctx, results, extended=extended)


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
    await _guard(ctx).check_chat(chat_guid)
    await _bb(ctx).mark_chat_read(chat_guid)
    return "Chat marked as read."


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_chat_unread(ctx: Context, chat_guid: str) -> str:
    """Mark a chat as unread.

    Args:
        chat_guid: The chat GUID.
    """
    await _guard(ctx).check_chat(chat_guid)
    await _bb(ctx).mark_chat_unread(chat_guid)
    return "Chat marked as unread."


@mcp.tool(annotations=SEND)
async def start_typing(ctx: Context, chat_guid: str) -> str:
    """Show a typing indicator in a chat (visible to the other person).

    Args:
        chat_guid: The chat GUID.
    """
    await _guard(ctx).check_chat(chat_guid)
    await _bb(ctx).start_typing(chat_guid)
    return "Typing indicator started."


@mcp.tool(annotations=SEND)
async def stop_typing(ctx: Context, chat_guid: str) -> str:
    """Stop the typing indicator in a chat.

    Args:
        chat_guid: The chat GUID.
    """
    await _guard(ctx).check_chat(chat_guid)
    await _bb(ctx).stop_typing(chat_guid)
    return "Typing indicator stopped."


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_chat(ctx: Context, chat_guid: str) -> str:
    """Delete an entire chat conversation. This is irreversible.

    Args:
        chat_guid: The chat GUID to delete.
    """
    await _guard(ctx).check_chat(chat_guid)
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
    if reply_to_guid and not _private_api(ctx):
        raise ValueError(
            "Threaded replies require the BlueBubbles Private API, which isn't "
            "enabled on this server. Send without reply_to_guid."
        )
    await _guard(ctx).check_chat(chat_guid)
    await _check_freshness(ctx, chat_guid)
    data = await _bb(ctx).send_message(
        chat_guid, message, method=_send_method(ctx), reply_to_guid=reply_to_guid
    )
    await _record_sent(ctx, chat_guid, data)
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
    if service.upper() == "SMS" and not _private_api(ctx):
        raise ValueError(
            "Sending over SMS requires the BlueBubbles Private API, which isn't "
            "enabled on this server. Use service='iMessage'."
        )
    _guard(ctx).check_address(address)
    data = await _bb(ctx).send_message_to_address(
        address, message, service=service, method=_send_method(ctx)
    )
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def create_group_chat(
    ctx: Context,
    addresses: list[str],
    message: str,
    service: str = "iMessage",
) -> str:
    """Create a new group chat with several people and send the first message.

    Args:
        addresses: Phone numbers and/or emails of all participants (2+ for a group).
        message: The opening message (required to create the chat).
        service: 'iMessage' or 'SMS' (default iMessage).
    """
    if service.upper() == "SMS" and not _private_api(ctx):
        raise ValueError(
            "Creating an SMS group requires the BlueBubbles Private API, which "
            "isn't enabled on this server. Use service='iMessage'."
        )
    guard = _guard(ctx)
    for address in addresses:
        guard.check_address(address)
    data = await _bb(ctx).create_chat(
        addresses, message=message, service=service, method=_send_method(ctx)
    )
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def send_multipart(
    ctx: Context,
    chat_guid: str,
    parts: list[dict[str, Any]],
) -> str:
    """Send one message combining text and attachments, in order.

    Each part is one of:
      - `{"text": "some text"}`
      - `{"filename": "photo.jpg", "data_base64": "...", "mime_type": "image/jpeg"}`

    Attachment parts are uploaded first, then sent together as a single message.

    Args:
        chat_guid: The chat GUID to send to.
        parts: Ordered list of text and/or attachment parts (see above).
    """
    await _guard(ctx).check_chat(chat_guid)
    await _check_freshness(ctx, chat_guid)
    assembled: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if part.get("text"):
            assembled.append({"partIndex": index, "text": part["text"]})
        elif part.get("data_base64"):
            filename = part.get("filename") or f"attachment-{index}"
            mime_type = part.get("mime_type", "application/octet-stream")
            uploaded = await _bb(ctx).upload_attachment(
                base64.b64decode(part["data_base64"]), filename, mime_type
            )
            assembled.append(
                {"partIndex": index, "attachment": uploaded.get("path"), "name": filename}
            )
        else:
            raise ValueError(f"Part {index} must have either 'text' or 'data_base64'.")
    data = await _bb(ctx).send_multipart(chat_guid, assembled)
    await _record_sent(ctx, chat_guid, data)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_message(ctx: Context, chat_guid: str, message_guid: str) -> str:
    """Delete a single message from a chat. Irreversible.

    Distinct from unsend_message, which retracts a message you sent so the other
    person no longer sees it; this deletes the message from the chat.

    Args:
        chat_guid: The chat GUID containing the message.
        message_guid: The GUID of the message to delete.
    """
    await _guard(ctx).check_chat(chat_guid)
    await _bb(ctx).delete_chat_message(chat_guid, message_guid)
    return "Message deleted."


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
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).send_reaction(chat_guid, message_guid, reaction)
    return _fmt(data)


# Note: edit_message/unsend_message take a message GUID, not a recipient, so the
# write allowlist isn't applied here — they only act on an already-sent message
# (which passed the allowlist at send time) and can't reach a new recipient.
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
    from_address: str | None = None,
    extended: bool = False,
) -> str:
    """Search messages by text content and/or filter by chat and time range.

    Args:
        query: Text to search for in message bodies.
        chat_guid: Limit search to a specific chat.
        limit: Max results (default 25).
        offset: Pagination offset.
        after: Only messages after this epoch-ms timestamp.
        before: Only messages before this epoch-ms timestamp.
        from_address: Only messages sent by this phone/email. Pass 'me' for the
            user's own messages.
        extended: Return full raw fields instead of the compact set (default False).
    """
    data = await _bb(ctx).search_messages(
        query=query, chat_guid=chat_guid, limit=_fetch_limit(limit, from_address),
        offset=offset, after=after, before=before,
    )
    return await _present_messages(
        ctx, data, extended=extended, from_address=from_address, limit=limit
    )


@mcp.tool(annotations=READ_ONLY)
async def get_message(
    ctx: Context, message_guid: str, extended: bool = False
) -> str:
    """Get a single message by its GUID, including chat and attachment info.

    Args:
        message_guid: The message GUID.
        extended: Return full raw fields instead of the compact set (default False).
    """
    data = await _bb(ctx).get_message(message_guid)
    return await _present(ctx, data, extended=extended)


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
async def find_contact(ctx: Context, name: str) -> str:
    """Find contacts by name (case-insensitive substring match).

    Use this when you know someone's name but not their number — then message
    them with send_message_to_address, or pass the name to find_chats. Returns
    each match's name, phone numbers, and emails.

    Args:
        name: Full or partial contact name (e.g. 'mom', 'alice').
    """
    matches = await _contacts(ctx).find(name)
    results = [
        {
            "name": contact_display_name(c),
            "phoneNumbers": [e.get("address") for e in (c.get("phoneNumbers") or [])],
            "emails": [e.get("address") for e in (c.get("emails") or [])],
        }
        for c in matches
    ]
    return _fmt(results)


@mcp.tool(annotations=READ_ONLY)
async def find_chats(ctx: Context, name: str, limit: int = 25) -> str:
    """Find existing chats involving a contact, by name (case-insensitive).

    Matches 1:1 and group chats whose participants resolve to a contact named
    `name`, or whose group title contains `name` — so you can read or reply
    without knowing the phone number. Returns chats, most recent first.

    Args:
        name: Full or partial contact or group name.
        limit: Max chats to return (default 25).
    """
    resolver = _contacts(ctx)
    matches = await resolver.find(name)
    targets = {
        resolver.normalize(addr) for c in matches for addr in contact_addresses(c)
    }
    q = name.strip().lower()
    chats = await _bb(ctx).list_chats(
        limit=1000, with_fields=["participants", "lastmessage"]
    )
    found = []
    for chat in chats:
        title = (chat.get("displayName") or "").lower()
        participants = {
            resolver.normalize(p["address"])
            for p in (chat.get("participants") or [])
            if p.get("address")
        }
        if (targets & participants) or (q and q in title):
            found.append(chat)
        if len(found) >= limit:
            break
    return _fmt(await _enrich(ctx, found))


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
# Handles
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def query_handles(
    ctx: Context,
    address: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """List or search the iMessage/SMS handles the server knows about.

    Args:
        address: Optional filter — a full or partial phone number / email.
        limit: Max handles to return (default 100).
        offset: Pagination offset.
    """
    data = await _bb(ctx).query_handles(address=address, limit=limit, offset=offset)
    return _fmt(await _enrich(ctx, data))


@mcp.tool(annotations=READ_ONLY)
async def get_handle(ctx: Context, handle_guid: str) -> str:
    """Get a single handle by its GUID/address.

    Args:
        handle_guid: The handle GUID or address (e.g. '+15551234567').
    """
    data = await _bb(ctx).get_handle(handle_guid)
    return _fmt(await _enrich(ctx, data))


@mcp.tool(annotations=READ_ONLY)
async def get_focus_status(ctx: Context, handle_guid: str) -> str:
    """Check a contact's Focus / Do Not Disturb status, if they share it.

    Args:
        handle_guid: The handle GUID or address to check.
    """
    data = await _bb(ctx).get_focus_status(handle_guid)
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
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).rename_group(chat_guid, name)
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def add_participant(ctx: Context, chat_guid: str, address: str) -> str:
    """Add a participant to a group chat.

    Args:
        chat_guid: The group chat GUID.
        address: Phone number or email of the person to add.
    """
    guard = _guard(ctx)
    await guard.check_chat(chat_guid)
    guard.check_address(address)  # the new member becomes a recipient too
    data = await _bb(ctx).add_participant(chat_guid, address)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_participant(ctx: Context, chat_guid: str, address: str) -> str:
    """Remove a participant from a group chat.

    Args:
        chat_guid: The group chat GUID.
        address: Phone number or email of the person to remove.
    """
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).remove_participant(chat_guid, address)
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def leave_chat(ctx: Context, chat_guid: str) -> str:
    """Leave a group chat.

    Args:
        chat_guid: The group chat GUID to leave.
    """
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).leave_chat(chat_guid)
    return "Left the group chat."


@mcp.tool(annotations=READ_ONLY)
async def get_group_icon(ctx: Context, chat_guid: str) -> str:
    """Download a group chat's icon as base64-encoded image data.

    Args:
        chat_guid: The group chat GUID.
    """
    data = await _bb(ctx).get_group_icon(chat_guid)
    return _fmt({
        "size_bytes": len(data),
        "data_base64": base64.b64encode(data).decode(),
    })


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def set_group_icon(
    ctx: Context,
    chat_guid: str,
    data_base64: str,
    filename: str = "icon.png",
    mime_type: str = "image/png",
) -> str:
    """Set a group chat's icon from base64-encoded image data.

    Args:
        chat_guid: The group chat GUID.
        data_base64: The image contents as a base64-encoded string.
        filename: Filename for the upload (default 'icon.png').
        mime_type: MIME type (default 'image/png').
    """
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).set_group_icon(
        chat_guid, base64.b64decode(data_base64), filename, mime_type
    )
    return _fmt(data)


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_group_icon(ctx: Context, chat_guid: str) -> str:
    """Remove a group chat's icon.

    Args:
        chat_guid: The group chat GUID.
    """
    await _guard(ctx).check_chat(chat_guid)
    await _bb(ctx).remove_group_icon(chat_guid)
    return "Group icon removed."


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
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).create_scheduled_message(
        chat_guid, message, scheduled_for, method=_send_method(ctx)
    )
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def get_scheduled_message(ctx: Context, schedule_id: int) -> str:
    """Get a single scheduled message by its ID.

    Args:
        schedule_id: The ID of the scheduled message.
    """
    data = await _bb(ctx).get_scheduled_message(schedule_id)
    return _fmt(data)


@mcp.tool(annotations=SEND)
async def update_scheduled_message(
    ctx: Context,
    schedule_id: int,
    chat_guid: str,
    message: str,
    scheduled_for: int,
) -> str:
    """Update an existing scheduled message's chat, text, and send time.

    Args:
        schedule_id: The ID of the scheduled message to update.
        chat_guid: The chat GUID to send to.
        message: The new message text.
        scheduled_for: New send time, as epoch milliseconds.
    """
    await _guard(ctx).check_chat(chat_guid)
    data = await _bb(ctx).update_scheduled_message(
        schedule_id, chat_guid, message, scheduled_for, method=_send_method(ctx)
    )
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
    await _guard(ctx).check_chat(chat_guid)
    await _check_freshness(ctx, chat_guid)
    file_data = base64.b64decode(data_base64)
    data = await _bb(ctx).send_attachment(
        chat_guid, file_data, filename, mime_type, method=_send_method(ctx)
    )
    await _record_sent(ctx, chat_guid, data)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Find My
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def find_my_devices(ctx: Context, refresh: bool = False) -> str:
    """Get Find My locations and battery for the user's own Apple devices.

    Args:
        refresh: Force a fresh location update before returning (slower).
    """
    data = await _bb(ctx).find_my_devices(refresh=refresh)
    return _fmt(data)


@mcp.tool(annotations=READ_ONLY)
async def find_my_friends(ctx: Context, refresh: bool = False) -> str:
    """Get Find My locations for people sharing their location with the user.

    Args:
        refresh: Force a fresh location update before returning (slower).
    """
    data = await _bb(ctx).find_my_friends(refresh=refresh)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _assert_freshness_transport_compatible(transport: str, env: Mapping[str, str]) -> None:
    """Refuse to start in a config where the freshness guard can't work.

    In ``session`` identity mode over HTTP, the guard keys each agent's watermark
    on the MCP transport session, which must persist across that agent's
    read→send. Stateless HTTP gives every request a fresh session, so a send would
    always see "no prior read" and be blocked — the guard would silently reject
    every send. Fail fast with a clear fix instead.

    ``meta`` mode is unaffected (identity comes from ``_meta.agentId``, not the
    session), and stdio sessions are always persistent — so this only fires for
    streamable-http + stateless + session-identity + guard-on.
    """
    if transport != "streamable-http" or not mcp.settings.stateless_http:
        return
    tracker, identity = _build_freshness(env)
    if tracker is not None and identity == "session":
        raise RuntimeError(
            "Incompatible config: the freshness guard in session-identity mode "
            "needs persistent MCP sessions, but stateless HTTP is enabled — every "
            "send would be blocked. Fix one of: disable stateless HTTP; set "
            "BLUEBUBBLES_FRESHNESS_IDENTITY=meta (behind a proxy that stamps "
            "_meta.agentId); or set BLUEBUBBLES_FRESHNESS=off."
        )


def main() -> None:
    """Run the server.

    Defaults to stdio. Set MCP_TRANSPORT=streamable-http to serve over HTTP
    instead, on MCP_HOST:MCP_PORT at path /mcp (default 127.0.0.1:8000) — useful
    for running as a container that an HTTP-capable MCP client connects to.
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    _assert_freshness_transport_compatible(transport, os.environ)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
