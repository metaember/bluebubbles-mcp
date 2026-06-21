"""Per-agent "read before you reply" freshness tracking.

An agent can reply to a conversation against a stale snapshot — it read the thread
a while ago, a new message arrived in the gap, and it answers without seeing it.
This module enforces, server-side, that an agent may only send into a chat it has
*recently read* with *nothing new since* — a per-agent compare-and-set on the
conversation's newest message.

"Nothing new" counts messages from *any* sender, not just inbound ones: if you or
another agent (or the user, from another device) barged in since the read, the
queued reply is stale too. An agent's own send advances its own watermark (the
server records it), so sending doesn't make the agent's *next* send look stale.

Each watermark is keyed by an opaque agent-id string the server resolves per
request (from the transport session by default, or a caller-supplied
``_meta.agentId`` behind a pooling proxy); this module is identity-agnostic and
only tracks watermarks by that key. See ``docs/freshness-guard.md`` for the full
design and the (proxy-only) airlock contract.

The tracker is pure and synchronous so it can be unit-tested with an injected
clock; the live "what's the newest inbound right now" fetch lives in the server
tool layer, which passes the result in for comparison.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_AGENTS = 10_000


class FreshnessError(Exception):
    """Raised when a send is blocked for staleness, or a required agent id is
    missing. The message is agent-facing and explains how to recover (re-read the
    chat), so it can be surfaced to the agent verbatim.
    """


@dataclass
class _Entry:
    """One agent's last-known state of one chat."""

    last_message_ts: int | None  # epoch-ms of the newest message seen, None if none
    recorded_at: float  # monotonic stamp, for TTL expiry


class FreshnessTracker:
    """Tracks, per agent, the newest inbound message each agent has seen per chat.

    Eviction is a feature, not just memory hygiene: a watermark that ages past the
    TTL (or falls out of the size-capped backstop) is *gone*, which forces the
    agent to re-read before it can send again. Because a missing watermark fails
    closed (a harmless forced re-read, never a bypass), forgetting is always safe.

    - TTL is the primary, meaningful eviction: it bounds how stale a read may be
      before a reply must re-confirm the live state.
    - ``max_agents`` is a generous memory backstop sized well above expected
      concurrency, so active agents never evict each other's hot watermarks under
      contention (which would cause re-read thrash). Eviction should fire on *age*,
      not *crowding*.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_agents: int = DEFAULT_MAX_AGENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_agents = max(1, max_agents)
        self._clock = clock
        # agent_id -> {chat_guid -> _Entry}; ordered for LRU eviction.
        self._agents: OrderedDict[str, dict[str, _Entry]] = OrderedDict()

    def record(
        self, agent_id: str, chat_guid: str, last_message_ts: int | None
    ) -> None:
        """Record what ``agent_id`` last knows of ``chat_guid``: the newest message
        it has seen, dated ``last_message_ts`` (epoch-ms; ``None`` if the chat has
        no messages). Called both when the agent reads the chat and when it sends
        into it (so its own send doesn't make its next send look stale).
        """
        chats = self._agents.get(agent_id)
        if chats is None:
            chats = {}
            self._agents[agent_id] = chats
        self._agents.move_to_end(agent_id)
        chats[chat_guid] = _Entry(last_message_ts, self._clock())
        self._evict()

    def last_seen(self, agent_id: str, chat_guid: str) -> int | None:
        """The newest-message timestamp ``agent_id`` last saw for ``chat_guid``.

        Returns ``None`` when a fresh read exists but the chat had no messages at
        read time. Raises :class:`FreshnessError` when there is no recorded read,
        or the recorded read is older than the TTL — in both cases the agent must
        re-read the chat (and reconsider its reply) before sending.
        """
        chats = self._agents.get(agent_id)
        entry = chats.get(chat_guid) if chats else None
        if entry is None:
            raise FreshnessError(
                "You haven't read this chat yet. Call get_chat_messages for it "
                "first, then decide what — if anything — to send based on its "
                "current state."
            )
        if self._clock() - entry.recorded_at > self._ttl:
            del chats[chat_guid]
            raise FreshnessError(
                "Your last read of this chat is stale. Call get_chat_messages for "
                "it again, then re-plan: your reply may need revising for what's "
                "changed, or may no longer be warranted."
            )
        self._agents.move_to_end(agent_id)
        return entry.last_message_ts

    def _evict(self) -> None:
        """Drop the least-recently-used agents past the size backstop."""
        while len(self._agents) > self._max_agents:
            self._agents.popitem(last=False)


def newest_message_ts(messages: object) -> int | None:
    """The ``dateCreated`` (epoch-ms) of the newest message in a raw list.

    Counts messages from *any* sender (inbound or outbound) — a barge-in by another
    agent or the user from another device makes a queued reply stale too. Returns
    ``None`` if there are no messages. Operates on raw BlueBubbles message dicts
    (before projection), using ``"isFromMe" in m`` to identify a message the same
    way ``projection._is_message`` does.
    """
    if not isinstance(messages, list):
        return None
    stamps = [
        m["dateCreated"]
        for m in messages
        if isinstance(m, dict) and "isFromMe" in m and m.get("dateCreated") is not None
    ]
    return max(stamps) if stamps else None
