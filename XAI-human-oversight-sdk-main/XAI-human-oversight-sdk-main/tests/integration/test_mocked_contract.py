"""Contract-shaped tests using mocked transport only."""

import httpx

from governance_sdk import Governance


def test_registration_uses_project_scope_and_idempotency(mocked_governance: Governance) -> None:
    observed: list[httpx.Request] = []

    def register(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(201, json={"agent_id": "agent_registered", "project_id": "project_test"})

    mocked_governance._test_handlers["/api/v1/agents"] = register
    assert mocked_governance.register_agent("Agent A") == "agent_registered"
    request = observed[0]
    assert request.headers["Idempotency-Key"]
    assert b'"project_id":"project_test"' in request.read()

