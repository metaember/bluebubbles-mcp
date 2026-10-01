"""The MCP contract must preserve the ordering used by archive pagination."""
from unittest.mock import AsyncMock

import pytest

from bb_mcp import server


@pytest.mark.parametrize("sort", ["ASC", "DESC"])
async def test_search_forwards_order_and_snapshot(monkeypatch, sort):
    client = AsyncMock()
    client.search_messages.return_value = []
    present = AsyncMock(return_value="[]")
    monkeypatch.setattr(server, "_bb", lambda ctx: client)
    monkeypatch.setattr(server, "_present_messages", present)
    assert await server.search_messages(None, limit=20, offset=40, after=100, before=200, sort=sort) == "[]"
    client.search_messages.assert_awaited_once_with(query=None, chat_guid=None, limit=20,
        offset=40, after=100, before=200, sort=sort)


async def test_search_default_stays_descending(monkeypatch):
    client = AsyncMock()
    client.search_messages.return_value = []
    monkeypatch.setattr(server, "_bb", lambda ctx: client)
    monkeypatch.setattr(server, "_present_messages", AsyncMock(return_value="[]"))
    await server.search_messages(None)
    assert client.search_messages.call_args.kwargs["sort"] == "DESC"


async def test_order_is_exposed_in_mcp_schema():
    tools = await server.mcp.list_tools()
    search = next(tool for tool in tools if tool.name == "search_messages")
    assert search.inputSchema["properties"]["sort"]["enum"] == ["ASC", "DESC"]
    assert search.inputSchema["properties"]["sort"]["default"] == "DESC"
