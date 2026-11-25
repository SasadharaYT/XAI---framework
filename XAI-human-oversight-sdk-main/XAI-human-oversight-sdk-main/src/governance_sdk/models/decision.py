"""Final human decision and authorization receipt model."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from .approval import ApprovalStatus


class ApprovalDecision(BaseModel):
    """Verified final decision, including reviewer feedback where available."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    request_id: str
    status: ApprovalStatus
    approved: bool
    reviewer_id: str | None = None
    reviewer_comment: str | None = None
    reviewed_at: datetime | None = None
    reason: str | None = None
    authorization_receipt: str | None = None

    @field_validator("reviewed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware decision timestamps."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("reviewed_at must include a timezone")
        return value

