"""Project-key authenticated Human-in-the-Loop API for the Python SDK."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import main as core
from .policy import evaluate as evaluate_policy

router = APIRouter()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise HTTPException(422, "Request values must be finite JSON data") from None


def _fingerprint(payload: core.SDKActionIn) -> str:
    action = payload.model_dump(exclude={"requested_at", "expires_at"})
    return hashlib.sha256(_canonical(action).encode("utf-8")).hexdigest()


def _parameters_hash(parameters: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(parameters).encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stored_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "Timestamps must include a timezone")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


_CREDENTIAL_MARKERS = (
    "apikey", "token", "password", "passwd", "secret", "credential", "authorization", "privatekey"
)


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(char.lower() for char in str(key) if char.isalnum())
            if any(marker in normalized for marker in _CREDENTIAL_MARKERS):
                raise HTTPException(422, "Credential-like fields are not allowed in action context")
            _reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_credentials(child)


def project_key_for_token(db: core.Session, token: str) -> core.ProjectAPIKey | None:
    """Return an active project key record without exposing its hash."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = db.scalar(select(core.ProjectAPIKey).where(core.ProjectAPIKey.key_hash == digest))
    return row if row is not None and row.revoked_at is None else None


def current_project(
    credentials: HTTPAuthorizationCredentials | None = Depends(core.bearer),
) -> str:
    if credentials is None:
        raise HTTPException(401, "Project API key required")
    with core.SessionLocal() as db:
        key = project_key_for_token(db, credentials.credentials)
        if key is None:
            raise HTTPException(401, "Invalid or revoked project API key")
        return key.project_id


def _require_membership(
    db: core.Session, user: core.User, project_id: str, *, manage_keys: bool = False
) -> core.TeamMember:
    member = db.scalar(select(core.TeamMember).where(core.TeamMember.email == user.email))
    project_ids = set(member.project_ids.split(",")) if member else set()
    if member is None or member.status != "Active" or project_id not in project_ids:
        raise HTTPException(403, "You do not have access to this project")
    if manage_keys and member.role.strip().lower() not in {
        "owner", "admin", "project admin", "team leader"
    }:
        raise HTTPException(403, "Project administrator role is required")
    return member


