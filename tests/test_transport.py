"""The HTTP transport (MCP_TRANSPORT=streamable-http) wires up without error."""

from bb_mcp.server import mcp


def test_streamable_http_app_builds() -> None:
    assert mcp.streamable_http_app() is not None
