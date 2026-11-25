"""Turn backend approval resources into verified decisions."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from ..exceptions import (
    ApprovalExpiredError,
    ApprovalTimeoutError,
    AuthorizationVerificationError,
    BackendUnavailableError,
    GovernanceError,
)
from ..models import ActionRequest, ApprovalDecision, ApprovalRequest, ApprovalStatus
from ..transport import GovernanceHTTPClient
from ..transport.endpoints import approval_cancel, approval_status
from .wait_strategy import PollingWaitStrategy


class ApprovalHandler:
    """Create, wait for, cancel, and verify approval requests."""

    def __init__(self, http: GovernanceHTTPClient, wait_strategy: PollingWaitStrategy) -> None:
        self._http = http
        self._wait_strategy = wait_strategy

    def attach(self, request: ApprovalRequest) -> ApprovalRequest:
        """Attach this handler to a validated request handle."""
        request._waiter = self
        return request

    def wait(self, request: ApprovalRequest, timeout: float | None = None) -> ApprovalDecision:
        """Synchronously poll and verify a final decision."""
        return self._wait_sync(request, timeout)

    async def await_decision(
        self, request: ApprovalRequest, timeout: float | None = None
    ) -> ApprovalDecision:
        """Asynchronously poll and verify a final decision."""
        return await self._wait_async(request, timeout)

    def cancel(self, request: ApprovalRequest) -> None:
        """Cancel a pending request."""
        self._http.request("POST", approval_cancel(request.approval_id), json={"request_id": request.request_id})

    async def acancel(self, request: ApprovalRequest) -> None:
        """Cancel a pending request asynchronously."""
        await self._http.arequest("POST", approval_cancel(request.approval_id), json={"request_id": request.request_id})

    def _wait_sync(self, request: ApprovalRequest, timeout: float | None) -> ApprovalDecision:
        effective_timeout, expires_first = self._effective_timeout(request, timeout)
        deadline = time.monotonic() + effective_timeout

        def poll() -> ApprovalDecision | None:
            data = self._http.request("GET", approval_status(request.approval_id))
            return self._terminal(request, data)

        decision = self._wait_strategy.sync_wait(deadline, poll)
        if decision is None:
            if expires_first:
                raise ApprovalExpiredError("Approval expired before a decision was received")
            raise ApprovalTimeoutError("Approval did not resolve before the configured timeout")
        return decision  # type: ignore[return-value]

    async def _wait_async(self, request: ApprovalRequest, timeout: float | None) -> ApprovalDecision:
        effective_timeout, expires_first = self._effective_timeout(request, timeout)
        deadline = time.monotonic() + effective_timeout

        async def poll() -> ApprovalDecision | None:
            data = await self._http.arequest("GET", approval_status(request.approval_id))
            return await self._aterminal(request, data)

        decision = await self._wait_strategy.async_wait(deadline, poll)
        if decision is None:
            if expires_first:
                raise ApprovalExpiredError("Approval expired before a decision was received")
            raise ApprovalTimeoutError("Approval did not resolve before the configured timeout")
        return decision  # type: ignore[return-value]

    @staticmethod
    def _effective_timeout(request: ApprovalRequest, timeout: float | None) -> tuple[float, bool]:
        expires_in = max(0.0, (request.expires_at - datetime.now(timezone.utc)).total_seconds())
        if timeout is None:
            return expires_in, True
        return min(timeout, expires_in), expires_in <= timeout

    def _terminal(self, request: ApprovalRequest, response: Any) -> ApprovalDecision | None:
        payload = response.get("decision", response) if isinstance(response, dict) else response
        try:
            status = ApprovalStatus(payload["status"])
        except (KeyError, TypeError, ValueError):
            if isinstance(response, dict) and response.get("status") == "pending":
                return None
            raise BackendUnavailableError("Governance backend returned an invalid approval status") from None
        if status == ApprovalStatus.PENDING:
            return None
        decision = self._parse_decision(request, payload)
        self._verify_if_approved(request, decision)
        return decision

    async def _aterminal(self, request: ApprovalRequest, response: Any) -> ApprovalDecision | None:
        payload = response.get("decision", response) if isinstance(response, dict) else response
        try:
            status = ApprovalStatus(payload["status"])
        except (KeyError, TypeError, ValueError):
            if isinstance(response, dict) and response.get("status") == "pending":
                return None
            raise BackendUnavailableError("Governance backend returned an invalid approval status") from None
        if status == ApprovalStatus.PENDING:
            return None
        decision = self._parse_decision(request, payload)
        await self._averify_if_approved(request, decision)
        return decision

    @staticmethod
    def _parse_decision(request: ApprovalRequest, payload: dict[str, Any]) -> ApprovalDecision:
        returned_approval_id = payload.get("approval_id", request.approval_id)
        returned_request_id = payload.get("request_id", request.request_id)
        if returned_approval_id != request.approval_id or returned_request_id != request.request_id:
            raise BackendUnavailableError("Governance backend returned a decision for a different request")
        normalized = {
            **payload,
            "approval_id": payload.get("approval_id", request.approval_id),
            "request_id": payload.get("request_id", request.request_id),
            "approved": payload.get("status") == ApprovalStatus.APPROVED.value,
        }
        try:
            return ApprovalDecision.model_validate(normalized)
        except (ValidationError, TypeError):
            raise BackendUnavailableError("Governance backend returned an invalid decision") from None

    def _verify_if_approved(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        if decision.status != ApprovalStatus.APPROVED:
            return
        self._verify(request, decision)

    async def _averify_if_approved(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        if decision.status != ApprovalStatus.APPROVED:
            return
        await self._averify(request, decision)

    def _verify(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        receipt = decision.authorization_receipt
        if not receipt:
            raise AuthorizationVerificationError("Approved decision did not include an authorization receipt")
        self.verify_action(request, receipt, approval_id=request.approval_id)

    def verify_action(
        self, action: ActionRequest | ApprovalRequest, receipt: str, approval_id: str | None = None
    ) -> None:
        """Atomically verify and consume an automatic or human authorization."""
        if not receipt:
            raise AuthorizationVerificationError("Authorization receipt is missing")
        try:
            data = self._http.request(
                "POST", "/api/v1/authorizations/verify",
                headers={"Idempotency-Key": action.request_id},
                json={"request_id": action.request_id, "approval_id": approval_id,
                      "authorization_receipt": receipt, "project_id": action.project_id,
                      "agent_id": action.agent_id, "action_name": action.action_name,
                      "action_parameters": action.action_parameters},
            )
        except ApprovalExpiredError:
            raise
        except GovernanceError:
            raise AuthorizationVerificationError("Authorization receipt could not be consumed") from None
        self._assert_verified(data)

    async def _averify(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        receipt = decision.authorization_receipt
        if not receipt:
            raise AuthorizationVerificationError("Approved decision did not include an authorization receipt")
        await self.averify_action(request, receipt, approval_id=request.approval_id)

    async def averify_action(
        self, action: ActionRequest | ApprovalRequest, receipt: str, approval_id: str | None = None
    ) -> None:
        """Atomically verify and consume an authorization asynchronously."""
        if not receipt:
            raise AuthorizationVerificationError("Authorization receipt is missing")
        try:
            data = await self._http.arequest(
                "POST", "/api/v1/authorizations/verify",
                headers={"Idempotency-Key": action.request_id},
                json={"request_id": action.request_id, "approval_id": approval_id,
                      "authorization_receipt": receipt, "project_id": action.project_id,
                      "agent_id": action.agent_id, "action_name": action.action_name,
                      "action_parameters": action.action_parameters},
            )
        except ApprovalExpiredError:
            raise
        except GovernanceError:
            raise AuthorizationVerificationError("Authorization receipt could not be consumed") from None
        self._assert_verified(data)

    @staticmethod
    def _assert_verified(data: Any) -> None:
        if not isinstance(data, dict) or data.get("valid") is not True or data.get("consumed") is not True:
            raise AuthorizationVerificationError("Authorization receipt was not valid and consumed")
        if not isinstance(data.get("expires_at"), str):
            raise AuthorizationVerificationError("Authorization response omitted receipt expiry")
        try:
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise AuthorizationVerificationError("Authorization response had an invalid expiry") from None
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise AuthorizationVerificationError("Authorization expiry must include a timezone")
        if expires <= datetime.now(timezone.utc):
            raise ApprovalExpiredError("Authorization receipt has expired")

