"""Critical mocked human-approval execution paths."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from governance_sdk import (
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalStatus,
    ApprovalTimeoutError,
    AuthorizationVerificationError,
    BackendUnavailableError,
    Governance,
    InvalidActionError,
)


def _approved_routes(
    governance: Governance, *, verification: dict | None = None,
    decision_status: str = "approved", pending: bool = False,
) -> list[httpx.Request]:
    calls: list[httpx.Request] = []
    action_context: dict = {}

    def evaluate(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"outcome": "approval_required"})

    def create(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        action = json.loads(request.read())
        action_context.update(action)
        return httpx.Response(201, json={
            "approval_id": "approval_1", "request_id": action["request_id"],
            "project_id": action["project_id"], "agent_id": action["agent_id"],
            "action_name": action["action_name"], "action_parameters": action["action_parameters"],
            "requested_at": action["requested_at"], "submitted_at": action["requested_at"],
            "expires_at": action["expires_at"], "status": "pending",
        })

    def status(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if pending:
            return httpx.Response(200, json={"approval_id": "approval_1", "status": "pending"})
        now = datetime.now(timezone.utc)
        return httpx.Response(200, json={
            "approval_id": "approval_1", "request_id": action_context["request_id"],
            "status": decision_status, "decision": {
                "status": decision_status, "reviewer_id": "reviewer_1",
                "reviewer_comment": "Checked ticket", "reviewed_at": now.isoformat(),
                "authorization_receipt": "receipt_1" if decision_status == "approved" else None,
            },
        })

    def verify(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=verification or {
            "valid": True, "consumed": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        })

    governance._test_handlers.update({
        "/api/v1/oversight/evaluate": evaluate,
        "/api/v1/approvals": create,
        "/api/v1/approvals/approval_1": status,
        "/api/v1/authorizations/verify": verify,
    })
    return calls


def _auto_allow_routes(governance: Governance) -> list[httpx.Request]:
    calls: list[httpx.Request] = []

    def evaluate(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"outcome": "allow", "authorization_receipt": "auto_receipt"}
        )

    def verify(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={
            "valid": True, "consumed": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        })

    governance._test_handlers.update({
        "/api/v1/oversight/evaluate": evaluate,
        "/api/v1/authorizations/verify": verify,
    })
    return calls


def test_initialization_keeps_agent_separate_from_secret() -> None:
    governance = Governance(
        api_key="secret-value", project_id="p1", base_url="https://example.test", agent_id="a1"
    )
    assert governance.config.agent_id == "a1"
    assert "secret-value" not in repr(governance.config.api_key)
    governance.close()


def test_auto_allow_executes_without_creating_approval(mocked_governance: Governance) -> None:
    calls = _auto_allow_routes(mocked_governance)
    invoked: list[int] = []

    @mocked_governance.protect(agent_id="agent_one")
    def tool(value: int) -> int:
        invoked.append(value)
        return value

    assert tool(7) == 7
    assert invoked == [7]
    assert calls[-1].url.path == "/api/v1/authorizations/verify"


def test_approval_is_verified_before_protected_execution(mocked_governance: Governance) -> None:
    calls = _approved_routes(mocked_governance)
    invoked: list[int] = []
    comments: list[str | None] = []

    @mocked_governance.protect(agent_id="agent_one", on_decision=lambda d: comments.append(d.reviewer_comment))
    def tool(value: int) -> int:
        invoked.append(value)
        return value

    assert tool(5) == 5
    assert [request.url.path for request in calls][-1] == "/api/v1/authorizations/verify"
    assert invoked == [5]
    assert comments == ["Checked ticket"]
    import json
    verify_body = json.loads(calls[-1].read())
    assert verify_body["action_parameters"] == {"value": 5}
    assert verify_body["agent_id"] == "agent_one"


def test_invalid_receipt_blocks_protected_execution(mocked_governance: Governance) -> None:
    _approved_routes(mocked_governance, verification={"valid": True, "consumed": False})
    invoked = False

    @mocked_governance.protect()
    def tool() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(AuthorizationVerificationError):
        tool()
    assert not invoked


def test_automatic_allow_without_receipt_fails_closed(mocked_governance: Governance) -> None:
    mocked_governance._test_handlers["/api/v1/oversight/evaluate"] = lambda _: httpx.Response(
        200, json={"outcome": "allow"}
    )
    invoked = False

    @mocked_governance.protect()
    def tool() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(AuthorizationVerificationError):
        tool()
    assert not invoked


def test_rejected_approval_blocks_and_exposes_reviewer_comment(mocked_governance: Governance) -> None:
    _approved_routes(mocked_governance, decision_status="rejected")
    invoked = False

    @mocked_governance.protect()
    def tool() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(ApprovalRejectedError) as error:
        tool()
    assert error.value.comment == "Checked ticket"
    assert not invoked


def test_expired_approval_blocks_execution(mocked_governance: Governance) -> None:
    _approved_routes(mocked_governance, decision_status="expired")
    invoked = False

    @mocked_governance.protect()
    def tool() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(ApprovalExpiredError):
        tool()
    assert not invoked


def test_pending_approval_times_out(mocked_governance: Governance) -> None:
    _approved_routes(mocked_governance, pending=True)
    invoked = False

    @mocked_governance.protect(timeout=0.005)
    def tool() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(ApprovalTimeoutError):
        tool()
    assert not invoked


def test_manual_request_returns_decision_without_running_tool(mocked_governance: Governance) -> None:
    _approved_routes(mocked_governance)
    ran = False
    request = mocked_governance.request_approval(
        action="remove", parameters={"id": 3}, reason="requested", agent_id="agent_one"
    )
    decision = request.wait(timeout=0.1)
    assert isinstance(decision, ApprovalDecision)
    assert decision.status == ApprovalStatus.APPROVED
    assert decision.reviewer_comment == "Checked ticket"
    assert not ran


def test_pending_request_can_be_cancelled(mocked_governance: Governance) -> None:
    observed: list[str] = []

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        return httpx.Response(201, json={
            "approval_id": "approval_cancel", "request_id": body["request_id"],
            "project_id": body["project_id"], "agent_id": body["agent_id"],
            "action_name": body["action_name"], "action_parameters": body["action_parameters"],
            "requested_at": body["requested_at"], "submitted_at": body["requested_at"],
            "expires_at": body["expires_at"], "status": "pending",
        })

    def cancel(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        return httpx.Response(204)

    mocked_governance._test_handlers.update({
        "/api/v1/approvals": create,
        "/api/v1/approvals/approval_cancel/cancel": cancel,
    })
    request = mocked_governance.request_approval(
        action="remove", parameters={"id": 4}, agent_id="agent_one"
    )
    request.cancel()
    assert observed == ["/api/v1/approvals/approval_cancel/cancel"]


def test_agent_id_is_required_without_default(mocked_governance: Governance) -> None:
    mocked_governance.config = mocked_governance.config.model_copy(update={"agent_id": None})
    mocked_governance._manager.config = mocked_governance.config

    @mocked_governance.protect()
    def tool() -> None:
        raise AssertionError("must not execute")

    with pytest.raises(InvalidActionError):
        tool()


def test_credential_parameter_is_rejected_before_network(mocked_governance: Governance) -> None:
    @mocked_governance.protect()
    def tool(api_key: str) -> None:
        raise AssertionError("must not execute")

    with pytest.raises(InvalidActionError):
        tool("secret")


def test_backend_unavailable_fails_closed(mocked_governance: Governance) -> None:
    mocked_governance._test_handlers["/api/v1/oversight/evaluate"] = lambda _: httpx.Response(503)
    ran = False

    @mocked_governance.protect()
    def tool() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(BackendUnavailableError):
        tool()
    assert not ran


def test_async_auto_allow_does_not_block(mocked_governance: Governance) -> None:
    _auto_allow_routes(mocked_governance)
    ran: list[bool] = []

    @mocked_governance.protect()
    async def tool() -> str:
        ran.append(True)
        return "ok"

    async def run() -> str:
        return await tool()

    assert asyncio.run(run()) == "ok"
    assert ran == [True]


def test_async_approval_path_verifies_before_execution(mocked_governance: Governance) -> None:
    _approved_routes(mocked_governance)
    ran: list[str] = []

    @mocked_governance.protect()
    async def tool(order_id: str) -> str:
        ran.append(order_id)
        return order_id

    async def run() -> str:
        return await tool("order-1")

    assert asyncio.run(run()) == "order-1"
    assert ran == ["order-1"]


def test_reused_idempotency_key_cannot_execute_twice(mocked_governance: Governance) -> None:
    _auto_allow_routes(mocked_governance)
    executed: list[str] = []

    @mocked_governance.protect(
        idempotency_key=lambda operation_id: "00000000-0000-0000-0000-000000000001"
    )
    def tool(operation_id: str) -> None:
        executed.append(operation_id)

    tool("operation-1")
    with pytest.raises(AuthorizationVerificationError):
        tool("operation-1")
    assert executed == ["operation-1"]


def test_concurrent_reuse_of_authorization_executes_once(mocked_governance: Governance) -> None:
    _auto_allow_routes(mocked_governance)
    executed: list[str] = []

    @mocked_governance.protect(
        idempotency_key=lambda operation_id: "00000000-0000-0000-0000-000000000002"
    )
    def tool(operation_id: str) -> None:
        executed.append(operation_id)

    def invoke() -> str:
        try:
            tool("same-operation")
            return "executed"
        except AuthorizationVerificationError:
            return "duplicate-blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: invoke(), range(2)))
    assert outcomes.count("executed") == 1
    assert outcomes.count("duplicate-blocked") == 1
    assert executed == ["same-operation"]


def test_timeout_and_rejection_exception_types() -> None:
    assert ApprovalTimeoutError.__name__ == "ApprovalTimeoutError"
    assert ApprovalExpiredError.__name__ == "ApprovalExpiredError"
    assert ApprovalRejectedError(comment="reviewer note").comment == "reviewer note"

