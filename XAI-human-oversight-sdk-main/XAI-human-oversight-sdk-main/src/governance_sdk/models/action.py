"""Action request and backend evaluation models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionRequest(BaseModel):
    """A specific agent's request to run a named action."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    project_id: str
    agent_id: str
    agent_name: str | None = None
    action_name: str = Field(min_length=1)
    action_parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    requested_at: datetime
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    """Authoritative result of evaluating a protected action."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["allow", "approval_required", "deny"]
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    reason: str | None = None
    reason_code: str | None = None
    matched_rule: dict[str, Any] | None = None
    policy_version: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    approval_id: str | None = None
    authorization_receipt: str | None = None

