"""The HTTP transport (MCP_TRANSPORT=streamable-http) wires up without error."""

import logging

from bb_mcp.server import mcp


def test_streamable_http_app_builds() -> None:
    assert mcp.streamable_http_app() is not None


def test_http_client_request_logs_are_disabled() -> None:
    # BlueBubbles puts its credential in URLs; transport logs must not emit them.
    assert logging.getLogger("httpx").getEffectiveLevel() > logging.CRITICAL
    assert logging.getLogger("httpcore").getEffectiveLevel() > logging.CRITICAL
