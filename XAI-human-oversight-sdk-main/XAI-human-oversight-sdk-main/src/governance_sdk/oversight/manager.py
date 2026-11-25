"""Create and evaluate human-oversight requests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from ..config import GovernanceConfig
from ..exceptions import BackendUnavailableError, GovernanceError, InvalidActionError
from ..models import ActionRequest, ApprovalRequest
from ..models.action import EvaluationResult
from ..transport import GovernanceHTTPClient
from ..transport.endpoints import AGENTS, APPROVALS, EVALUATE
from ..utils.serialization import json_safe, validate_safe_payload
from .approval_handler import ApprovalHandler


class ApprovalManager:
    """Own request construction and backend evaluation/approval creation."""

    def __init__(
        self, config: GovernanceConfig, http: GovernanceHTTPClient, handler: ApprovalHandler
    ) -> None:
        self.config = config
        self.http = http
        self.handler = handler

    def register_agent(self, name: str, description: str = "", metadata: dict[str, Any] | None = None) -> str:
        """Register an agent in this project and return its backend-issued ID."""
        request_id = str(uuid.uuid4())
        body = {"project_id": self.config.project_id, "name": name, "description": description,
                "metadata": json_safe(metadata or {})}
        validate_safe_payload(body, "agent")
        data = self.http.request("POST", AGENTS, headers={"Idempotency-Key": request_id}, json=body)
        if not isinstance(data, dict) or not isinstance(data.get("agent_id", data.get("id")), str):
            raise BackendUnavailableError("Governance backend returned an invalid agent registration")
        return data.get("agent_id", data.get("id"))

    async def aregister_agent(
        self, name: str, description: str = "", metadata: dict[str, Any] | None = None
    ) -> str:
        """Register an agent asynchronously and return its backend-issued ID."""
        request_id = str(uuid.uuid4())
        body = {"project_id": self.config.project_id, "name": name, "description": description,
                "metadata": json_safe(metadata or {})}
        validate_safe_payload(body, "agent")
        data = await self.http.arequest("POST", AGENTS, headers={"Idempotency-Key": request_id}, json=body)
        if not isinstance(data, dict) or not isinstance(data.get("agent_id", data.get("id")), str):
            raise BackendUnavailableError("Governance backend returned an invalid agent registration")
        return data.get("agent_id", data.get("id"))

    def build_action(
        self,
        *,
        agent_id: str,
        action_name: str,
        action_parameters: dict[str, Any],
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        agent_name: str | None = None,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> ActionRequest:
        """Validate and build a uniquely identified action request."""
        if not agent_id:
            raise InvalidActionError("Every action must specify an agent_id")
        if request_id is not None:
            try:
                request_id = str(uuid.UUID(request_id))
            except (ValueError, AttributeError, TypeError):
                raise InvalidActionError("request_id/idempotency key must be a UUID") from None
        params = json_safe(action_parameters)
        context = json_safe(metadata or {})
        validate_safe_payload(params)
        validate_safe_payload(context, "metadata")
        requested_at = datetime.now(timezone.utc)
        try:
            return ActionRequest(
                request_id=request_id or str(uuid.uuid4()),
                project_id=self.config.project_id,
                agent_id=agent_id,
                agent_name=agent_name,
                action_name=action_name,
                action_parameters=params,
                reason=reason,
                requested_at=requested_at,
                expires_at=requested_at + timedelta(
                    seconds=timeout if timeout is not None else self.config.approval_timeout
                ),
                metadata=context,
            )
        except ValidationError:
            raise InvalidActionError("Action request is invalid") from None

    def evaluate(self, action: ActionRequest) -> EvaluationResult:
        """Ask backend to evaluate whether approval is required."""
        try:
            data = self.http.request(
                "POST", EVALUATE, headers={"Idempotency-Key": action.request_id},
                json=action.model_dump(mode="json"),
            )
            return EvaluationResult.model_validate(data)
        except ValidationError:
            raise BackendUnavailableError("Governance backend returned an invalid evaluation") from None

    async def aevaluate(self, action: ActionRequest) -> EvaluationResult:
        """Evaluate a protected action asynchronously."""
        try:
            data = await self.http.arequest(
                "POST", EVALUATE, headers={"Idempotency-Key": action.request_id},
                json=action.model_dump(mode="json"),
            )
            return EvaluationResult.model_validate(data)
        except ValidationError:
            raise BackendUnavailableError("Governance backend returned an invalid evaluation") from None

    def request_approval(
        self, *, agent_id: str, action: str, parameters: dict[str, Any], reason: str | None = None,
        metadata: dict[str, Any] | None = None, agent_name: str | None = None,
        timeout: float | None = None, request_id: str | None = None,
    ) -> ApprovalRequest:
        """Submit a manual approval request without executing its action."""
        action_request = self.build_action(
            agent_id=agent_id, action_name=action, action_parameters=parameters, reason=reason,
            metadata=metadata, agent_name=agent_name, timeout=timeout, request_id=request_id,
        )
        return self.create_for_action(action_request)

    async def arequest_approval(self, **kwargs: Any) -> ApprovalRequest:
        """Submit a manual approval request asynchronously."""
        action_request = self.build_action(
            agent_id=kwargs["agent_id"], action_name=kwargs["action"],
            action_parameters=kwargs["parameters"], reason=kwargs.get("reason"),
            metadata=kwargs.get("metadata"), agent_name=kwargs.get("agent_name"),
            timeout=kwargs.get("timeout"), request_id=kwargs.get("request_id"),
        )
        return await self.acreate_for_action(action_request)

    def create_for_action(self, action: ActionRequest, approval_id: str | None = None) -> ApprovalRequest:
        """Create an approval record or attach to one already created by evaluation."""
        if approval_id:
            data = {"approval_id": approval_id, "request_id": action.request_id,
                    "submitted_at": action.requested_at.isoformat(), **action.model_dump(mode="json")}
        else:
            data = self.http.request(
                "POST", APPROVALS, headers={"Idempotency-Key": action.request_id},
                json=action.model_dump(mode="json"),
            )
        return self._approval_handle(action, data)

    async def acreate_for_action(
        self, action: ActionRequest, approval_id: str | None = None
    ) -> ApprovalRequest:
        """Create or attach to an approval request asynchronously."""
        if approval_id:
            data = {"approval_id": approval_id, "request_id": action.request_id,
                    "submitted_at": action.requested_at.isoformat(), **action.model_dump(mode="json")}
        else:
            data = await self.http.arequest(
                "POST", APPROVALS, headers={"Idempotency-Key": action.request_id},
                json=action.model_dump(mode="json"),
            )
        return self._approval_handle(action, data)

    def _approval_handle(self, action: ActionRequest, data: Any) -> ApprovalRequest:
        if not isinstance(data, dict):
            raise BackendUnavailableError("Governance backend returned an invalid approval request")
        payload = data.get("approval", data)
        try:
            values = {
                **action.model_dump(),
                **payload,
                "approval_id": payload.get("approval_id", payload.get("id")),
                "request_id": payload.get("request_id", action.request_id),
                "project_id": payload.get("project_id", action.project_id),
                "agent_id": payload.get("agent_id", action.agent_id),
                "submitted_at": payload.get("submitted_at", action.requested_at),
                "expires_at": payload.get("expires_at", action.expires_at),
            }
            request = ApprovalRequest.model_validate(values)
        except (ValidationError, TypeError):
            raise BackendUnavailableError("Governance backend returned an invalid approval request") from None
        if request.project_id != action.project_id or request.agent_id != action.agent_id:
            raise GovernanceError("Governance backend returned an approval for a different identity")
        if request.request_id != action.request_id:
            raise GovernanceError("Governance backend returned an approval for a different request")
        if request.action_name != action.action_name or request.action_parameters != action.action_parameters:
            raise GovernanceError("Governance backend returned approval details for a different action")
        return self.handler.attach(request)

