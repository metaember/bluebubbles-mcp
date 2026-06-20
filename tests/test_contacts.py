"""Tests for contact-name resolution and response enrichment (bb_mcp.contacts)."""

from __future__ import annotations

import httpx
import pytest
import respx

from bb_mcp.client import BlueBubblesClient
from bb_mcp.contacts import (
    ContactResolver,
    collect_addresses,
    contact_addresses,
    contact_display_name,
    inject_names,
)

BASE_URL = "http://bb.local:1234"
API = f"{BASE_URL}/api/v1"

ALICE = {
    "displayName": "Alice Smith",
    "phoneNumbers": [{"address": "+15551234567"}],
    "emails": [{"address": "alice@example.com"}],
}
BOB = {
    "displayName": "",
    "firstName": "Bob",
    "lastName": "Jones",
    "phoneNumbers": [{"address": "+15557654321"}],
    "emails": [],
}


def ok_json(data, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"status": 200, "data": data})


# ===========================================================================
# Pure helpers
# ===========================================================================

class TestDisplayName:
    def test_prefers_display_name(self) -> None:
        assert contact_display_name(ALICE) == "Alice Smith"

    def test_falls_back_to_first_last(self) -> None:
        assert contact_display_name(BOB) == "Bob Jones"

    def test_falls_back_to_nickname(self) -> None:
        assert contact_display_name({"nickname": "Buddy"}) == "Buddy"

    def test_none_when_empty(self) -> None:
        assert contact_display_name({"displayName": "  "}) is None


class TestContactAddresses:
    def test_collects_phones_and_emails(self) -> None:
        assert contact_addresses(ALICE) == ["+15551234567", "alice@example.com"]

    def test_handles_string_entries(self) -> None:
        assert contact_addresses({"phoneNumbers": ["+15550001111"]}) == ["+15550001111"]

    def test_empty(self) -> None:
        assert contact_addresses({}) == []


class TestCollectAndInject:
    def test_collect_walks_nested(self) -> None:
        payload = {
            "participants": [{"address": "+15551234567"}, {"address": "a@b.com"}],
            "lastMessage": {"handle": {"address": "+15551234567"}},
        }
        assert collect_addresses(payload) == {"+15551234567", "a@b.com"}

    def test_inject_annotates_matching_handles(self) -> None:
        payload = {
            "participants": [{"address": "+15551234567"}, {"address": "a@b.com"}],
            "lastMessage": {"handle": {"address": "+15551234567"}},
        }
        inject_names(payload, {"+15551234567": "Alice"})
        assert payload["participants"][0]["contactName"] == "Alice"
        assert payload["lastMessage"]["handle"]["contactName"] == "Alice"
        assert "contactName" not in payload["participants"][1]


# ===========================================================================
# ContactResolver
# ===========================================================================

@pytest.fixture()
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture()
def client() -> BlueBubblesClient:
    return BlueBubblesClient(base_url=BASE_URL, password="pw", timeout=5.0)


class TestResolverNamesFor:
    async def test_resolves(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/contact/query").mock(return_value=ok_json([ALICE]))
        names = await ContactResolver(client).names_for(["+15551234567"])
        assert names == {"+15551234567": "Alice Smith"}

    async def test_normalizes_and_caches(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/contact/query").mock(
            return_value=ok_json([ALICE])
        )
        resolver = ContactResolver(client)
        await resolver.names_for(["+15551234567"])
        # A differently-formatted spelling of the same number hits the cache...
        names = await resolver.names_for(["(555) 123-4567"])
        assert route.call_count == 1
        assert names == {"(555) 123-4567": "Alice Smith"}  # keyed by the input

    async def test_unknown_is_cached_as_miss(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/contact/query").mock(return_value=ok_json([]))
        resolver = ContactResolver(client)
        assert await resolver.names_for(["+15559999999"]) == {}
        assert await resolver.names_for(["+15559999999"]) == {}
        assert route.call_count == 1  # second lookup served from the miss cache

    async def test_query_failure_is_tolerated(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/contact/query").mock(
            return_value=httpx.Response(500, json={"status": 500})
        )
        assert await ContactResolver(client).names_for(["+15551234567"]) == {}


class TestResolverFind:
    async def test_substring_case_insensitive(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/contact").mock(return_value=ok_json([ALICE, BOB]))
        matches = await ContactResolver(client).find("ALI")
        assert [contact_display_name(c) for c in matches] == ["Alice Smith"]

    async def test_find_warms_name_cache(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/contact").mock(return_value=ok_json([ALICE]))
        query = mock_api.post(f"{API}/contact/query").mock(return_value=ok_json([]))
        resolver = ContactResolver(client)
        await resolver.find("alice")
        names = await resolver.names_for(["+15551234567"])
        assert names == {"+15551234567": "Alice Smith"}
        assert not query.called  # served from the cache find() warmed

    async def test_full_list_fetched_once(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/contact").mock(return_value=ok_json([ALICE]))
        resolver = ContactResolver(client)
        await resolver.find("alice")
        await resolver.find("bob")
        assert route.call_count == 1
