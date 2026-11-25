"""Approval request models and lifecycle states."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


class ApprovalStatus(StrEnum):
    """Known lifecycle states returned by the backend."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ApprovalRequest(BaseModel):
    """A review request handle with sync and async decision wait methods."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    request_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    project_id: str
    agent_id: str
    agent_name: str | None = None
    action_name: str
    action_parameters: dict[str, object] = Field(default_factory=dict)
    reason: str | None = None
    requested_at: datetime
    submitted_at: datetime
    expires_at: datetime
    metadata: dict[str, object] = Field(default_factory=dict)

    _waiter: Any = PrivateAttr(default=None)

    @field_validator("requested_at", "submitted_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps from the backend."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamps must include a timezone")
        return value

    def wait(self, timeout: float | None = None) -> "ApprovalDecision":
        """Wait synchronously for a final, verified decision."""
        if self._waiter is None:
            raise RuntimeError("This approval request is not attached to a Governance client")
        return self._waiter.wait(self, timeout)

    async def await_decision(self, timeout: float | None = None) -> "ApprovalDecision":
        """Wait asynchronously without blocking the event loop."""
        if self._waiter is None:
            raise RuntimeError("This approval request is not attached to a Governance client")
        return await self._waiter.await_decision(self, timeout)

    def cancel(self) -> None:
        """Cancel this pending request through the backend."""
        if self._waiter is None:
            raise RuntimeError("This approval request is not attached to a Governance client")
        self._waiter.cancel(self)

    async def acancel(self) -> None:
        """Cancel this pending request asynchronously."""
        if self._waiter is None:
            raise RuntimeError("This approval request is not attached to a Governance client")
        await self._waiter.acancel(self)

