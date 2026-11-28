"""Exercise real SDK wrappers against the backend's HTTP routes and database."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from test_automatic_authorization import context  # noqa: F401
from test_policy import rule

sys.path.insert(0, str(Path(__file__).resolve().parents[3] /
                       'XAI-human-oversight-sdk-main/XAI-human-oversight-sdk-main/src'))
from governance_sdk import ApprovalRejectedError, AuthorizationVerificationError, Governance


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('outcome', ['allow', 'approval_required', 'deny'])
def test_real_sdk_backend_execution(context, monkeypatch, asynchronous, outcome):
    sessions, _ = context
    monkeypatch.setenv('GOVERNANCE_POLICY_JSON', json.dumps({'version': 'v1', 'rules': [rule(outcome)]}))
    with sessions() as db:
        db.add(main.ProjectAPIKey(id='key', project_id='p', key_hash=hashlib.sha256(b'test-key').hexdigest(),
                                 key_prefix='test', name='test', created_by='reviewer'))
        db.add(main.TeamMember(id='reviewer', name='Reviewer', email='reviewer@example.test',
                              initials='R', role='Owner', status='Active', joined_date='Today', project_ids='p'))
        db.commit()
    backend = TestClient(main.app)
    observed = []

    def forward(request):
        observed.append(request.url.path)
        if request.method == 'GET' and request.url.path.startswith('/api/v1/approvals/'):
            from app.governance import record_dashboard_decision
            with sessions() as db:
                user = main.User(id='reviewer', email='reviewer@example.test', name='Reviewer', password_hash='unused')
                record_dashboard_decision(db, request.url.path.rsplit('/', 1)[1], 'Approved', 'Checked', user)
                db.commit()
        response = backend.request(request.method, request.url.path,
                                   headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, json=response.json())

    client = Governance(api_key='test-key', project_id='p', agent_id='a', base_url='https://test', poll_interval=0.001)
    client._http._sync.close()
    client._http._sync = httpx.Client(base_url='https://test', transport=httpx.MockTransport(forward),
                                     headers={'Authorization': 'Bearer test-key'})
    client._http._async = httpx.AsyncClient(base_url='https://test', transport=httpx.MockTransport(forward),
                                           headers={'Authorization': 'Bearer test-key'})
    invoked = []
    fixed_id = '12345678-1234-4234-8234-123456789012'

    @client.protect(action_name='read', idempotency_key=lambda **kw: fixed_id)
    def sync_tool(**kwargs):
        invoked.append(kwargs)
        return 'executed'

    @client.protect(action_name='read', idempotency_key=lambda **kw: fixed_id)
    async def async_tool(**kwargs):
        invoked.append(kwargs)
        return 'executed'

    def run():
        return asyncio.run(async_tool(value=42)) if asynchronous else sync_tool(value=42)

    try:
        if outcome == 'deny':
            with pytest.raises(ApprovalRejectedError):
                run()
            assert invoked == []
            assert observed == ['/api/v1/oversight/evaluate']
        else:
            assert run() == 'executed'
            assert observed[-1] == '/api/v1/authorizations/verify'
            with pytest.raises(AuthorizationVerificationError):
                run()
            assert len(invoked) == 1
        with sessions() as db:
            assert db.query(main.GovernanceApproval).count() == (1 if outcome == 'approval_required' else 0)
    finally:
        client.close()
        asyncio.run(client.aclose())
        backend.close()
