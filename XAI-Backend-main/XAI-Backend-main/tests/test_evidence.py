"""Recorded evidence must stay scoped, immutable and separate from execution."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import main, governance
from test_project_policy import api  # noqa: F401
from test_policy import store_policy


def test_evaluation_snapshot_and_safe_decision_projection(api):
    client, headers, sessions, action = api
    store_policy(sessions, 'allow')
    original = governance.evaluate_action(action, 'r', 'p')
    governance.evaluate_action(action, 'r', 'p')
    result = client.get('/api/v1/projects/p/evaluations/r', headers=headers['member'])
    assert result.status_code == 200
    record = result.json()
    assert record['evaluation']['outcome'] == 'allow'
    assert record['evaluation']['policy_version'] == 'v1'
    assert record['evaluation']['matched_rule']['rule_id'] == 'allow'
    assert record['context']['reason'] == 'Allow me'
    assert record['context_source'] == 'evaluation_snapshot'
    assert record['model_reasoning'] is None
    assert record['execution_outcome'] is None
    assert record['authorization_consumptions'] == []
    assert original['authorization_receipt'] not in result.text
    assert 'authorization_receipt' not in result.text
    assert 'body_hash' not in result.text
    with sessions() as db:
        assert db.query(main.EvaluationContext).count() == 1


@pytest.mark.parametrize('identity,status', [(None, 401), ('other', 403), ('inactive', 403), ('outsider', 403), ('sdk', 401)])
@pytest.mark.parametrize('suffix', ['', '/r'])
def test_evidence_membership_and_authentication(api, identity, status, suffix):
    client, headers, _, _ = api
    auth = {'Authorization': 'Bearer sdk-key'} if identity == 'sdk' else headers.get(identity, {})
    response = client.get('/api/v1/projects/p/evaluations' + suffix, headers=auth)
    assert response.status_code == status


def test_project_isolation_and_pagination(api):
    client, headers, sessions, action = api
    governance.evaluate_action(action, 'r', 'p')
    with sessions() as db:
        db.add(main.GovernanceEvaluation(request_id='foreign', project_id='q', body_hash='unused', response_json='{}'))
        db.commit()
    page = client.get('/api/v1/projects/p/evaluations?limit=1&offset=0', headers=headers['member']).json()
    assert page['total'] == 1
    assert [item['request_id'] for item in page['items']] == ['r']
    assert client.get('/api/v1/projects/p/evaluations/foreign', headers=headers['member']).status_code == 404
    assert client.get('/api/v1/projects/p/evaluations?offset=1', headers=headers['member']).json()['items'] == []
    for query in ['limit=101', 'offset=-1', 'limit=0']:
        assert client.get('/api/v1/projects/p/evaluations?' + query, headers=headers['member']).status_code == 422


def test_legacy_missing_fields_are_not_reported_as_checks(api):
    client, headers, sessions, _ = api
    with sessions() as db:
        for request_id, raw in [('legacy', '{"outcome":"deny"}'), ('null', '{"matched_rule":null,"reason_code":"NO_MATCH"}'), ('corrupt', '{')]:
            db.add(main.GovernanceEvaluation(request_id=request_id, project_id='p', body_hash='unused', response_json=raw))
        db.commit()
    legacy = client.get('/api/v1/projects/p/evaluations/legacy', headers=headers['member']).json()
    assert legacy['context'] is None
    assert legacy['evaluation']['field_states']['matched_rule'] == 'not_recorded'
    assert legacy['evaluation']['risk_level'] is None
    null = client.get('/api/v1/projects/p/evaluations/null', headers=headers['member']).json()
    assert null['evaluation']['field_states']['matched_rule'] == 'recorded_null'
    corrupt = client.get('/api/v1/projects/p/evaluations/corrupt', headers=headers['member']).json()
    assert corrupt['evaluation']['record_state'] == 'unreadable'
    assert corrupt['evaluation']['outcome'] is None


def test_review_outcomes_are_read_without_signing_or_status_mutation(api, monkeypatch):
    client, headers, sessions, action = api
    approval = governance.create_approval(action, 'r', 'p')
    approved = client.post(f"/api/v1/approvals/{approval['approval_id']}/decision",
                           headers=headers['member'], json={'decision': 'Approved', 'comment': 'Checked the ticket'})
    assert approved.status_code == 200
    monkeypatch.delenv('GOVERNANCE_RECEIPT_SECRET')
    record = client.get('/api/v1/projects/p/evaluations/r', headers=headers['member']).json()
    assert record['human_review']['status'] == 'approved'
    assert record['human_review']['reviewer_comment'] == 'Checked the ticket'
    assert record['human_review']['reviewer_id'] == 'member'
    assert record['human_review']['reviewed_at']
    assert record['authorization_consumptions'] == []
    assert 'authorization_receipt' not in json.dumps(record)
    with sessions() as db:
        db.query(main.EvaluationContext).delete()
        db.commit()
    assert client.get('/api/v1/projects/p/evaluations/r', headers=headers['member']).json()['context_source'] == 'approval_record'


def test_receipt_consumption_does_not_establish_execution(api):
    client, headers, sessions, action = api
    store_policy(sessions, 'allow')
    evaluation = governance.evaluate_action(action, 'r', 'p')
    verify = main.AuthorizationVerifyIn(request_id='r', project_id='p', agent_id='a', action_name='read',
                                        action_parameters={}, authorization_receipt=evaluation['authorization_receipt'])
    governance.verify_authorization(verify, 'p')
    with sessions() as db:
        db.query(main.EvaluationContext).delete()
        db.commit()
    record = client.get('/api/v1/projects/p/evaluations/r', headers=headers['member']).json()
    assert len(record['authorization_consumptions']) == 1
    assert record['execution_outcome'] is None
    assert record['context_source'] == 'automatic_authorization'
    assert 'reason' not in record['context']


def test_context_redaction_and_retry_cannot_invent_legacy_evidence(api):
    client, headers, sessions, action = api
    governance.evaluate_action(action, 'r', 'p')
    with sessions() as db:
        row = db.get(main.EvaluationContext, 'r')
        original = json.loads(row.context_json)
        original['metadata'] = {'nested': {'access_token': 'must-not-appear'}}
        row.context_json = json.dumps(original)
        db.commit()
    response = client.get('/api/v1/projects/p/evaluations/r', headers=headers['member'])
    assert 'must-not-appear' not in response.text
    assert response.json()['context']['metadata']['nested']['access_token'] == '[redacted]'
    with sessions() as db:
        db.query(main.EvaluationContext).delete()
        db.commit()
    governance.evaluate_action(action, 'r', 'p')
    assert client.get('/api/v1/projects/p/evaluations/r', headers=headers['member']).json()['context'] is None


def test_reading_past_deadline_does_not_invent_an_expiry_transition(api):
    client, headers, sessions, action = api
    approval = governance.create_approval(action, 'r', 'p')
    with sessions() as db:
        row = db.get(main.GovernanceApproval, approval['approval_id'])
        row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    record = client.get('/api/v1/projects/p/evaluations/r', headers=headers['member']).json()
    assert record['human_review']['status'] == 'pending'
    assert record['human_review']['reviewed_at'] is None
    with sessions() as db:
        assert db.get(main.GovernanceApproval, approval['approval_id']).status == 'pending'


def test_invalid_stored_fields_are_not_projected_as_valid_evidence(api):
    client, headers, sessions, _ = api
    with sessions() as db:
        db.add(main.GovernanceEvaluation(request_id='invalid', project_id='p', body_hash='unused',
               response_json=json.dumps({'outcome': 'success', 'risk_level': 0, 'reason': ['invented'],
                                         'matched_rule': {'rule_id': 'fake'}, 'authorization_receipt': 'hidden'})))
        db.commit()
    response = client.get('/api/v1/projects/p/evaluations/invalid', headers=headers['member'])
    for field in ['outcome', 'risk_level', 'reason', 'matched_rule']:
        assert response.json()['evaluation'][field] is None
        assert response.json()['evaluation']['field_states'][field] == 'invalid'
    assert 'hidden' not in response.text
