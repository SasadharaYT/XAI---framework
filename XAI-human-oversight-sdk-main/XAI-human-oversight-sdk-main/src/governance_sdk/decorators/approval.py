"""Sync and async wrappers that gate function execution."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar, cast

from ..models import ApprovalDecision
from ..oversight.interceptor import ExecutionInterceptor
from ..oversight.manager import ApprovalManager

F = TypeVar("F", bound=Callable[..., Any])


def create_protector(manager: ApprovalManager, interceptor: ExecutionInterceptor) -> Callable[..., Any]:
    """Build a ``protect`` decorator bound to one Governance client."""

    def protect(
        function: F | None = None,
        *,
        agent_id: str | None = None,
        agent_name: str | None = None,
        action_name: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        on_decision: Callable[[ApprovalDecision], Any] | None = None,
        idempotency_key: Callable[..., str] | None = None,
    ) -> F | Callable[[F], F]:
        def decorate(target: F) -> F:
            action = action_name or target.__name__
            signature = inspect.signature(target)

            def parameters(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
                try:
                    bound = signature.bind(*args, **kwargs)
                    bound.apply_defaults()
                    result = dict(bound.arguments)
                    if "self" in result:
                        result.pop("self")
                    if "cls" in result:
                        result.pop("cls")
                    return result
                except TypeError:
                    raise TypeError("Protected action arguments do not match its function signature") from None

            if inspect.iscoroutinefunction(target):
                @functools.wraps(target)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    resolved_agent = agent_id or manager.config.agent_id
                    request = manager.build_action(
                        agent_id=resolved_agent or "", action_name=action,
                        action_parameters=parameters(args, kwargs), reason=reason, metadata=metadata,
                        agent_name=agent_name,
                        request_id=idempotency_key(*args, **kwargs) if idempotency_key else None,
                    )
                    return await interceptor.arun(
                        request, lambda: target(*args, **kwargs), timeout, on_decision
                    )

                return cast(F, async_wrapper)

            @functools.wraps(target)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                resolved_agent = agent_id or manager.config.agent_id
                request = manager.build_action(
                    agent_id=resolved_agent or "", action_name=action,
                    action_parameters=parameters(args, kwargs), reason=reason, metadata=metadata,
                    agent_name=agent_name,
                    request_id=idempotency_key(*args, **kwargs) if idempotency_key else None,
                )
                return interceptor.run(
                    request, lambda: target(*args, **kwargs), timeout, on_decision
                )

            return cast(F, sync_wrapper)

        if function is not None:
            return decorate(function)
        return decorate

    return protect

