"""Execution gate shared by protected sync and async tool calls."""

from __future__ import annotations

import threading
from collections.abc import Callable
from inspect import isawaitable
from typing import Any

from ..exceptions import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    AuthorizationVerificationError,
    BackendUnavailableError,
)
from ..models import ActionRequest, ApprovalDecision, ApprovalStatus
from .manager import ApprovalManager


class ExecutionInterceptor:
    """Enforce backend decisions and prevent in-process duplicate executions."""

    def __init__(self, manager: ApprovalManager) -> None:
        self._manager = manager
        self._sync_lock = threading.Lock()
        self._claimed: set[str] = set()

    def run(
        self, request: ActionRequest, function: Callable[[], Any], timeout: float | None,
        on_decision: Callable[[ApprovalDecision], Any] | None = None,
    ) -> Any:
        with self._sync_lock:
            if request.request_id in self._claimed:
                raise AuthorizationVerificationError("This authorization request was already executed")
            self._claimed.add(request.request_id)
        try:
            evaluation = self._manager.evaluate(request)
            if evaluation.outcome == "deny":
                raise ApprovalRejectedError(evaluation.reason or "Backend denied this action")
            if evaluation.outcome == "allow":
                if evaluation.approval_id is not None:
                    raise AuthorizationVerificationError("Automatic authorization included an approval ID")
                self._manager.handler.verify_action(request, evaluation.authorization_receipt or "")
            elif evaluation.outcome == "approval_required":
                approval = self._manager.create_for_action(request, evaluation.approval_id)
                decision = approval.wait(timeout)
                self._require_approved(decision)
                if on_decision is not None:
                    on_decision(decision)
            else:
                raise AuthorizationVerificationError("Backend returned an unknown execution outcome")
            return function()
        except BaseException:
            # Retain the claim after evaluation: repeating a possibly consumed authorization is unsafe.
            raise

    async def arun(
        self, request: ActionRequest, function: Callable[[], Any], timeout: float | None,
        on_decision: Callable[[ApprovalDecision], Any] | None = None,
    ) -> Any:
        # One lock covers sync and async callers sharing this interceptor.
        with self._sync_lock:
            if request.request_id in self._claimed:
                raise AuthorizationVerificationError("This authorization request was already executed")
            self._claimed.add(request.request_id)
        evaluation = await self._manager.aevaluate(request)
        if evaluation.outcome == "deny":
            raise ApprovalRejectedError(evaluation.reason or "Backend denied this action")
        if evaluation.outcome == "allow":
            if evaluation.approval_id is not None:
                raise AuthorizationVerificationError("Automatic authorization included an approval ID")
            await self._manager.handler.averify_action(request, evaluation.authorization_receipt or "")
        elif evaluation.outcome == "approval_required":
            approval = await self._manager.acreate_for_action(request, evaluation.approval_id)
            decision = await approval.await_decision(timeout)
            self._require_approved(decision)
            if on_decision is not None:
                callback_result = on_decision(decision)
                if isawaitable(callback_result):
                    await callback_result
        else:
            raise AuthorizationVerificationError("Backend returned an unknown execution outcome")
        return await function()

    @staticmethod
    def _require_approved(decision: ApprovalDecision) -> None:
        if decision.status == ApprovalStatus.REJECTED:
            raise ApprovalRejectedError("Approval was rejected", comment=decision.reviewer_comment)
        if decision.status == ApprovalStatus.EXPIRED:
            raise ApprovalExpiredError("Approval expired before authorization")
        if decision.status == ApprovalStatus.CANCELLED:
            raise ApprovalRejectedError("Approval was cancelled")
        if decision.status != ApprovalStatus.APPROVED:
            raise BackendUnavailableError("Approval did not reach an executable decision")

