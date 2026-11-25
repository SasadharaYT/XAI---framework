"""Shared mocked SDK client fixtures."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from governance_sdk import Governance


@pytest.fixture
def mocked_governance() -> Any:
    """Create a client with synchronous and asynchronous mock transports."""
    handlers: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        handler = handlers.get(request.url.path)
        if handler is None:
            return httpx.Response(500, json={"error": "unexpected path"})
        return handler(request)

    client = Governance(
        api_key="test-key", project_id="project_test", base_url="https://governance.test",
        agent_id="agent_default", approval_timeout=30.0, poll_interval=0.001,
        max_poll_interval=0.002,
    )
    client._http._sync.close()
    client._http._sync = httpx.Client(
        base_url="https://governance.test", transport=httpx.MockTransport(dispatch),
        headers={"Authorization": "Bearer test-key"},
    )
    client._http._async = httpx.AsyncClient(
        base_url="https://governance.test", transport=httpx.MockTransport(dispatch),
        headers={"Authorization": "Bearer test-key"},
    )
    client._test_handlers = handlers
    yield client
    client._http.close()
    asyncio.run(client._http._async.aclose())

