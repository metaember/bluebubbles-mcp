"""Tests for the write-recipient allowlist (bb_mcp.policy)."""

from __future__ import annotations

import httpx
import pytest
import respx

from bb_mcp.client import BlueBubblesClient
from bb_mcp.policy import (
    AccessDenied,
    Allowlist,
    Guard,
    address_from_guid,
    normalize_address,
)

BASE_URL = "http://bb.local:1234"
API = f"{BASE_URL}/api/v1"


def ok_json(data, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"status": 200, "data": data})


# ===========================================================================
# normalize_address
# ===========================================================================

class TestNormalizeAddress:
    def test_email_lowercased_and_stripped(self) -> None:
        assert normalize_address("  Alice@Example.COM ") == "alice@example.com"

    @pytest.mark.parametrize(
        "raw",
        ["+15551234567", "5551234567", "(555) 123-4567", "+1 555-123-4567"],
    )
    def test_phone_formats_collapse_to_e164(self, raw: str) -> None:
        assert normalize_address(raw, region="US") == "+15551234567"

    def test_unparseable_phone_falls_back(self) -> None:
        # A string phonenumbers can't parse at all -> lowercased fallback,
        # which is still deterministic so allowlist comparison holds.
        assert normalize_address("Nope", region="US") == "nope"

    def test_region_changes_parse(self) -> None:
        # A UK national number only resolves with the GB region.
        assert normalize_address("020 7946 0958", region="GB") == "+442079460958"


# ===========================================================================
# address_from_guid
# ===========================================================================

class TestAddressFromGuid:
    def test_one_on_one_returns_address(self) -> None:
        assert address_from_guid("iMessage;-;+15551234567") == "+15551234567"

    def test_one_on_one_email(self) -> None:
        assert address_from_guid("iMessage;-;a@b.com") == "a@b.com"

    def test_group_returns_none(self) -> None:
        assert address_from_guid("iMessage;+;chat123") is None

    def test_unexpected_shape_returns_none(self) -> None:
        assert address_from_guid("not-a-guid") is None


# ===========================================================================
# Allowlist.from_env
# ===========================================================================

class TestAllowlistFromEnv:
    def test_absent_is_unrestricted(self) -> None:
        al = Allowlist.from_env({}, "BLUEBUBBLES_WRITE_ALLOWLIST")
        assert al.restricted is False
        assert al.contains("+15551234567") is True

    def test_empty_string_denies_all(self) -> None:
        al = Allowlist.from_env(
            {"BLUEBUBBLES_WRITE_ALLOWLIST": ""}, "BLUEBUBBLES_WRITE_ALLOWLIST"
        )
        assert al.restricted is True
        assert al.contains("+15551234567") is False

    def test_entries_are_normalized(self) -> None:
        al = Allowlist.from_env(
            {"BLUEBUBBLES_WRITE_ALLOWLIST": "(555) 123-4567, Bob@Example.com"},
            "BLUEBUBBLES_WRITE_ALLOWLIST",
        )
        assert al.contains("+15551234567") is True
        assert al.contains("bob@example.com") is True
        assert al.contains("+15559999999") is False

    def test_blank_entries_ignored(self) -> None:
        al = Allowlist.from_env(
            {"BLUEBUBBLES_WRITE_ALLOWLIST": "+15551234567, , ,"},
            "BLUEBUBBLES_WRITE_ALLOWLIST",
        )
        assert al.allowed == frozenset({"+15551234567"})

    def test_region_env_honored(self) -> None:
        al = Allowlist.from_env(
            {
                "BLUEBUBBLES_WRITE_ALLOWLIST": "020 7946 0958",
                "BLUEBUBBLES_ALLOWLIST_REGION": "GB",
            },
            "BLUEBUBBLES_WRITE_ALLOWLIST",
        )
        assert al.contains("+442079460958") is True

    def test_rejected_returns_offenders(self) -> None:
        al = Allowlist.from_env(
            {"BLUEBUBBLES_WRITE_ALLOWLIST": "+15551234567"},
            "BLUEBUBBLES_WRITE_ALLOWLIST",
        )
        assert al.rejected(["+15551234567", "+15559999999"]) == {"+15559999999"}


