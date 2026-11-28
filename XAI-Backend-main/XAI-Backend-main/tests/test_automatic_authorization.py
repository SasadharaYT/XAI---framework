"""Server-issued authority, persisted binding, and single-use enforcement."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import jwt
import pytest

from app import main, governance
from test_policy import _route_context, store_policy


@pytest.fixture
def context(monkeypatch):
    engine, sessions, action = _route_context(monkeypatch)
    monkeypatch.setenv('GOVERNANCE_RECEIPT_SECRET', 'testing-only-signing-key-at-least-32-characters')
    store_policy(sessions, 'allow')
    yield sessions, action
    engine.dispose()


def verification(action, receipt, **updates):
    return main.AuthorizationVerifyIn(**{
        'request_id': action.request_id, 'project_id': action.project_id,
        'agent_id': action.agent_id, 'action_name': action.action_name,
        'action_parameters': action.action_parameters, 'authorization_receipt': receipt,
        **updates,
    })


def test_allow_issues_one_receipt_and_retry_cannot_renew_it(context, monkeypatch):
    sessions, action = context
    first = governance.evaluate_action(action, 'r', 'p')
    assert first['outcome'] == 'allow'
    assert first['approval_id'] is None
    claims = jwt.decode(first['authorization_receipt'], governance._receipt_secret(), algorithms=['HS256'])
    assert claims['policy_version'] == 'v1'
    assert claims['action_hash'] == governance._parameters_hash(action.action_parameters)
    store_policy(sessions, 'deny', 'v2')
    retry = action.model_copy(update={'expires_at': action.expires_at + timedelta(minutes=1)})
    assert governance.evaluate_action(retry, 'r', 'p') == first
    request = verification(action, first['authorization_receipt'])
    assert governance.verify_authorization(request, 'p')['consumed'] is True
    assert governance.evaluate_action(retry, 'r', 'p') == first
    with pytest.raises(main.HTTPException) as exc:
        governance.verify_authorization(request, 'p')
    assert exc.value.status_code == 409
    with sessions() as db:
        assert db.query(main.AutomaticAuthorization).count() == 1
        assert db.query(main.GovernanceApproval).count() == 0
        assert db.query(main.AuthorizationUse).count() == 1


@pytest.mark.parametrize('updates', [
    {'request_id': 'other'}, {'project_id': 'other'}, {'agent_id': 'other'},
    {'action_name': 'other'}, {'action_parameters': {'amount': 123}}, {'approval_id': 'fake'},
])
def test_receipt_cannot_authorize_a_different_action(context, updates):
    sessions, action = context
    result = governance.evaluate_action(action, 'r', 'p')
    with pytest.raises(main.HTTPException) as exc:
        governance.verify_authorization(verification(action, result['authorization_receipt'], **updates), 'p')
    assert exc.value.status_code == 403
    with sessions() as db:
        assert db.query(main.AuthorizationUse).count() == 0


@pytest.mark.parametrize('claim,value,code', [
    ('policy_version', 'forged', 403), ('jti', 'forged', 403),
    ('sub', 'forged', 403), ('exp', 1, 410), ('kind', 'human', 403),
])
def test_even_signed_claims_must_match_server_record(context, claim, value, code):
    _, action = context
    result = governance.evaluate_action(action, 'r', 'p')
    claims = jwt.decode(result['authorization_receipt'], governance._receipt_secret(), algorithms=['HS256'])
    claims[claim] = value
    receipt = jwt.encode(claims, governance._receipt_secret(), algorithm='HS256')
    with pytest.raises(main.HTTPException) as exc:
        governance.verify_authorization(verification(action, receipt), 'p')
    assert exc.value.status_code == code


def test_invalid_signature_and_missing_server_state_block(context):
    sessions, action = context
    result = governance.evaluate_action(action, 'r', 'p')
    claims = jwt.decode(result['authorization_receipt'], governance._receipt_secret(), algorithms=['HS256'])
    forged = jwt.encode(claims, 'different-key-that-is-at-least-32-characters', algorithm='HS256')
    with pytest.raises(main.HTTPException):
        governance.verify_authorization(verification(action, forged), 'p')
    with sessions() as db:
        db.query(main.GovernanceEvaluation).delete()
        db.commit()
    with pytest.raises(main.HTTPException):
        governance.verify_authorization(verification(action, result['authorization_receipt']), 'p')


def test_unconfigured_signing_does_not_persist_allow(context, monkeypatch):
    sessions, action = context
    monkeypatch.delenv('GOVERNANCE_RECEIPT_SECRET')
    with pytest.raises(main.HTTPException) as exc:
        governance.evaluate_action(action, 'r', 'p')
    assert exc.value.status_code == 503
    with sessions() as db:
        assert db.query(main.AutomaticAuthorization).count() == 0
        assert db.query(main.GovernanceEvaluation).count() == 0


@pytest.mark.parametrize('outcome', ['allow', 'deny'])
def test_direct_approval_cannot_bypass_policy(context, monkeypatch, outcome):
    sessions, action = context
    store_policy(sessions, outcome)
    with pytest.raises(main.HTTPException) as exc:
        governance.create_approval(action, 'r', 'p')
    assert exc.value.status_code == 403
    with sessions() as db:
        assert db.query(main.GovernanceApproval).count() == 0


def test_human_review_still_requires_approved_single_use_receipt(context, monkeypatch):
    sessions, action = context
    with sessions() as db:
        db.query(main.ProjectPolicy).delete()
        db.commit()
    result = governance.evaluate_action(action, 'r', 'p')
    assert result['outcome'] == 'approval_required'
    assert result['authorization_receipt'] is None
    approval = governance.create_approval(action, 'r', 'p')
    with sessions() as db:
        db.add(main.TeamMember(id='reviewer', name='Reviewer', email='reviewer@example.test',
                              initials='R', role='Owner', status='Active', joined_date='Today', project_ids='p'))
        db.commit()
        user = main.User(id='reviewer', email='reviewer@example.test', name='Reviewer', password_hash='unused')
        row = db.get(main.GovernanceApproval, approval['approval_id'])
        receipt = governance._receipt_for(row)
        with pytest.raises(main.HTTPException):
            governance.verify_authorization(verification(action, receipt, approval_id=row.id), 'p')
        assert governance.record_dashboard_decision(db, row.id, 'Approved', 'Reviewed', user)
        db.commit()
    approved = governance.get_approval_status(approval['approval_id'], 'p')
    request = verification(action, approved['decision']['authorization_receipt'], approval_id=approval['approval_id'])
    assert governance.verify_authorization(request, 'p')['valid'] is True
    with pytest.raises(main.HTTPException) as exc:
        governance.verify_authorization(request, 'p')
    assert exc.value.status_code == 409


def test_concurrent_consumers_only_one_succeeds(context, monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    _, action = context
    engine = create_engine(f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}",
                           connect_args={'check_same_thread': False})
    main.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'SessionLocal', sessions)
    with sessions() as db:
        db.add(main.Project(id='p', name='Test', description='', type='test', risk='Low', last_activity='', accent=''))
        db.add(main.Agent(id='a', project_id='p', name='Agent', description='', framework='', endpoint=''))
        db.commit()
    try:
        store_policy(sessions, 'allow')
        # Concurrent evaluation retries must also return exactly the same authority.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: governance.evaluate_action(action, 'r', 'p'), range(4)))
        assert all(result == results[0] for result in results)
        request = verification(action, results[0]['authorization_receipt'])

        def consume(_):
            try:
                governance.verify_authorization(request, 'p')
                return 200
            except main.HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert sorted(pool.map(consume, range(4))) == [200, 409, 409, 409]
        with sessions() as db:
            assert db.query(main.AutomaticAuthorization).count() == 1
            assert db.query(main.AuthorizationUse).count() == 1
    finally:
        engine.dispose()
