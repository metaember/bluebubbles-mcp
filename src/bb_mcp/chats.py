"""Canonical chat identity & alias resolution.

A single human conversation can exist as several ``chat.db`` rows under different
service prefixes — ``iMessage``, ``SMS``, ``RCS``, the macOS-26 ``any``, and the
``iMessageLite`` shadow (a real Apple Messages transport that, in the wild, often
holds a stale single message from an old registration/handoff). BlueBubbles surfaces
each row verbatim, so the same person is addressable under multiple GUIDs that point
at *different* (sometimes months-stale) message sets, and sends may target the wrong
row.

We treat a conversation as **service-agnostic**: one identity per participant (1:1)
or per group id, regardless of service. Two jobs live here:

- :func:`canonical_chat_key` — a pure, service-agnostic key for deduplicating
  chat listings. Freshness uses the resolved GUID itself, so an unresolved alias
  cannot borrow a different row's watermark.
- :class:`ChatResolver` — resolves an alias GUID to the **live canonical chat**
  (most-recent row for the participant, iMessage-family preferred), so reads and
  sends land on the real thread instead of the stale shadow. Enumerates ``/chat/query``
  and caches the mapping briefly (chat topology is global and rarely changes).

See ``docs/canonical-chat-identity.md``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from bb_mcp.client import BlueBubblesError
from bb_mcp.policy import address_from_guid

# Lower rank wins when two rows for one participant are equally recent. iMessage
# family first; the iMessageLite shadow is demoted below real iMessage; SMS/RCS last.
_SERVICE_RANK = {"imessage": 0, "any": 0, "imessagelite": 1, "rcs": 2, "sms": 3}
_DEFAULT_RANK = 4

DEFAULT_RESOLVE_TTL_SECONDS = 60.0
_RESOLVE_PAGE_SIZE = 1000
_LIST_PAGE_SIZE = 100


def _split(guid: str) -> list[str]:
    return guid.split(";")


def canonical_chat_key(guid: str, normalize: Callable[[str], str]) -> str:
    """A service-agnostic identity for the conversation ``guid`` belongs to.

    - 1:1 (``<service>;-;<address>``) → ``"1:1:<normalized address>"`` (any service
      to one person is one conversation).
    - group (``<service>;+;<id>``) → ``"group:<id>"`` (service-stripped).
    - anything else → ``"raw:<guid>"`` (fail-safe: unique, never merges).
    """
    address = address_from_guid(guid)  # not None only for a 1:1 GUID
    if address is not None:
        return f"1:1:{normalize(address)}"
    parts = _split(guid)
    if len(parts) == 3 and parts[1] == "+":
        return f"group:{parts[2]}"
    return f"raw:{guid}"


def _service_rank(guid: str) -> int:
    return _SERVICE_RANK.get(_split(guid)[0].lower(), _DEFAULT_RANK)


def _last_message_ts(chat: dict[str, Any]) -> int:
    last = chat.get("lastMessage") or {}
    return (isinstance(last, dict) and last.get("dateCreated")) or 0


def _is_one_to_one(guid: str) -> bool:
    return address_from_guid(guid) is not None


def dedupe_chats(
    chats: list[dict[str, Any]], normalize: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Collapse alias rows (one conversation surfaced under several services) to one
    chat each — the most recent, iMessage-family preferred — preserving input order
    (which is recency order when the source is sorted by ``lastmessage``)."""
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for chat in chats or []:
        guid = chat.get("guid") or ""
        key = canonical_chat_key(guid, normalize)
        if key not in best:
            best[key] = chat
            order.append(key)
        elif (-_last_message_ts(chat), _service_rank(guid)) < (
            -_last_message_ts(best[key]),
            _service_rank(best[key].get("guid") or ""),
        ):
            best[key] = chat
    return [best[key] for key in order]


async def list_unique_chats(
    client: Any, normalize: Callable[[str], str], limit: int, offset: int
) -> list[dict[str, Any]]:
    """Apply offset and limit to conversations, not raw alias rows."""
    if limit <= 0:
        return []
    unique: list[dict[str, Any]] = []
    raw_offset = 0
    end = offset + limit
    while len(unique) < end:
        page = await client.list_chats(
            limit=_LIST_PAGE_SIZE, offset=raw_offset, with_fields=["lastmessage"]
        ) or []
        if not page:
            break
        unique = dedupe_chats(unique + page, normalize)
        raw_offset += len(page)
        if len(page) < _LIST_PAGE_SIZE:
            break
    return unique[offset:end]


