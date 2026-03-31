from __future__ import annotations

import pytest
import respx

from bb_mcp.client import BlueBubblesClient

BASE_URL = "http://bb.local:1234"
PASSWORD = "test-secret"


@pytest.fixture()
def mock_api():
    """Yield a `respx` router scoped to the test; auto-started/stopped."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture()
def client(mock_api: respx.Router) -> BlueBubblesClient:
    """Return a client pointed at the mocked base URL."""
    return BlueBubblesClient(base_url=BASE_URL, password=PASSWORD, timeout=5.0)
