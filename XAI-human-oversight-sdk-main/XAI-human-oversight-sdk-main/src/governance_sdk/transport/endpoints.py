"""Proposed backend endpoint paths."""

EVALUATE = "/api/v1/oversight/evaluate"
AGENTS = "/api/v1/agents"
APPROVALS = "/api/v1/approvals"
AUTHORIZATION_VERIFY = "/api/v1/authorizations/verify"


def approval_status(approval_id: str) -> str:
    """Return the status resource path for an approval ID."""
    return f"{APPROVALS}/{approval_id}"


def approval_wait(approval_id: str) -> str:
    """Return the wait resource path for an approval ID."""
    return f"{APPROVALS}/{approval_id}/wait"


def approval_cancel(approval_id: str) -> str:
    """Return the cancellation resource path for an approval ID."""
    return f"{APPROVALS}/{approval_id}/cancel"

