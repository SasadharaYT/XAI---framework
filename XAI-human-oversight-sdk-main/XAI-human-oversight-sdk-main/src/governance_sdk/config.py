"""Validated SDK configuration."""

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class GovernanceConfig(BaseModel):
    """Connection and default identity settings for a governance project."""

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr
    project_id: str = Field(min_length=1)
    base_url: AnyHttpUrl
    agent_id: str | None = None
    timeout: float = Field(default=10.0, gt=0)
    approval_timeout: float = Field(default=300.0, gt=0)
    poll_interval: float = Field(default=2.0, gt=0)
    max_poll_interval: float = Field(default=10.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def strip_base_url_path(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require a plain server base URL so endpoints join predictably."""
        if value.path not in ("", "/") or value.query or value.fragment:
            raise ValueError("base_url must contain only the scheme, host, and optional port")
        return value

    @field_validator("api_key")
    @classmethod
    def require_api_key(cls, value: SecretStr) -> SecretStr:
        """Reject empty credentials without ever rendering their value."""
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be empty")
        return value

    @field_validator("project_id", "agent_id")
    @classmethod
    def require_nonblank_identity(cls, value: str | None) -> str | None:
        """Reject whitespace-only project or default agent identifiers."""
        if value is not None and not value.strip():
            raise ValueError("identifiers must not be blank")
        return value

    @model_validator(mode="after")
    def validate_poll_intervals(self) -> "GovernanceConfig":
        """Ensure the initial polling delay does not exceed its configured cap."""
        if self.max_poll_interval < self.poll_interval:
            raise ValueError("max_poll_interval must be greater than or equal to poll_interval")
        return self

