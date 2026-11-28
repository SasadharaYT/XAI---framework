"""Policy validation, authenticated tenant boundaries, and stored evaluation."""

import hashlib
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app import main, governance
from test_policy import _route_context, rule, store_policy


@pytest.fixture
def api(monkeypatch):
    engine, sessions, action = _route_context(monkeypatch)
    monkeypatch.setenv('GOVERNANCE_RECEIPT_SECRET', 'test-signing-key-at-least-thirty-two-characters')
    users = {}
    with sessions() as db:
        db.add(main.Project(id='q', name='Other', description='', type='test', risk='Low', last_activity='', accent=''))
        db.add(main.Agent(id='b', project_id='q', name='Other', description='', framework='', endpoint=''))
        for name, projects, status in [('member', 'p', 'Active'), ('other', 'q', 'Active'),
                                       ('inactive', 'p', 'Inactive'), ('outsider', '', 'Active')]:
            user = main.User(id=name, email=f'{name}@test.example', name=name, password_hash='unused')
            users[name] = user
            db.add(user)
            db.add(main.TeamMember(id=name, name=name, email=user.email, initials='T', role='Member',
                                  status=status, joined_date='Today', project_ids=projects))
        db.add(main.ProjectAPIKey(id='key', project_id='p', name='SDK', key_prefix='sdk',
                                 key_hash=hashlib.sha256(b'sdk-key').hexdigest(), created_by='member'))
        db.commit()
    client = TestClient(main.app)
    headers = {name: {'Authorization': f'Bearer {main.issue_token(user)}'} for name, user in users.items()}
    yield client, headers, sessions, action
    client.close()
    engine.dispose()


def payload(outcome='deny', version='v1', **updates):
    return {'version': version, 'enabled': True, 'rules': [rule(outcome)], **updates}


def test_create_get_replace_disable_and_timestamps(api):
    client, headers, _, _ = api
    path = '/api/v1/projects/p/policy'
    assert client.get(path, headers=headers['member']).status_code == 404
    first = client.put(path, headers=headers['member'], json=payload()).json()
    assert first['project_id'] == 'p'
    assert first['enabled'] is True
    assert datetime.fromisoformat(first['created_at'].replace('Z', '+00:00')).tzinfo is not None
    assert client.get(path, headers=headers['member']).json() == first
    assert client.put(path, headers=headers['member'], json=payload()).json() == first
    assert client.put(path, headers=headers['member'], json=payload('allow')).status_code == 409
    updated = client.put(path, headers=headers['member'], json=payload('allow', 'v2', enabled=False))
    assert updated.status_code == 200
    assert updated.json()['created_at'] == first['created_at']
    assert updated.json()['updated_at'] >= first['updated_at']
    assert updated.json()['enabled'] is False
    assert updated.json()['version'] == 'v2'


@pytest.mark.parametrize('method', ['get', 'put'])
@pytest.mark.parametrize('identity,status', [(None, 401), ('other', 403), ('inactive', 403), ('outsider', 403), ('sdk', 401)])
def test_authentication_and_project_membership(api, method, identity, status):
    client, headers, _, _ = api
    auth = {'Authorization': 'Bearer sdk-key'} if identity == 'sdk' else headers.get(identity, {})
    kwargs = {'json': payload()} if method == 'put' else {}
    assert getattr(client, method)('/api/v1/projects/p/policy', headers=auth, **kwargs).status_code == status


@pytest.mark.parametrize('body', [
    {'enabled': True, 'rules': []}, payload(version=''), payload(version='   '),
    payload(enabled='true'), payload(rules=[{}]), payload(rules=[rule('allow'), rule('allow')]),
    payload(rules=[dict(rule('allow'), unknown=True)]), payload(rules=[dict(rule('allow'), outcome='ALLOW')]),
    payload(rules=[dict(rule('allow'), risk_level='unknown')]),
    payload(rules=[dict(rule('allow'), priority='1')]), payload(rules=[dict(rule('allow'), action_name=' ')]),
    payload(project_id='q'), payload(rules=[dict(rule('allow'), project_id='q')]),
    payload(rules=[dict(rule('allow'), agent_id='b')]), payload(rules=[dict(rule('allow'), agent_id='missing')]),
])
def test_invalid_policy_is_never_persisted(api, body):
    client, headers, sessions, _ = api
    response = client.put('/api/v1/projects/p/policy', headers=headers['member'], json=body)
    assert response.status_code == 422
    with sessions() as db:
        assert db.query(main.ProjectPolicy).count() == 0


