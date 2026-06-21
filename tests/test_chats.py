"""Tests for canonical chat identity & alias resolution (bb_mcp.chats)."""

from __future__ import annotations

import pytest

from bb_mcp.chats import ChatResolver, canonical_chat_key

NORM = lambda a: a.strip().lower()  # noqa: E731 - simple passthrough normalizer


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeClient:
    def __init__(self, chats: list[dict]) -> None:
        self.chats = chats
        self.list_calls = 0

    async def list_chats(self, **kwargs) -> list[dict]:
        self.list_calls += 1
        return self.chats


def chat(guid: str, address: str, last_ts: int) -> dict:
    return {
        "guid": guid,
        "participants": [{"address": address}],
        "lastMessage": {"dateCreated": last_ts},
    }


# ===========================================================================
# canonical_chat_key
# ===========================================================================


class TestCanonicalChatKey:
    def test_all_services_to_one_person_share_a_key(self) -> None:
        keys = {
            canonical_chat_key(f"{svc};-;+15551234567", NORM)
            for svc in ("iMessage", "iMessageLite", "SMS", "RCS", "any")
        }
        assert keys == {"1:1:+15551234567"}

    def test_key_normalizes_the_address(self) -> None:
        assert canonical_chat_key("iMessage;-;+1 (555) 123", str.lower) == "1:1:+1 (555) 123"

    def test_distinct_people_get_distinct_keys(self) -> None:
        assert canonical_chat_key("iMessage;-;+1111", NORM) != canonical_chat_key(
            "iMessage;-;+2222", NORM
        )

    def test_group_keyed_by_id_service_stripped(self) -> None:
        assert canonical_chat_key("iMessage;+;chat123", NORM) == "group:chat123"
        assert canonical_chat_key("SMS;+;chat123", NORM) == "group:chat123"

    def test_group_and_one_to_one_never_collide(self) -> None:
        assert canonical_chat_key("iMessage;+;x", NORM) != canonical_chat_key(
            "iMessage;-;x", NORM
        )

    def test_unparseable_guid_keys_on_raw_self(self) -> None:
        # Fail-safe: distinct, never merges with anything.
        assert canonical_chat_key("weird-guid", NORM) == "raw:weird-guid"


# ===========================================================================
# ChatResolver
# ===========================================================================


class TestChatResolver:
    async def test_stale_imessagelite_resolves_to_live_imessage(self) -> None:
        client = FakeClient([
            chat("iMessageLite;-;+15550001", "+15550001", 100),  # stale shadow
            chat("iMessage;-;+15550001", "+15550001", 900),  # live thread
        ])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.canonical_guid("iMessageLite;-;+15550001") == "iMessage;-;+15550001"
        assert await r.canonical_guid("iMessage;-;+15550001") == "iMessage;-;+15550001"

    async def test_service_rank_breaks_recency_ties(self) -> None:
        client = FakeClient([
            chat("iMessageLite;-;+15550002", "+15550002", 500),
            chat("iMessage;-;+15550002", "+15550002", 500),  # same recency, real iMessage wins
        ])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.canonical_guid("iMessageLite;-;+15550002") == "iMessage;-;+15550002"

    async def test_group_resolves_to_itself(self) -> None:
        r = ChatResolver(FakeClient([]), NORM, clock=FakeClock())
        assert await r.canonical_guid("iMessage;+;chat9") == "iMessage;+;chat9"

    async def test_unknown_guid_resolves_to_itself(self) -> None:
        r = ChatResolver(FakeClient([]), NORM, clock=FakeClock())
        assert await r.canonical_guid("iMessage;-;+19999999") == "iMessage;-;+19999999"

    async def test_find_for_address(self) -> None:
        client = FakeClient([
            chat("iMessageLite;-;+15550003", "+15550003", 100),
            chat("iMessage;-;+15550003", "+15550003", 900),
        ])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.find_for_address("+15550003") == "iMessage;-;+15550003"
        assert await r.find_for_address("+15559999") is None

    async def test_enumeration_is_cached_within_ttl(self) -> None:
        client = FakeClient([chat("iMessage;-;+15550004", "+15550004", 1)])
        clock = FakeClock()
        r = ChatResolver(client, NORM, ttl_seconds=60, clock=clock)
        await r.canonical_guid("iMessage;-;+15550004")
        await r.canonical_guid("iMessage;-;+15550004")
        await r.find_for_address("+15550004")
        assert client.list_calls == 1  # one enumeration reused

    async def test_enumeration_refreshes_after_ttl(self) -> None:
        client = FakeClient([chat("iMessage;-;+15550005", "+15550005", 1)])
        clock = FakeClock()
        r = ChatResolver(client, NORM, ttl_seconds=60, clock=clock)
        await r.canonical_guid("iMessage;-;+15550005")
        clock.advance(61)
        await r.canonical_guid("iMessage;-;+15550005")
        assert client.list_calls == 2
