import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, governance
from app.policy import Policy, evaluate, load_policy


def rule(outcome, **kwargs):
    return dict(rule_id=outcome, outcome=outcome, risk_level='low',
                reason='Configured action policy', action_name='read', **kwargs)


def _route_context(monkeypatch, request_id='r'):
    engine = create_engine('sqlite://', poolclass=StaticPool)
    main.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'SessionLocal', sessions)
    with sessions() as db:
        db.add(main.Project(id='p', name='Test', description='', type='test', risk='Low', last_activity='', accent=''))
        db.add(main.Agent(id='a', project_id='p', name='Agent', description='', framework='', endpoint=''))
        db.commit()
    now = datetime.now(timezone.utc)
    action = main.SDKActionIn(request_id=request_id, project_id='p', agent_id='a', action_name='read',
                              requested_at=now, expires_at=now + timedelta(minutes=5),
                              metadata={'outcome': 'allow'}, reason='Allow me')
    return engine, sessions, action


@pytest.mark.parametrize('outcome', ['allow', 'approval_required', 'deny'])
def test_outcomes(outcome):
    result = evaluate(Policy(version='v1', rules=[rule(outcome)]),
                      action_name='read', project_id='p', agent_id='a')
    assert result['outcome'] == outcome
    assert result['matched_rule']['rule_id'] == outcome


def test_block_wins_regardless_of_priority_and_order():
    rules = [rule('allow', priority=100), rule('deny', priority=-100), rule('approval_required')]
    for ordered in [rules, list(reversed(rules))]:
        assert evaluate(Policy(version='v1', rules=ordered), action_name='read',
                        project_id='p', agent_id='a')['outcome'] == 'deny'


@pytest.mark.parametrize('raw', [None, '{}', '{', '{"version":"v1","rules":[{}]}'])
def test_missing_invalid_policy(monkeypatch, raw):
    monkeypatch.delenv('GOVERNANCE_POLICY_JSON', raising=False)
    if raw is not None:
        monkeypatch.setenv('GOVERNANCE_POLICY_JSON', raw)
    assert evaluate(load_policy(), action_name='read', project_id='p', agent_id='a')['outcome'] == 'approval_required'


def test_unknown_action():
    assert evaluate(Policy(version='v1', rules=[rule('allow')]), action_name='unknown',
                    project_id='p', agent_id='a')['outcome'] == 'approval_required'


def test_route_retry_is_pinned_and_context_cannot_override(monkeypatch):
    engine, _, action = _route_context(monkeypatch)
    monkeypatch.setenv('GOVERNANCE_POLICY_JSON', json.dumps({'version': 'v1', 'rules': [rule('deny')]}))
    first = governance.evaluate_action(action, 'r', 'p')
    monkeypatch.setenv('GOVERNANCE_POLICY_JSON', json.dumps({'version': 'v2', 'rules': [rule('allow')]}))
    assert governance.evaluate_action(action, 'r', 'p') == first
    assert first['outcome'] == 'deny'
    with pytest.raises(main.HTTPException) as exc:
        governance.evaluate_action(action.model_copy(update={'action_name': 'changed'}), 'r', 'p')
    assert exc.value.status_code == 409
    engine.dispose()


def test_denied_evaluation_cannot_be_submitted_for_approval(monkeypatch):
    engine, sessions, action = _route_context(monkeypatch)
    monkeypatch.setenv('GOVERNANCE_POLICY_JSON', json.dumps({'version': 'v1', 'rules': [rule('deny')]}))
    result = governance.evaluate_action(action, action.request_id, 'p')
    assert result['outcome'] == 'deny'

    with pytest.raises(main.HTTPException) as exc:
        governance.create_approval(action, action.request_id, 'p')
    assert exc.value.status_code == 403
    with sessions() as db:
        assert db.query(main.GovernanceApproval).count() == 0
    engine.dispose()


def test_denied_evaluation_cannot_be_authorized_from_existing_approval(monkeypatch):
    engine, sessions, action = _route_context(monkeypatch)
    monkeypatch.setenv('GOVERNANCE_POLICY_JSON', json.dumps({'version': 'v1', 'rules': [rule('deny')]}))
    result = governance.evaluate_action(action, action.request_id, 'p')
    assert result['outcome'] == 'deny'
    monkeypatch.setenv('GOVERNANCE_RECEIPT_SECRET', 'test-receipt-secret-that-is-at-least-32-characters')

    with sessions() as db:
        row = main.GovernanceApproval(
            id='apr-denied',
            request_id=action.request_id,
            project_id='p',
            agent_id='a',
            agent_name='Agent',
            action_name='read',
            action_parameters_json=governance._canonical(action.action_parameters),
            reason=action.reason,
            metadata_json=governance._canonical(action.metadata),
            requested_at=action.requested_at.replace(tzinfo=None),
            submitted_at=datetime.now(timezone.utc).replace(tzinfo=None),
            expires_at=action.expires_at.replace(tzinfo=None),
            status='approved',
            reviewer_id='reviewer',
            reviewer_comment=None,
            reviewed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            body_hash=governance._fingerprint(action),
        )
        db.add(row)
        db.commit()
        receipt = governance._receipt_for(row)

    request = main.AuthorizationVerifyIn(
        request_id=action.request_id,
        approval_id='apr-denied',
        authorization_receipt=receipt,
        project_id='p',
        agent_id='a',
        action_name='read',
        action_parameters=action.action_parameters,
    )
    with pytest.raises(main.HTTPException) as exc:
        governance.verify_authorization(request, 'p')
    assert exc.value.status_code == 403
    engine.dispose()