def _require_agent(db: core.Session, project_id: str, agent_id: str) -> core.Agent:
    if db.get(core.Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    agent = db.get(core.Agent, agent_id)
    if agent is None or agent.project_id != project_id:
        raise HTTPException(403, "Agent does not belong to the authenticated project")
    return agent


def _validate_action(
    db: core.Session, payload: core.SDKActionIn, project_id: str
) -> tuple[core.Agent, datetime, datetime]:
    if payload.project_id != project_id:
        raise HTTPException(403, "Request project does not match the API key project")
    agent = _require_agent(db, project_id, payload.agent_id)
    if payload.agent_name and payload.agent_name != agent.name:
        raise HTTPException(422, "Agent name does not match the registered agent")
    _reject_credentials(payload.action_parameters)
    _reject_credentials(payload.metadata)
    requested_at = _stored_time(payload.requested_at)
    expires_at = _stored_time(payload.expires_at)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if expires_at <= requested_at or expires_at <= now:
        raise HTTPException(422, "Action expiry must be after its request timestamp and current time")
    maximum = int(core.os.getenv("GOVERNANCE_MAX_APPROVAL_SECONDS", "86400"))
    if (expires_at - requested_at).total_seconds() > maximum:
        raise HTTPException(422, "Action expiry exceeds the configured maximum")
    return agent, requested_at, expires_at


def _receipt_secret() -> str:
    secret = core.os.getenv("GOVERNANCE_RECEIPT_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "Authorization signing is not configured")
    return secret


def _receipt_for(row: core.GovernanceApproval) -> str:
    claims = {
        "sub": row.id,
        "request_id": row.request_id,
        "project_id": row.project_id,
        "agent_id": row.agent_id,
        "action_name": row.action_name,
        "action_hash": _parameters_hash(json.loads(row.action_parameters_json)),
        "jti": hashlib.sha256(f"{row.id}:{row.request_id}".encode("utf-8")).hexdigest(),
        "exp": int(datetime.fromisoformat(_iso(row.expires_at).replace("Z", "+00:00")).timestamp()),
    }
    return jwt.encode(claims, _receipt_secret(), algorithm="HS256")


def _receipt_for_evaluation(response: dict[str, Any]) -> str:
    claims = {
        "sub": response["request_id"],
        "kind": "evaluation",
        "request_id": response["request_id"],
        "project_id": response["project_id"],
        "agent_id": response["agent_id"],
        "action_name": response["action_name"],
        "action_hash": response["action_hash"],
        "policy_version": response["policy_version"],
        "rule_id": (response.get("matched_rule") or {}).get("rule_id"),
        "jti": hashlib.sha256(f"evaluation:{response['request_id']}".encode("utf-8")).hexdigest(),
        "exp": int(datetime.fromisoformat(response["expires_at"].replace("Z", "+00:00")).timestamp()),
    }
    return jwt.encode(claims, _receipt_secret(), algorithm="HS256")


def _approval_dict(row: core.GovernanceApproval) -> dict[str, Any]:
    result = {
        "approval_id": row.id,
        "request_id": row.request_id,
        "project_id": row.project_id,
        "agent_id": row.agent_id,
        "agent_name": row.agent_name,
        "action_name": row.action_name,
        "action_parameters": json.loads(row.action_parameters_json),
        "reason": row.reason,
        "requested_at": _iso(row.requested_at),
        "submitted_at": _iso(row.submitted_at),
        "expires_at": _iso(row.expires_at),
        "metadata": json.loads(row.metadata_json or "{}"),
        "status": row.status,
    }
    if row.status != "pending":
        result["decision"] = {
            "status": row.status,
            "reviewer_id": row.reviewer_id,
            "reviewer_comment": row.reviewer_comment,
            "reviewed_at": _iso(row.reviewed_at),
            "reason": None,
            "authorization_receipt": _receipt_for(row) if row.status == "approved" else None,
        }
    return result


def _sync_dashboard_row(
    db: core.Session, row: core.GovernanceApproval, evaluation: dict[str, Any] | None = None
) -> core.Approval:
    existing = db.get(core.Approval, row.id)
    if existing is None:
        existing = core.Approval(
            id=row.id,
            agent=row.agent_name or row.agent_id,
            action=row.action_name,
            tool=row.action_name,
            parameters=row.action_parameters_json,
            risk=str((evaluation or {}).get("risk_level", "medium")).capitalize(),
            status="Pending",
            request=row.reason or row.action_name,
            reason=row.reason or "",
            explanation=_canonical(json.loads(row.metadata_json or "{}")),
            reviewer="Unassigned",
            time=_iso(row.submitted_at) or "",
            execution="Interrupted",
        )
        db.add(existing)
    return existing


def _expire_if_needed(db: core.Session, row: core.GovernanceApproval) -> None:
    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if row.status == "pending" and expiry <= datetime.now(timezone.utc):
        row.status = "expired"
        dashboard = db.get(core.Approval, row.id)
        if dashboard is not None:
            dashboard.status = "Expired"
        db.commit()


@router.post("/api/v1/projects/{project_id}/api-keys", status_code=201)
def create_project_api_key(
    project_id: str,
    payload: core.ProjectAPIKeyIn,
    user: core.User = Depends(core.current_user),
) -> dict[str, Any]:
    """Create a project-scoped SDK key; reveal its secret only in this response."""
    with core.SessionLocal() as db:
        if db.get(core.Project, project_id) is None:
            raise HTTPException(404, "Project not found")
        _require_membership(db, user, project_id, manage_keys=True)
        raw_key = "gsk_" + secrets.token_urlsafe(32)
        record = core.ProjectAPIKey(
            id=f"key-{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            key_hash=hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
            key_prefix=raw_key[:12],
            name=payload.name.strip(),
            created_by=user.id,
        )
        db.add(record)
        db.commit()
        return {
            "id": record.id,
            "project_id": project_id,
            "name": record.name,
            "api_key": raw_key,
            "created_at": _iso(record.created_at),
        }


@router.get("/api/v1/projects/{project_id}/api-keys")
def list_project_api_keys(
    project_id: str, user: core.User = Depends(core.current_user)
) -> list[dict[str, Any]]:
    with core.SessionLocal() as db:
        if db.get(core.Project, project_id) is None:
            raise HTTPException(404, "Project not found")
        _require_membership(db, user, project_id, manage_keys=True)
        rows = db.scalars(
            select(core.ProjectAPIKey).where(core.ProjectAPIKey.project_id == project_id)
        ).all()
        return [
            {
                "id": row.id,
                "project_id": row.project_id,
                "name": row.name,
                "key_prefix": row.key_prefix,
                "created_at": _iso(row.created_at),
                "revoked_at": _iso(row.revoked_at),
            }
            for row in rows
        ]


@router.delete("/api/v1/projects/{project_id}/api-keys/{key_id}", status_code=204)
def revoke_project_api_key(
    project_id: str, key_id: str, user: core.User = Depends(core.current_user)
) -> None:
    with core.SessionLocal() as db:
        _require_membership(db, user, project_id, manage_keys=True)
        row = db.get(core.ProjectAPIKey, key_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "API key not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            db.commit()


@router.post("/api/v1/oversight/evaluate")
def evaluate_action(
    payload: core.SDKActionIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    project_id: str = Depends(current_project),
) -> dict[str, Any]:
    if idempotency_key != payload.request_id:
        raise HTTPException(400, "Idempotency-Key must match request_id")
    with core.SessionLocal() as db:
        agent, requested_at, expires_at = _validate_action(db, payload, project_id)
        project = db.get(core.Project, project_id)
        fingerprint = _fingerprint(payload)
        approval = db.scalar(
            select(core.GovernanceApproval).where(core.GovernanceApproval.request_id == payload.request_id)
        )
        if approval is not None:
            if approval.project_id != project_id or approval.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            previous = db.get(core.GovernanceEvaluation, payload.request_id)
            response = json.loads(previous.response_json) if previous is not None else {
                "outcome": "approval_required", "risk_level": "medium",
                "reason": "Human review is required", "reason_code": "EXISTING_APPROVAL",
                "matched_rule": None, "policy_version": "unknown", "evidence": [],
            }
            response["approval_id"] = approval.id
            return response
        previous = db.get(core.GovernanceEvaluation, payload.request_id)
        if previous is not None:
            if previous.project_id != project_id or previous.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            return json.loads(previous.response_json)
        result = evaluate_policy(
            project=project,
            agent=agent,
            action_name=payload.action_name,
            action_parameters=payload.action_parameters,
            metadata=payload.metadata,
        )
        response = {
            **result.as_dict(),
            "approval_id": None,
            "authorization_receipt": None,
            "request_id": payload.request_id,
            "project_id": project_id,
            "agent_id": agent.id,
            "action_name": payload.action_name,
            "action_hash": _parameters_hash(payload.action_parameters),
            "expires_at": _iso(expires_at),
        }
        if response["outcome"] == "allow":
            response["authorization_receipt"] = _receipt_for_evaluation(response)
        db.add(core.GovernanceEvaluation(
            request_id=payload.request_id,
            project_id=project_id,
            body_hash=fingerprint,
            response_json=_canonical(response),
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            previous = db.get(core.GovernanceEvaluation, payload.request_id)
            if previous is None or previous.project_id != project_id or previous.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data") from None
            return json.loads(previous.response_json)
        return response


@router.post("/api/v1/approvals", status_code=201)
def create_approval(
    payload: core.SDKActionIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    project_id: str = Depends(current_project),
) -> dict[str, Any]:
    if idempotency_key != payload.request_id:
        raise HTTPException(400, "Idempotency-Key must match request_id")
    with core.SessionLocal() as db:
        agent, requested_at, expires_at = _validate_action(db, payload, project_id)
        fingerprint = _fingerprint(payload)
        existing = db.scalar(
            select(core.GovernanceApproval).where(core.GovernanceApproval.request_id == payload.request_id)
        )
        if existing is not None:
            if existing.project_id != project_id or existing.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            return _approval_dict(existing)
        evaluation = db.get(core.GovernanceEvaluation, payload.request_id)
        if evaluation is not None and (evaluation.project_id != project_id or evaluation.body_hash != fingerprint):
            raise HTTPException(409, "Idempotency key was reused with different action data")
        evaluation_response = json.loads(evaluation.response_json) if evaluation is not None else None
        row = core.GovernanceApproval(
            id=f"apr-{uuid.uuid4().hex[:20]}",
            request_id=payload.request_id,
            project_id=project_id,
            agent_id=agent.id,
            agent_name=agent.name,
            action_name=payload.action_name,
            action_parameters_json=_canonical(payload.action_parameters),
            reason=payload.reason,
            metadata_json=_canonical(payload.metadata),
            requested_at=requested_at,
            submitted_at=datetime.now(timezone.utc).replace(tzinfo=None),
            expires_at=expires_at,
            status="pending",
            body_hash=fingerprint,
        )
        db.add(row)
        db.flush()
        _sync_dashboard_row(db, row, evaluation_response)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(core.GovernanceApproval).where(core.GovernanceApproval.request_id == payload.request_id)
            )
            if existing is None or existing.project_id != project_id or existing.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data") from None
            return _approval_dict(existing)
        return _approval_dict(row)


def _read_status(db: core.Session, approval_id: str, project_id: str) -> core.GovernanceApproval:
    row = db.get(core.GovernanceApproval, approval_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(404, "Approval not found")
    _expire_if_needed(db, row)
    return row


@router.get("/api/v1/approvals/{approval_id}")
def get_approval_status(
    approval_id: str, project_id: str = Depends(current_project)
) -> dict[str, Any]:
    with core.SessionLocal() as db:
        return _approval_dict(_read_status(db, approval_id, project_id))


@router.get("/api/v1/approvals/{approval_id}/wait")
def wait_for_approval(
    approval_id: str,
    timeout_seconds: int = Query(default=20, ge=0, le=30),
    project_id: str = Depends(current_project),
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        with core.SessionLocal() as db:
            row = _read_status(db, approval_id, project_id)
            result = _approval_dict(row)
            if row.status != "pending" or time.monotonic() >= deadline:
                return result
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


@router.post("/api/v1/approvals/{approval_id}/cancel")
def cancel_approval(
    approval_id: str,
    payload: dict[str, str],
    project_id: str = Depends(current_project),
) -> dict[str, Any]:
    request_id = payload.get("request_id")
    if not request_id:
        raise HTTPException(422, "request_id is required")
    with core.SessionLocal() as db:
        row = db.get(core.GovernanceApproval, approval_id)
        if row is None or row.project_id != project_id or row.request_id != request_id:
            raise HTTPException(404, "Approval not found")
        _expire_if_needed(db, row)
        if row.status == "cancelled":
            return _approval_dict(row)
        if row.status != "pending":
            raise HTTPException(409, "Only pending approvals can be cancelled")
        row.status = "cancelled"
        row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        dashboard = db.get(core.Approval, row.id)
        if dashboard is not None:
            dashboard.status = "Cancelled"
        db.commit()
        return _approval_dict(row)


@router.post("/api/v1/authorizations/verify")
def verify_authorization(
    payload: core.AuthorizationVerifyIn,
    project_id: str = Depends(current_project),
) -> dict[str, Any]:
    if payload.project_id != project_id:
        raise HTTPException(403, "Request project does not match the API key project")
    _reject_credentials(payload.action_parameters)
    with core.SessionLocal() as db:
        _require_agent(db, project_id, payload.agent_id)
        signing_secret = _receipt_secret()
        try:
            claims = jwt.decode(
                payload.authorization_receipt,
                signing_secret,
                algorithms=["HS256"],
                options={"require": ["exp", "jti", "request_id", "project_id", "agent_id", "action_name", "action_hash", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(410, "Authorization receipt has expired") from None
        except jwt.InvalidTokenError:
            raise HTTPException(403, "Authorization receipt is invalid") from None
        parameters_hash = _parameters_hash(payload.action_parameters)
        if claims.get("kind") == "evaluation":
            if payload.approval_id is not None:
                raise HTTPException(403, "Automatic authorization cannot reference an approval")
            evaluation = db.get(core.GovernanceEvaluation, payload.request_id)
            if evaluation is None:
                raise HTTPException(403, "Automatic authorization evaluation was not found")
            response = json.loads(evaluation.response_json)
            if response.get("outcome") != "allow" or response.get("authorization_receipt") != payload.authorization_receipt:
                raise HTTPException(403, "Automatic authorization is not valid")
            if response.get("action_hash") != parameters_hash:
                raise HTTPException(403, "Authorization receipt does not match this action")
        elif (
            claims.get("request_id") != payload.request_id
            or claims.get("project_id") != project_id
            or claims.get("agent_id") != payload.agent_id
            or claims.get("action_name") != payload.action_name
            or claims.get("action_hash") != parameters_hash
            or claims.get("sub") != payload.approval_id
        ):
            raise HTTPException(403, "Authorization receipt does not match this action")
        if claims.get("kind") == "evaluation":
            if claims.get("project_id") != project_id or claims.get("agent_id") != payload.agent_id or claims.get("action_name") != payload.action_name:
                raise HTTPException(403, "Authorization receipt does not match this action")
        elif payload.approval_id is None:
            raise HTTPException(403, "Authorization receipt is not attached to an approval")
        else:
            row = db.get(core.GovernanceApproval, payload.approval_id)
            if (
                row is None
                or row.project_id != project_id
                or row.request_id != payload.request_id
                or row.agent_id != payload.agent_id
                or row.action_name != payload.action_name
                or row.action_parameters_json != _canonical(payload.action_parameters)
                or row.status != "approved"
            ):
                raise HTTPException(403, "Authorization receipt is not valid for this approval")
        use = core.AuthorizationUse(
            jti=claims["jti"],
            request_id=payload.request_id,
            project_id=project_id,
            consumed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(use)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Authorization receipt has already been consumed") from None
        return {
            "valid": True,
            "consumed": True,
            "expires_at": datetime.fromtimestamp(claims["exp"], timezone.utc).isoformat().replace("+00:00", "Z"),
        }


def record_dashboard_decision(
    db: core.Session, approval_id: str, decision: str, comment: str, user: core.User
) -> bool:
    """Mirror a dashboard reviewer decision into the SDK approval record."""
    row = db.get(core.GovernanceApproval, approval_id)
    if row is None:
        return False
    _require_membership(db, user, row.project_id)
    _expire_if_needed(db, row)
    if row.status != "pending":
        raise HTTPException(409, "Only pending requests can be reviewed")
    if decision == "Approved":
        _receipt_secret()
        row.status = "approved"
    else:
        row.status = "rejected"
    row.reviewer_id = user.id
    row.reviewer_comment = comment or None
    row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    dashboard = db.get(core.Approval, approval_id)
    if dashboard is not None:
        dashboard.status = decision
        dashboard.reviewer = user.name
        if comment:
            dashboard.reason = f"{dashboard.reason} Reviewer comment: {comment}".strip()
    db.flush()
    return True