def test_project_isolation_on_read_write_and_evaluation(api):
    client, headers, sessions, action = api
    assert client.put('/api/v1/projects/p/policy', headers=headers['member'], json=payload('allow')).status_code == 200
    assert client.put('/api/v1/projects/q/policy', headers=headers['other'], json=payload('deny')).status_code == 200
    for method in ['get', 'put']:
        kwargs = {'json': payload('allow', 'v2')} if method == 'put' else {}
        assert getattr(client, method)('/api/v1/projects/q/policy', headers=headers['member'], **kwargs).status_code == 403
    assert governance.evaluate_action(action, 'r', 'p')['outcome'] == 'allow'
    other = action.model_copy(update={'request_id': 'q-request', 'project_id': 'q', 'agent_id': 'b'})
    assert governance.evaluate_action(other, 'q-request', 'q')['outcome'] == 'deny'
    with pytest.raises(main.HTTPException):
        governance.evaluate_action(other, 'q-request', 'p')
    assert client.get('/api/v1/projects/q/policy', headers=headers['other']).json()['version'] == 'v1'


@pytest.mark.parametrize('state', ['missing', 'disabled', 'corrupt', 'unknown'])
def test_conservative_fallback_ignores_global_allow(api, monkeypatch, state):
    _, _, sessions, action = api
    monkeypatch.setenv('GOVERNANCE_POLICY_JSON', json.dumps({'version': 'global', 'rules': [rule('allow')]}))
    if state != 'missing':
        store_policy(sessions, 'allow')
        with sessions() as db:
            row = db.get(main.ProjectPolicy, 'p')
            if state == 'disabled':
                row.enabled = False
            elif state == 'corrupt':
                row.rules_json = '{'
            else:
                action = action.model_copy(update={'action_name': 'unknown'})
            db.commit()
    result = governance.evaluate_action(action, 'r', 'p')
    assert result['outcome'] == 'approval_required'
    assert result['authorization_receipt'] is None


def test_updates_affect_new_requests_but_preserve_original_decisions(api):
    client, headers, _, action = api
    path = '/api/v1/projects/p/policy'
    client.put(path, headers=headers['member'], json=payload('deny'))
    first = governance.evaluate_action(action, 'r', 'p')
    client.put(path, headers=headers['member'], json=payload('allow', 'v2'))
    assert governance.evaluate_action(action, 'r', 'p') == first
    new = action.model_copy(update={'request_id': 'new'})
    result = governance.evaluate_action(new, 'new', 'p')
    assert (result['outcome'], result['policy_version']) == ('allow', 'v2')
    assert result['authorization_receipt']


def test_additive_schema_creation_is_repeatable(api):
    _, _, sessions, _ = api
    engine = sessions.kw['bind']
    main.Base.metadata.create_all(engine)
    main.Base.metadata.create_all(engine)
    assert 'project_policies' in inspect(engine).get_table_names()
    with sessions() as db:
        assert db.get(main.Project, 'p') is not None


def test_project_key_evaluation_uses_validated_scoped_rule(api):
    client, headers, _, action = api
    body = payload('deny', rules=[dict(rule('deny'), project_id='p', agent_id='a')])
    assert client.put('/api/v1/projects/p/policy', headers=headers['member'], json=body).status_code == 200
    auth = {'Authorization': 'Bearer sdk-key', 'Idempotency-Key': 'r'}
    response = client.post('/api/v1/oversight/evaluate', headers=auth, json=action.model_dump(mode='json'))
    assert response.status_code == 200
    assert response.json()['outcome'] == 'deny'
    assert response.json()['policy_version'] == 'v1'
    assert response.json()['matched_rule']['rule_id'] == 'deny'


def test_concurrent_updates_do_not_silently_overwrite(api):
    from sqlalchemy.orm.exc import StaleDataError

    _, _, sessions, _ = api
    store_policy(sessions, 'deny')
    with sessions() as first, sessions() as second:
        first_row = first.get(main.ProjectPolicy, 'p')
        second_row = second.get(main.ProjectPolicy, 'p')
        first_row.version = 'v2'
        first.commit()
        second_row.version = 'v3'
        with pytest.raises(StaleDataError):
            second.commit()
