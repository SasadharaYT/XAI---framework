"""Safe action parameter validation and JSON serialization."""

import json
from typing import Any

from ..exceptions import InvalidActionError

_CREDENTIAL_MARKERS = (
    "apikey", "accesstoken", "refreshtoken", "bearertoken", "token", "password",
    "passwd", "secret", "clientsecret", "privatekey", "authorization", "credential",
)


def _is_credential_key(key: str) -> bool:
    """Catch common snake, kebab, and camel-case credential field names."""
    normalized = "".join(character.lower() for character in key if character.isalnum())
    return any(marker in normalized for marker in _CREDENTIAL_MARKERS)


def validate_safe_payload(value: Any, path: str = "parameters") -> None:
    """Reject credential-like field names recursively before sending action data."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _is_credential_key(key):
                raise InvalidActionError(
                    f"Credential-like field at {path}.{key} cannot be sent; pass a secret reference"
                )
            validate_safe_payload(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            validate_safe_payload(child, f"{path}[{index}]")


def json_safe(value: Any) -> Any:
    """Round-trip values through JSON to reject unsupported or non-JSON values."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise InvalidActionError("Action parameters and metadata must contain JSON values") from None