# ===========================================================================
# Guard — address checks (synchronous, no API)
# ===========================================================================

class TestGuardCheckAddress:
    def _guard(self, raw: str | None) -> Guard:
        env = {} if raw is None else {"BLUEBUBBLES_WRITE_ALLOWLIST": raw}
        al = Allowlist.from_env(env, "BLUEBUBBLES_WRITE_ALLOWLIST")
        return Guard(al, client=None)  # type: ignore[arg-type]

    def test_unrestricted_allows_anything(self) -> None:
        self._guard(None).check_address("+15559999999")  # no raise

    def test_allowed_address_passes(self) -> None:
        self._guard("+15551234567").check_address("(555) 123-4567")  # no raise

    def test_blocked_address_raises(self) -> None:
        with pytest.raises(AccessDenied):
            self._guard("+15551234567").check_address("+15559999999")


# ===========================================================================
# Guard — chat checks (resolve participants)
# ===========================================================================

class TestGuardCheckChat:
    @pytest.fixture()
    def mock_api(self):
        with respx.mock(assert_all_called=False) as router:
            yield router

    @pytest.fixture()
    def client(self) -> BlueBubblesClient:
        return BlueBubblesClient(base_url=BASE_URL, password="pw", timeout=5.0)

    def _guard(self, raw: str | None, client: BlueBubblesClient) -> Guard:
        env = {} if raw is None else {"BLUEBUBBLES_WRITE_ALLOWLIST": raw}
        al = Allowlist.from_env(env, "BLUEBUBBLES_WRITE_ALLOWLIST")
        return Guard(al, client)

    async def test_unrestricted_skips_api(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/chat/iMessage;+;chat1").mock(
            return_value=ok_json({})
        )
        await self._guard(None, client).check_chat("iMessage;+;chat1")
        assert not route.called  # no resolution when there's no allowlist

    async def test_one_on_one_allowed_without_api(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/chat/iMessage;-;+15551234567").mock(
            return_value=ok_json({})
        )
        await self._guard("+15551234567", client).check_chat(
            "iMessage;-;+15551234567"
        )
        assert not route.called  # address parsed straight from the GUID

    async def test_one_on_one_blocked(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        with pytest.raises(AccessDenied):
            await self._guard("+15551234567", client).check_chat(
                "iMessage;-;+15559999999"
            )

    async def test_group_all_allowed(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/chat/iMessage;+;chat1").mock(
            return_value=ok_json(
                {"participants": [
                    {"address": "+15551234567"},
                    {"address": "bob@example.com"},
                ]}
            )
        )
        guard = self._guard("+15551234567, bob@example.com", client)
        await guard.check_chat("iMessage;+;chat1")  # no raise

    async def test_group_one_member_not_allowed_blocks(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/chat/iMessage;+;chat1").mock(
            return_value=ok_json(
                {"participants": [
                    {"address": "+15551234567"},
                    {"address": "+15559999999"},
                ]}
            )
        )
        guard = self._guard("+15551234567", client)
        with pytest.raises(AccessDenied):
            await guard.check_chat("iMessage;+;chat1")

    async def test_group_resolution_is_cached(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/chat/iMessage;+;chat1").mock(
            return_value=ok_json({"participants": [{"address": "+15551234567"}]})
        )
        guard = self._guard("+15551234567", client)
        await guard.check_chat("iMessage;+;chat1")
        await guard.check_chat("iMessage;+;chat1")
        assert route.call_count == 1  # second check hits the cache

    async def test_unresolvable_group_fails_closed(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/chat/iMessage;+;chat1").mock(
            return_value=ok_json({"participants": []})
        )
        guard = self._guard("+15551234567", client)
        with pytest.raises(AccessDenied):
            await guard.check_chat("iMessage;+;chat1")