class ChatResolver:
    """Resolves alias chat GUIDs to the live canonical chat for a conversation.

    The canonical chat for a participant is the **most recent** 1:1 row sharing that
    participant, breaking ties by service preference (iMessage-family over the
    iMessageLite shadow / SMS / RCS). Group and unparseable GUIDs resolve to
    themselves (group aliasing across services is rare). The alias→canonical map is
    built from one ``/chat/query`` enumeration and cached for reads. Guarded sends
    refresh it so a newly active alias cannot be missed.
    """

    def __init__(
        self,
        client: Any,
        normalize: Callable[[str], str],
        ttl_seconds: float = DEFAULT_RESOLVE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._normalize = normalize
        self._ttl = ttl_seconds
        self._clock = clock
        self._guid_to_canonical: dict[str, str] = {}
        self._addr_to_canonical: dict[str, str] = {}
        self._built_at: float | None = None
        self._first_page_full = False
        self._lock = asyncio.Lock()

    def _is_cache_fresh(self) -> bool:
        return self._built_at is not None and self._clock() - self._built_at <= self._ttl

    async def _ensure_fresh(self, *, force: bool = False) -> None:
        if not force and self._is_cache_fresh():
            return
        async with self._lock:
            if not force and self._is_cache_fresh():  # built while we waited
                return
            chats = await self._client.list_chats(
                limit=_RESOLVE_PAGE_SIZE,
                sort="lastmessage",
                with_fields=["participants", "lastmessage"],
            )
            if chats is None:
                if force:
                    raise BlueBubblesError("Chat enumeration unavailable")
                chats = []
            self._first_page_full = len(chats) == _RESOLVE_PAGE_SIZE
            self._rebuild(chats)

    def _rebuild(self, chats: list[dict[str, Any]]) -> None:
        by_addr: dict[str, list[dict[str, Any]]] = {}
        for chat in chats:
            guid = chat.get("guid") or ""
            if not _is_one_to_one(guid):
                continue
            addr = self._chat_address(chat, guid)
            if addr:
                by_addr.setdefault(addr, []).append(chat)

        guid_map: dict[str, str] = {}
        addr_map: dict[str, str] = {}
        for addr, group in by_addr.items():
            canonical = min(
                group,
                key=lambda c: (-_last_message_ts(c), _service_rank(c.get("guid") or "")),
            )
            canonical_guid = canonical.get("guid") or ""
            addr_map[addr] = canonical_guid
            for chat in group:
                guid_map[chat.get("guid") or ""] = canonical_guid
        self._guid_to_canonical = guid_map
        self._addr_to_canonical = addr_map
        self._built_at = self._clock()

    def _chat_address(self, chat: dict[str, Any], guid: str) -> str | None:
        participants = [
            p.get("address")
            for p in (chat.get("participants") or [])
            if p.get("address")
        ]
        if len(participants) == 1:
            return self._normalize(participants[0])
        # Fall back to the address embedded in a 1:1 GUID's final segment.
        address = address_from_guid(guid)
        return self._normalize(address) if address else None

    async def canonical_guid(self, guid: str, *, refresh: bool = False) -> str:
        """The live canonical GUID for ``guid``'s conversation.

        Group/unparseable GUIDs resolve to themselves. An unlisted 1:1 alias can
        use a known address mapping; otherwise it keeps its own GUID.
        """
        if not _is_one_to_one(guid):
            return guid
        await self._ensure_fresh(force=refresh)
        address = address_from_guid(guid)
        assert address is not None
        return self._guid_to_canonical.get(guid) or self._addr_to_canonical.get(
            self._normalize(address)
        ) or guid

    async def find_for_address(self, address: str, *, refresh: bool = False) -> str | None:
        """A GUID of an existing 1:1 conversation with ``address``,
        or ``None`` if the person has no chat yet (so a new one may be started)."""
        await self._ensure_fresh(force=refresh)
        normalized = self._normalize(address)
        known = self._addr_to_canonical.get(normalized)
        if known or not self._first_page_full:
            return known

        # An old conversation may be past the first page. `create_chat` must not
        # treat a truncated enumeration as proof that it is a first contact.
        offset = _RESOLVE_PAGE_SIZE
        while True:
            page = await self._client.list_chats(
                limit=_RESOLVE_PAGE_SIZE,
                offset=offset,
                sort="lastmessage",
                with_fields=["participants", "lastmessage"],
            )
            if page is None:
                raise BlueBubblesError("Chat enumeration unavailable")
            for chat in page:
                guid = chat.get("guid") or ""
                if _is_one_to_one(guid) and self._chat_address(chat, guid) == normalized:
                    return guid
            if len(page) < _RESOLVE_PAGE_SIZE:
                return None
            offset += len(page)
