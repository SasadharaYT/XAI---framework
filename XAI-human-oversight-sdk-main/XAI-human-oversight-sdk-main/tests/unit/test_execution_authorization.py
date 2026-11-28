"""Sync/async execution must fail closed at every authorization boundary."""

import asyncio
import httpx
import pytest

from governance_sdk import GovernanceError
from test_governance import _approved_routes, _auto_allow_routes


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('outcome', ['allow', 'approval_required'])
@pytest.mark.parametrize('failure', ['missing', 'invalid', 'expired', 'replayed', 'unavailable', 'unconsumed', 'flag_only'])
def test_unverifiable_authority_never_executes(mocked_governance, asynchronous, outcome, failure):
    client = mocked_governance
    calls = _auto_allow_routes(client) if outcome == 'allow' else _approved_routes(client)
    invoked = []
    if failure == 'missing':
        if outcome == 'allow':
            client._test_handlers['/api/v1/oversight/evaluate'] = lambda r: httpx.Response(200, json={'outcome': 'allow'})
        else:
            client._test_handlers['/api/v1/approvals/approval_1'] = lambda r: httpx.Response(200, json={
                'status': 'approved', 'approved': True,
            })
    else:
        responses = {
            'invalid': (403, {}), 'expired': (410, {}), 'replayed': (409, {}),
            'unavailable': (503, {}), 'unconsumed': (200, {'valid': True, 'consumed': False}),
            'flag_only': (200, {'approved': True}),
        }
        status, body = responses[failure]
        client._test_handlers['/api/v1/authorizations/verify'] = lambda r: httpx.Response(status, json=body)

    @client.protect()
    def sync_tool():
        invoked.append(True)

    @client.protect()
    async def async_tool():
        invoked.append(True)

    with pytest.raises(GovernanceError):
        asyncio.run(async_tool()) if asynchronous else sync_tool()
    assert invoked == []
    if outcome == 'allow':
        assert not any(r.url.path == '/api/v1/approvals' for r in calls)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_block_stops_before_approval_or_verification(mocked_governance, asynchronous):
    calls = []

    def evaluate(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'outcome': 'deny', 'authorization_receipt': 'untrusted'})

    mocked_governance._test_handlers['/api/v1/oversight/evaluate'] = evaluate
    invoked = []

    @mocked_governance.protect()
    def sync_tool():
        invoked.append(True)

    @mocked_governance.protect()
    async def async_tool():
        invoked.append(True)

    with pytest.raises(GovernanceError):
        asyncio.run(async_tool()) if asynchronous else sync_tool()
    assert invoked == []
    assert calls == ['/api/v1/oversight/evaluate']
