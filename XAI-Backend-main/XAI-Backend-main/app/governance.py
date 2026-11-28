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
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from . import main as core
from .policy import Policy, ProjectPolicyIn, evaluate

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


def _automatic_receipt(row: core.AutomaticAuthorization) -> str:
    return jwt.encode({
        "sub": row.id,
        "kind": "automatic",
        "request_id": row.request_id,
        "project_id": row.project_id,
        "agent_id": row.agent_id,
        "action_name": row.action_name,
        "action_hash": _parameters_hash(json.loads(row.action_parameters_json)),
        "policy_version": row.policy_version,
        "jti": row.jti,
        "exp": int(row.expires_at.replace(tzinfo=timezone.utc).timestamp()),
    }, _receipt_secret(), algorithm="HS256")


def _sync_dashboard_row(db: core.Session, row: core.GovernanceApproval) -> core.Approval:
    existing = db.get(core.Approval, row.id)
    if existing is None:
        existing = core.Approval(
            id=row.id,
            agent=row.agent_name or row.agent_id,
            action=row.action_name,
            tool=row.action_name,
            parameters=row.action_parameters_json,
            risk="Human review",
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
        agent, _, expires_at = _validate_action(db, payload, project_id)
        fingerprint = _fingerprint(payload)
        approval = db.scalar(
            select(core.GovernanceApproval).where(core.GovernanceApproval.request_id == payload.request_id)
        )
        if approval is not None:
            if approval.project_id != project_id or approval.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            previous = db.get(core.GovernanceEvaluation, payload.request_id)
            if previous is not None:
                result = json.loads(previous.response_json)
                result['approval_id'] = approval.id
                return result
            return {"outcome": "approval_required", "reason": "Human review is required", "approval_id": approval.id,
                    "risk_level": "medium", "reason_code": "EXISTING_APPROVAL", "matched_rule": None,
                    "policy_version": "legacy", "authorization_receipt": None}
        previous = db.get(core.GovernanceEvaluation, payload.request_id)
        if previous is not None:
            if previous.project_id != project_id or previous.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            return json.loads(previous.response_json)
        response = evaluate(load_project_policy(db, project_id), action_name=payload.action_name,
                            project_id=project_id, agent_id=agent.id)
        if response["outcome"] == "allow":
            authorization = core.AutomaticAuthorization(
                id=f"aut-{uuid.uuid4().hex}", request_id=payload.request_id,
                project_id=project_id, agent_id=agent.id, action_name=payload.action_name,
                action_parameters_json=_canonical(payload.action_parameters),
                policy_version=response["policy_version"], expires_at=expires_at,
                jti=uuid.uuid4().hex,
            )
            response["authorization_receipt"] = _automatic_receipt(authorization)
            db.add(authorization)
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


def load_project_policy(db: core.Session, project_id: str) -> Policy:
    """Missing, disabled, or corrupt tenant configuration always requires review."""
    row = db.get(core.ProjectPolicy, project_id)
    if row is None:
        return Policy(version="default")
    try:
        stored = ProjectPolicyIn.model_validate({
            "version": row.version, "enabled": row.enabled, "rules": json.loads(row.rules_json),
        })
        return Policy(version=stored.version, rules=stored.rules if stored.enabled else [])
    except (ValidationError, ValueError, TypeError):
        return Policy(version="invalid")


def _policy_dict(row: core.ProjectPolicy) -> dict[str, Any]:
    return {"project_id": row.project_id, "version": row.version, "enabled": row.enabled,
            "rules": json.loads(row.rules_json), "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at)}


@router.get("/api/v1/projects/{project_id}/policy")
def get_project_policy(project_id: str, user: core.User = Depends(core.current_user)) -> dict[str, Any]:
    with core.SessionLocal() as db:
        _require_membership(db, user, project_id)
        if db.get(core.Project, project_id) is None:
            raise HTTPException(404, "Project not found")
        row = db.get(core.ProjectPolicy, project_id)
        if row is None:
            raise HTTPException(404, "Project policy not configured")
        return _policy_dict(row)


@router.put("/api/v1/projects/{project_id}/policy")
def put_project_policy(
    project_id: str, payload: ProjectPolicyIn, user: core.User = Depends(core.current_user),
) -> dict[str, Any]:
    with core.SessionLocal() as db:
        _require_membership(db, user, project_id)
        if db.get(core.Project, project_id) is None:
            raise HTTPException(404, "Project not found")
        for rule in payload.rules:
            if rule.project_id is not None and rule.project_id != project_id:
                raise HTTPException(422, "Rule project must match the policy project")
            if rule.agent_id is not None:
                agent = db.get(core.Agent, rule.agent_id)
                if agent is None or agent.project_id != project_id:
                    raise HTTPException(422, "Rule agent must belong to the policy project")
        rules_json = _canonical([rule.model_dump() for rule in payload.rules])
        row = db.get(core.ProjectPolicy, project_id)
        if row is None:
            row = core.ProjectPolicy(project_id=project_id, version=payload.version,
                                     enabled=payload.enabled, rules_json=rules_json)
            db.add(row)
        elif (row.version, row.enabled, row.rules_json) == (payload.version, payload.enabled, rules_json):
            return _policy_dict(row)
        else:
            if row.version == payload.version:
                raise HTTPException(409, "Policy changes require a new version")
            row.version, row.enabled, row.rules_json = payload.version, payload.enabled, rules_json
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            db.commit()
        except (IntegrityError, StaleDataError):
            db.rollback()
            raise HTTPException(409, "Policy changed concurrently; fetch it and retry") from None
        return _policy_dict(row)


@router.post("/api/v1/approvals", status_code=201)
def create_approval(
    payload: core.SDKActionIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    project_id: str = Depends(current_project),
) -> dict[str, Any]:
    if idempotency_key != payload.request_id:
        raise HTTPException(400, "Idempotency-Key must match request_id")
    # Direct/manual callers must pass the same server policy gate as protected calls.
    result = evaluate_action(payload, idempotency_key, project_id)
    if result["outcome"] != "approval_required":
        raise HTTPException(403, "Only actions requiring human review can create an approval")
    with core.SessionLocal() as db:
        agent, requested_at, expires_at = _validate_action(db, payload, project_id)
        fingerprint = _fingerprint(payload)
        evaluation = db.get(core.GovernanceEvaluation, payload.request_id)
        if evaluation is not None:
            if evaluation.project_id != project_id or evaluation.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            if json.loads(evaluation.response_json).get("outcome") == "deny":
                raise HTTPException(403, "Action is denied by policy and cannot be submitted for approval")
        existing = db.scalar(
            select(core.GovernanceApproval).where(core.GovernanceApproval.request_id == payload.request_id)
        )
        if existing is not None:
            if existing.project_id != project_id or existing.body_hash != fingerprint:
                raise HTTPException(409, "Idempotency key was reused with different action data")
            return _approval_dict(existing)
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
        _sync_dashboard_row(db, row)
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
        evaluation = db.get(core.GovernanceEvaluation, payload.request_id)
        if evaluation is not None:
            if evaluation.project_id != project_id:
                raise HTTPException(403, "Evaluation does not belong to the authenticated project")
            if json.loads(evaluation.response_json).get("outcome") == "deny":
                raise HTTPException(403, "Action is denied by policy and cannot be authorized")
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
        if (
            claims.get("request_id") != payload.request_id
            or claims.get("project_id") != project_id
            or claims.get("agent_id") != payload.agent_id
            or claims.get("action_name") != payload.action_name
            or claims.get("action_hash") != parameters_hash
        ):
            raise HTTPException(403, "Authorization receipt does not match this action")
        if claims.get("kind") == "automatic":
            row = db.get(core.AutomaticAuthorization, claims["sub"])
            decision = json.loads(evaluation.response_json) if evaluation else {}
            if (
                payload.approval_id is not None or row is None
                or row.project_id != project_id or row.request_id != payload.request_id
                or row.agent_id != payload.agent_id or row.action_name != payload.action_name
                or row.action_parameters_json != _canonical(payload.action_parameters)
                or claims.get("policy_version") != row.policy_version
                or claims["jti"] != row.jti
                or claims["exp"] != int(row.expires_at.replace(tzinfo=timezone.utc).timestamp())
                or decision.get("outcome") != "allow"
                or decision.get("policy_version") != row.policy_version
                or decision.get("authorization_receipt") != payload.authorization_receipt
            ):
                raise HTTPException(403, "Automatic authorization does not match its persisted evaluation")
        else:
            if payload.approval_id is None or claims["sub"] != payload.approval_id:
                raise HTTPException(403, "Authorization receipt is not attached to an approval")
            row = db.get(core.GovernanceApproval, payload.approval_id)
            if (
                row is None
                or row.project_id != project_id
                or row.request_id != payload.request_id
                or row.agent_id != payload.agent_id
                or row.action_name != payload.action_name
                or row.action_parameters_json != _canonical(payload.action_parameters)
                or row.status != "approved"
                or payload.authorization_receipt != _receipt_for(row)
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

