"""Tests for Private API capability detection and toolset adaptation."""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from bb_mcp.capabilities import (
    PRIVATE_API_TOOLS,
    my_address_from_info,
    parse_override,
    private_api_from_info,
)
from bb_mcp.server import lifespan

BASE_URL = "http://bb.local:1234"
API = f"{BASE_URL}/api/v1"


def ok_json(data, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"status": 200, "data": data})


async def _noop() -> str:
    return "ok"


# ===========================================================================
# parse_override
# ===========================================================================

class TestParseOverride:
    def test_unset_is_auto(self) -> None:
        assert parse_override(None) is None

    @pytest.mark.parametrize("v", ["auto", "", "  ", "maybe"])
    def test_unrecognized_is_auto(self, v: str) -> None:
        assert parse_override(v) is None

    @pytest.mark.parametrize("v", ["true", "TRUE", " 1 ", "yes", "on"])
    def test_truthy(self, v: str) -> None:
        assert parse_override(v) is True

    @pytest.mark.parametrize("v", ["false", "False", "0", "no", "off"])
    def test_falsy(self, v: str) -> None:
        assert parse_override(v) is False


# ===========================================================================
# private_api_from_info
# ===========================================================================

class TestPrivateApiFromInfo:
    def test_detection_failure_preserves_default(self) -> None:
        assert private_api_from_info(None) is True

    def test_missing_flag_preserves_default(self) -> None:
        # An older server that doesn't report the flag shouldn't get pruned.
        assert private_api_from_info({"server_version": "1.0"}) is True

    def test_explicitly_off(self) -> None:
        assert private_api_from_info({"private_api": False}) is False

    def test_enabled(self) -> None:
        assert private_api_from_info({"private_api": True}) is True

    def test_enabled_but_helper_disconnected(self) -> None:
        assert private_api_from_info(
            {"private_api": True, "helper_connected": False}
        ) is False

    def test_enabled_and_helper_connected(self) -> None:
        assert private_api_from_info(
            {"private_api": True, "helper_connected": True}
        ) is True


# ===========================================================================
# my_address_from_info
# ===========================================================================

class TestMyAddressFromInfo:
    def test_none(self) -> None:
        assert my_address_from_info(None) is None

    def test_absent(self) -> None:
        assert my_address_from_info({"private_api": True}) is None

    def test_icloud_fallback(self) -> None:
        assert my_address_from_info({"detected_icloud": "me@icloud.com"}) == "me@icloud.com"

    def test_prefers_imessage_handle(self) -> None:
        assert my_address_from_info(
            {"detected_imessage": "+15551112222", "detected_icloud": "me@icloud.com"}
        ) == "+15551112222"


# ===========================================================================
# lifespan — capability detection + tool pruning
# ===========================================================================

@pytest.fixture()
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture(autouse=True)
def _bb_env(monkeypatch):
    monkeypatch.setenv("BLUEBUBBLES_URL", BASE_URL)
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "pw")
    monkeypatch.delenv("BLUEBUBBLES_PRIVATE_API", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_MY_ADDRESS", raising=False)
    monkeypatch.delenv("BLUEBUBBLES_WRITE_ALLOWLIST", raising=False)


@pytest.fixture(autouse=True)
def _server_info_route(mock_api: respx.Router):
    """Default /server/info mock so the lifespan's startup read always resolves.

    Individual tests override it with `.mock(...)` to assert specific behavior.
    """
    return mock_api.get(f"{API}/server/info").mock(return_value=ok_json({}))


def _server_with_gated_tools() -> FastMCP:
    server = FastMCP("test")
    for name in (*PRIVATE_API_TOOLS, "send_message"):
        server.add_tool(_noop, name=name)
    return server


async def _tool_names(server: FastMCP) -> set[str]:
    return {t.name for t in await server.list_tools()}


class TestLifespanPruning:
    async def test_forced_off_prunes_and_keeps_basics(self, monkeypatch) -> None:
        monkeypatch.setenv("BLUEBUBBLES_PRIVATE_API", "false")
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["private_api"] is False
            names = await _tool_names(server)
        assert names.isdisjoint(PRIVATE_API_TOOLS)
        assert "send_message" in names  # basic send survives via AppleScript

    async def test_forced_on_overrides_server_report(
        self, monkeypatch, mock_api: respx.Router
    ) -> None:
        # Server reports the Private API off, but the operator forces it on.
        monkeypatch.setenv("BLUEBUBBLES_PRIVATE_API", "true")
        mock_api.get(f"{API}/server/info").mock(
            return_value=ok_json({"private_api": False})
        )
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["private_api"] is True
            assert set(PRIVATE_API_TOOLS) <= await _tool_names(server)

    async def test_auto_detects_enabled(self, mock_api: respx.Router) -> None:
        mock_api.get(f"{API}/server/info").mock(
            return_value=ok_json({"private_api": True})
        )
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["private_api"] is True
            assert set(PRIVATE_API_TOOLS) <= await _tool_names(server)

    async def test_auto_detects_disabled(self, mock_api: respx.Router) -> None:
        mock_api.get(f"{API}/server/info").mock(
            return_value=ok_json({"private_api": False})
        )
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["private_api"] is False
            assert (await _tool_names(server)).isdisjoint(PRIVATE_API_TOOLS)

    async def test_auto_tolerates_server_info_failure(
        self, mock_api: respx.Router
    ) -> None:
        # A /server/info blip must not take the server down or hide tools.
        mock_api.get(f"{API}/server/info").mock(
            return_value=httpx.Response(500, json={"status": 500})
        )
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["private_api"] is True
            assert set(PRIVATE_API_TOOLS) <= await _tool_names(server)


class TestLifespanMyAddress:
    async def test_from_server_info(self, mock_api: respx.Router) -> None:
        mock_api.get(f"{API}/server/info").mock(
            return_value=ok_json({"detected_icloud": "me@icloud.com"})
        )
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["my_address"] == "me@icloud.com"

    async def test_env_override_wins(
        self, monkeypatch, mock_api: respx.Router
    ) -> None:
        monkeypatch.setenv("BLUEBUBBLES_MY_ADDRESS", "+15550001111")
        mock_api.get(f"{API}/server/info").mock(
            return_value=ok_json({"detected_icloud": "me@icloud.com"})
        )
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["my_address"] == "+15550001111"

    async def test_none_when_undetectable(self, mock_api: respx.Router) -> None:
        mock_api.get(f"{API}/server/info").mock(return_value=ok_json({}))
        server = _server_with_gated_tools()
        async with lifespan(server) as ctx:
            assert ctx["my_address"] is None
