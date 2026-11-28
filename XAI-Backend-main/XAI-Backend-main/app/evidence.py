"""Read-only project evidence. Never mint receipts or infer execution events."""
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from . import main as core
from .governance import _CREDENTIAL_MARKERS, _iso, _require_membership

router = APIRouter()
_FIELDS = ('outcome', 'risk_level', 'reason', 'reason_code', 'matched_rule', 'policy_version')


def _object(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ('[redacted]' if any(marker in ''.join(c.lower() for c in key if c.isalnum())
                    for marker in _CREDENTIAL_MARKERS) else _redact(child)) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


def _evaluation(raw: str) -> dict:
    data = _object(raw)
    values, states = {}, {}
    for name in _FIELDS:
        value = data.get(name) if data is not None else None
        state = 'not_recorded' if data is None or name not in data else 'recorded_null' if value is None else 'recorded'
        valid = value is None or isinstance(value, str)
        if name == 'outcome':
            valid = value is None or value in ('allow', 'approval_required', 'deny')
        elif name == 'risk_level':
            valid = value is None or value in ('low', 'medium', 'high', 'critical')
        elif name == 'matched_rule':
            valid = value is None or (isinstance(value, dict) and isinstance(value.get('rule_id'), str)
                    and value.get('effect') in ('allow', 'approval_required', 'deny')
                    and type(value.get('priority')) is int)
            if valid and value is not None:
                value = {key: value[key] for key in ('rule_id', 'effect', 'priority')}
        if not valid:
            value, state = None, 'invalid'
        values[name], states[name] = value, state
    return {**values, 'field_states': states, 'record_state': 'recorded' if data is not None else 'unreadable'}


def _evidence(db: core.Session, row: core.GovernanceEvaluation) -> dict:
    scope = (row.project_id, row.request_id)
    approval = db.scalar(select(core.GovernanceApproval).where(
        core.GovernanceApproval.project_id == scope[0], core.GovernanceApproval.request_id == scope[1]))
    snapshot = db.scalar(select(core.EvaluationContext).where(
        core.EvaluationContext.project_id == scope[0], core.EvaluationContext.request_id == scope[1]))
    context, source = None, None
    if snapshot is not None:
        original = _object(snapshot.context_json)
        if original is not None:
            context = {name: original[name] for name in ('agent_id', 'agent_name', 'action_name',
                       'action_parameters', 'reason', 'metadata', 'requested_at', 'expires_at') if name in original}
            source = 'evaluation_snapshot'
    elif approval is not None:
        context = {'agent_id': approval.agent_id, 'agent_name': approval.agent_name,
                   'action_name': approval.action_name, 'action_parameters': _object(approval.action_parameters_json),
                   'reason': approval.reason, 'metadata': _object(approval.metadata_json),
                   'requested_at': _iso(approval.requested_at), 'expires_at': _iso(approval.expires_at)}
        source = 'approval_record'
    else:
        automatic = db.scalar(select(core.AutomaticAuthorization).where(
            core.AutomaticAuthorization.project_id == scope[0], core.AutomaticAuthorization.request_id == scope[1]))
        if automatic is not None:
            context = {'agent_id': automatic.agent_id, 'action_name': automatic.action_name,
                       'action_parameters': _object(automatic.action_parameters_json), 'expires_at': _iso(automatic.expires_at)}
            source = 'automatic_authorization'
    review = None if approval is None else {
        'approval_id': approval.id, 'status': approval.status, 'submitted_at': _iso(approval.submitted_at),
        'expires_at': _iso(approval.expires_at), 'reviewer_id': approval.reviewer_id,
        'reviewer_comment': approval.reviewer_comment, 'reviewed_at': _iso(approval.reviewed_at),
    }
    consumptions = db.scalars(select(core.AuthorizationUse).where(
        core.AuthorizationUse.project_id == scope[0], core.AuthorizationUse.request_id == scope[1]
    ).order_by(core.AuthorizationUse.consumed_at)).all()
    return {'request_id': row.request_id, 'project_id': row.project_id, 'evaluated_at': _iso(row.created_at),
            'evaluation': _evaluation(row.response_json), 'context': _redact(context), 'context_source': source,
            'human_review': review, 'authorization_consumptions': [_iso(use.consumed_at) for use in consumptions],
            'execution_outcome': None, 'model_reasoning': None, 'integrity_status': 'not_verified'}


def _access(db: core.Session, user: core.User, project_id: str) -> None:
    _require_membership(db, user, project_id)
    if db.get(core.Project, project_id) is None:
        raise HTTPException(404, 'Project not found')


@router.get('/api/v1/projects/{project_id}/evaluations')
def list_evaluations(project_id: str, limit: int = Query(50, ge=1, le=100),
                     offset: int = Query(0, ge=0), user: core.User = Depends(core.current_user)) -> dict:
    with core.SessionLocal() as db:
        _access(db, user, project_id)
        condition = core.GovernanceEvaluation.project_id == project_id
        rows = db.scalars(select(core.GovernanceEvaluation).where(condition).order_by(
            core.GovernanceEvaluation.created_at.desc(), core.GovernanceEvaluation.request_id.desc()
        ).limit(limit).offset(offset)).all()
        total = db.scalar(select(func.count()).select_from(core.GovernanceEvaluation).where(condition))
        return {'project_id': project_id, 'items': [_evidence(db, row) for row in rows],
                'total': total, 'limit': limit, 'offset': offset}


@router.get('/api/v1/projects/{project_id}/evaluations/{request_id}')
def get_evaluation(project_id: str, request_id: str, user: core.User = Depends(core.current_user)) -> dict:
    with core.SessionLocal() as db:
        _access(db, user, project_id)
        row = db.scalar(select(core.GovernanceEvaluation).where(
            core.GovernanceEvaluation.project_id == project_id, core.GovernanceEvaluation.request_id == request_id))
        if row is None:
            raise HTTPException(404, 'Evaluation not found')
        return _evidence(db, row)
