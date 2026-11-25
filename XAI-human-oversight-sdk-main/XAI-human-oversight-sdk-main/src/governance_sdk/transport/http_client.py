"""HTTPX transport; response bodies and secrets are never logged."""

import logging
from typing import Any

import httpx

from ..config import GovernanceConfig
from ..exceptions import (
    ApprovalExpiredError,
    AuthenticationError,
    BackendUnavailableError,
    GovernanceError,
)

logger = logging.getLogger(__name__)


class GovernanceHTTPClient:
    """Own sync and async HTTPX clients for the proposed backend contract."""

    def __init__(self, config: GovernanceConfig) -> None:
        self._config = config
        base_url = str(config.base_url).rstrip("/")
        headers = {"Authorization": f"Bearer {config.api_key.get_secret_value()}"}
        self._sync = httpx.Client(base_url=base_url, headers=headers, timeout=config.timeout)
        self._async: httpx.AsyncClient | None = None
        self._base_url = base_url
        self._headers = headers

    def _async_client(self) -> httpx.AsyncClient:
        """Create the async pool lazily so sync-only clients have no async resource."""
        if self._async is None:
            self._async = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._config.timeout,
            )
        return self._async

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send a sync request and return validated JSON data."""
        try:
            response = self._sync.request(method, path, **kwargs)
        except httpx.HTTPError:
            logger.warning("Governance backend request failed")
            raise BackendUnavailableError("Governance backend is unavailable") from None
        self._raise_for_status(response)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise BackendUnavailableError("Governance backend returned an invalid response") from None

    async def arequest(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send an async request and return validated JSON data."""
        try:
            response = await self._async_client().request(method, path, **kwargs)
        except httpx.HTTPError:
            logger.warning("Governance backend request failed")
            raise BackendUnavailableError("Governance backend is unavailable") from None
        self._raise_for_status(response)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise BackendUnavailableError("Governance backend returned an invalid response") from None

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        logger.warning("Governance backend returned HTTP status %d", response.status_code)
        if response.status_code in (401, 403):
            raise AuthenticationError("Governance backend rejected the configured credentials")
        if response.status_code == 404:
            raise GovernanceError("Governance resource was not found")
        if response.status_code == 410:
            raise ApprovalExpiredError("Governance authorization has expired")
        if response.status_code == 409:
            raise GovernanceError("Governance request conflicts with its current state")
        if response.status_code == 422:
            raise GovernanceError("Governance backend rejected the request data")
        raise BackendUnavailableError("Governance backend returned an unsuccessful response")

    def close(self) -> None:
        """Close the synchronous HTTP client."""
        self._sync.close()

    async def aclose(self) -> None:
        """Close both HTTP clients, including the asynchronous client."""
        self._sync.close()
        if self._async is not None:
            await self._async.aclose()

