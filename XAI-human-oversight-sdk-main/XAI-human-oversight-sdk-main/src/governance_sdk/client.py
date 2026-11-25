"""Public Governance client."""

from __future__ import annotations

from typing import Any

from .config import GovernanceConfig
from .decorators import create_protector
from .models import ApprovalRequest
from .oversight.approval_handler import ApprovalHandler
from .oversight.interceptor import ExecutionInterceptor
from .oversight.manager import ApprovalManager
from .oversight.wait_strategy import PollingWaitStrategy
from .transport import GovernanceHTTPClient


class Governance:
    """Client for approval workflows in a single governance project."""

    def __init__(
        self,
        *,
        api_key: str,
        project_id: str,
        base_url: str,
        agent_id: str | None = None,
        timeout: float = 10.0,
        approval_timeout: float = 300.0,
        poll_interval: float = 2.0,
        max_poll_interval: float = 10.0,
    ) -> None:
        """Initialize a client; agent identity is independent from the API key."""
        self.config = GovernanceConfig(
            api_key=api_key, project_id=project_id, base_url=base_url, agent_id=agent_id,
            timeout=timeout, approval_timeout=approval_timeout, poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
        )
        self._http = GovernanceHTTPClient(self.config)
        strategy = PollingWaitStrategy(poll_interval, max_poll_interval)
        self._handler = ApprovalHandler(self._http, strategy)
        self._manager = ApprovalManager(self.config, self._http, self._handler)
        self._interceptor = ExecutionInterceptor(self._manager)
        self.protect = create_protector(self._manager, self._interceptor)

    def register_agent(
        self, name: str, description: str = "", metadata: dict[str, Any] | None = None
    ) -> str:
        """Register a project agent and return its backend-issued agent ID."""
        return self._manager.register_agent(name, description, metadata)

    async def aregister_agent(
        self, name: str, description: str = "", metadata: dict[str, Any] | None = None
    ) -> str:
        """Register a project agent asynchronously."""
        return await self._manager.aregister_agent(name, description, metadata)

    def request_approval(
        self,
        *,
        agent_id: str | None = None,
        action: str,
        parameters: dict[str, Any],
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        agent_name: str | None = None,
        timeout: float | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        """Submit a manual approval; this method never invokes the action."""
        return self._manager.request_approval(
            agent_id=agent_id or self.config.agent_id or "", action=action, parameters=parameters,
            reason=reason, metadata=metadata, agent_name=agent_name, timeout=timeout,
            request_id=request_id,
        )

    async def arequest_approval(
        self,
        *,
        agent_id: str | None = None,
        action: str,
        parameters: dict[str, Any],
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        agent_name: str | None = None,
        timeout: float | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        """Submit a manual approval asynchronously; no action is executed."""
        return await self._manager.arequest_approval(
            agent_id=agent_id or self.config.agent_id or "", action=action,
            parameters=parameters, reason=reason, metadata=metadata, agent_name=agent_name,
            timeout=timeout, request_id=request_id,
        )

    def close(self) -> None:
        """Close the synchronous HTTP connection pool."""
        self._http.close()

    async def aclose(self) -> None:
        """Close the synchronous and asynchronous HTTP connection pools."""
        await self._http.aclose()

    def __enter__(self) -> "Governance":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> "Governance":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

