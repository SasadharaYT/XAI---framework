"""Safe, user-facing exceptions raised by the governance SDK."""


class GovernanceError(Exception):
    """Base exception for governance workflow failures."""


class AuthenticationError(GovernanceError):
    """The backend rejected the configured API key."""


class BackendUnavailableError(GovernanceError):
    """The backend could not be reached or returned an unexpected error."""


class ApprovalRejectedError(GovernanceError):
    """A reviewer rejected an approval request."""

    def __init__(self, message: str = "Approval was rejected", *, comment: str | None = None):
        super().__init__(message)
        self.comment = comment


class ApprovalTimeoutError(GovernanceError):
    """The approval did not reach a terminal decision before its deadline."""


class ApprovalExpiredError(GovernanceError):
    """The approval or authorization receipt expired."""


class AuthorizationVerificationError(GovernanceError):
    """The backend did not verify and consume an authorization receipt."""


class InvalidActionError(GovernanceError):
    """The action or its context is invalid or contains credential-like fields."""

