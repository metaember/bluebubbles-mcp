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

- :func:`canonical_chat_key` — a pure, service-agnostic key for the freshness
  watermark, so a read under any alias counts for a send under any alias. Fail-safe:
  an unparseable GUID keys on its raw self (distinct, never merges → never
  manufactures a false "fresh" state).
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

from bb_mcp.policy import address_from_guid

# Lower rank wins when two rows for one participant are equally recent. iMessage
# family first; the iMessageLite shadow is demoted below real iMessage; SMS/RCS last.
_SERVICE_RANK = {"imessage": 0, "any": 0, "imessagelite": 1, "rcs": 2, "sms": 3}
_DEFAULT_RANK = 4

DEFAULT_RESOLVE_TTL_SECONDS = 60.0


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


class ChatResolver:
    """Resolves alias chat GUIDs to the live canonical chat for a conversation.

    The canonical chat for a participant is the **most recent** 1:1 row sharing that
    participant, breaking ties by service preference (iMessage-family over the
    iMessageLite shadow / SMS / RCS). Group and unparseable GUIDs resolve to
    themselves (group aliasing across services is rare). The alias→canonical map is
    built from one ``/chat/query`` enumeration and reused for ``ttl_seconds`` so reads
    and sends don't each pay an enumeration.
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
        self._lock = asyncio.Lock()

    def _is_cache_fresh(self) -> bool:
        return self._built_at is not None and self._clock() - self._built_at <= self._ttl

    async def _ensure_fresh(self) -> None:
        if self._is_cache_fresh():
            return
        async with self._lock:
            if self._is_cache_fresh():  # built by another coroutine while we waited
                return
            # `list_chats` can return None on an empty/edge response; never iterate None.
            chats = await self._client.list_chats(
                limit=1000, sort="lastmessage", with_fields=["participants", "lastmessage"]
            ) or []
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

    async def canonical_guid(self, guid: str) -> str:
        """The live canonical GUID for ``guid``'s conversation.

        Group/unparseable GUIDs and 1:1s with no other known alias resolve to
        themselves (fail-safe — never invents a target).
        """
        if not _is_one_to_one(guid):
            return guid
        await self._ensure_fresh()
        return self._guid_to_canonical.get(guid, guid)

    async def find_for_address(self, address: str) -> str | None:
        """The canonical GUID of an existing 1:1 conversation with ``address``,
        or ``None`` if the person has no chat yet (so a new one may be started)."""
        await self._ensure_fresh()
        return self._addr_to_canonical.get(self._normalize(address))
