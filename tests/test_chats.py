"""Tests for canonical chat identity & alias resolution (bb_mcp.chats)."""

from __future__ import annotations

import pytest

from bb_mcp.client import BlueBubblesError
from bb_mcp.chats import ChatResolver, canonical_chat_key, dedupe_chats, list_unique_chats

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
        if self.chats is None:
            return None
        offset = kwargs.get("offset", 0)
        return self.chats[offset:offset + kwargs.get("limit", len(self.chats))]


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

    async def test_none_enumeration_is_fail_safe(self) -> None:
        # list_chats can return None on an edge response; resolve to self, don't crash.
        client = FakeClient(None)  # type: ignore[arg-type]
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.canonical_guid("iMessage;-;+15550006") == "iMessage;-;+15550006"
        assert await r.find_for_address("+15550006") is None

    async def test_forced_refresh_rejects_missing_enumeration(self) -> None:
        client = FakeClient(None)  # type: ignore[arg-type]
        r = ChatResolver(client, NORM, clock=FakeClock())
        with pytest.raises(BlueBubblesError, match="enumeration unavailable"):
            await r.canonical_guid("iMessage;-;+15550006", refresh=True)
        with pytest.raises(BlueBubblesError, match="enumeration unavailable"):
            await r.find_for_address("+15550006", refresh=True)

    async def test_unknown_alias_uses_known_address(self) -> None:
        client = FakeClient([chat("iMessage;-;+15550009", "+15550009", 900)])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.canonical_guid("iMessageLite;-;+15550009") == (
            "iMessage;-;+15550009"
        )

    async def test_refresh_sees_newly_active_alias(self) -> None:
        live = chat("iMessage;-;+15550010", "+15550010", 900)
        newly_active = chat("SMS;-;+15550010", "+15550010", 100)
        client = FakeClient([live, newly_active])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.canonical_guid(live["guid"]) == live["guid"]
        newly_active["lastMessage"]["dateCreated"] = 950
        assert await r.canonical_guid(live["guid"], refresh=True) == newly_active["guid"]
        assert client.list_calls == 2

    async def test_find_for_address_searches_beyond_first_thousand(self) -> None:
        filler = [chat(f"iMessage;-;+1999{i:04d}", f"+1999{i:04d}", 2000 - i)
                  for i in range(1000)]
        target = chat("iMessage;-;+15550011", "+15550011", 1)
        client = FakeClient(filler + [target])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.find_for_address("+15550011") == target["guid"]
        assert client.list_calls == 2

    async def test_find_for_address_refreshes_cached_absence(self) -> None:
        client = FakeClient([])
        r = ChatResolver(client, NORM, clock=FakeClock())
        assert await r.find_for_address("+15550012") is None
        new_chat = chat("iMessage;-;+15550012", "+15550012", 100)
        client.chats = [new_chat]
        assert await r.find_for_address("+15550012", refresh=True) == new_chat["guid"]
        assert client.list_calls == 2


class TestDedupeChats:
    def test_collapses_alias_rows_keeping_most_recent(self) -> None:
        rows = [
            chat("iMessage;-;+15550007", "+15550007", 900),
            chat("iMessageLite;-;+15550007", "+15550007", 100),  # stale shadow
            chat("iMessage;-;+15550008", "+15550008", 800),  # different person
        ]
        out = dedupe_chats(rows, NORM)
        guids = {c["guid"] for c in out}
        assert guids == {"iMessage;-;+15550007", "iMessage;-;+15550008"}

    def test_none_and_empty(self) -> None:
        assert dedupe_chats(None, NORM) == []  # type: ignore[arg-type]
        assert dedupe_chats([], NORM) == []


class TestUniqueChatPagination:
    async def test_dedupes_before_offset_and_limit(self, monkeypatch) -> None:
        monkeypatch.setattr("bb_mcp.chats._LIST_PAGE_SIZE", 2)
        rows = [
            chat("iMessage;-;+15550001", "+15550001", 400),
            chat("iMessage;-;+15550002", "+15550002", 300),
            chat("iMessageLite;-;+15550001", "+15550001", 200),
            chat("iMessage;-;+15550003", "+15550003", 100),
        ]
        client = FakeClient(rows)
        first = await list_unique_chats(client, NORM, limit=2, offset=0)
        second = await list_unique_chats(client, NORM, limit=2, offset=2)
        assert [row["guid"] for row in first] == [rows[0]["guid"], rows[1]["guid"]]
        assert [row["guid"] for row in second] == [rows[3]["guid"]]

    async def test_fills_page_after_alias_is_removed(self, monkeypatch) -> None:
        monkeypatch.setattr("bb_mcp.chats._LIST_PAGE_SIZE", 2)
        rows = [
            chat("iMessage;-;+15550001", "+15550001", 400),
            chat("iMessageLite;-;+15550001", "+15550001", 300),
            chat("iMessage;-;+15550002", "+15550002", 200),
        ]
        result = await list_unique_chats(FakeClient(rows), NORM, limit=2, offset=0)
        assert [row["guid"] for row in result] == [rows[0]["guid"], rows[2]["guid"]]
