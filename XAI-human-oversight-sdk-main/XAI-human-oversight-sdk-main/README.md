# Human-in-the-Loop Governance Python SDK

Python SDK for adding human approval gates to existing agent tools. The SDK communicates with a separately hosted governance API; it does not include a dashboard, backend, database, model, risk scorer, or policy engine.

## Install

Python 3.11 or later is required.

```bash
pip install -e .
```

For local development and tests:

```bash
pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
```

## Configure credentials and identity

Create a project and project API key in the governance dashboard/backend. See [the backend API contract](docs/api-contract.md) for the key issuance endpoint, permissions, one-time secret display, and revocation. Register each agent using `register_agent()` or use an agent ID provisioned for that project. Keep the API key in a secret manager or environment variable; agent IDs are identifiers, not credentials. The backend verifies project membership for each action.

```python
import os
from governance_sdk import Governance

governance = Governance(
    api_key=os.environ["GOVERNANCE_API_KEY"],
    project_id=os.environ["GOVERNANCE_PROJECT_ID"],
    agent_id=os.environ.get("GOVERNANCE_AGENT_ID"),  # optional project default
    base_url=os.environ["GOVERNANCE_BASE_URL"],
    timeout=10,
    approval_timeout=300,
    poll_interval=2,
)
```

An agent-specific `agent_id=` can override the default on a decorator or approval request. This lets one SDK client serve multiple agents in the same project. The backend must confirm project membership on every action.

## Protect an existing tool

```python
@governance.protect(agent_id="agent_001", reason="Agent requested a customer deletion")
def delete_customer(customer_id: int) -> str:
    # This body does not run until backend authorization succeeds.
    return f"Deleted customer {customer_id}"

result = delete_customer(customer_id=123)
```

The SDK submits the function name and JSON-compatible arguments. The current backend routes every action to human approval; an evaluator that returns automatic allow or deny is not implemented yet. A human approval requires a one-time authorization receipt verified by the backend before the function runs. Rejection, expiry, cancellation, timeout, backend failure, or failed receipt verification raises a typed exception and does not call the function.

For application-level retries, provide an idempotency-key factory that returns the same UUID for the same logical operation and a new UUID for a different operation. The SDK uses it as the request ID for evaluation and approval creation. Use `on_decision` when protected callers need the reviewer comment; the callback runs after verification and before the tool body.

## Manual approval

Manual requests only create and wait for an approval. Your application stays responsible for carrying out the action after the verified decision.

```python
request = governance.request_approval(
    agent_id="agent_001",
    action="delete_customer",
    parameters={"customer_id": 123},
    reason="Customer deletion requested by the support agent",
    metadata={"ticket_id": "T-84"},
)
decision = request.wait(timeout=180)
if decision.approved:
    print("Approved by", decision.reviewer_id, decision.reviewer_comment)
    # The application explicitly performs its own action here.
else:
    print("Not approved:", decision.status, decision.reviewer_comment)
```

## Async use

```python
@governance.protect(agent_id="agent_001")
async def issue_refund(order_id: str, amount: int) -> None:
    await payment_service.refund(order_id, amount)

await issue_refund("ord_42", 2500)

request = await governance.arequest_approval(
    agent_id="agent_001", action="issue_refund",
    parameters={"order_id": "ord_42", "amount": 2500},
    reason="Refund requested by the customer",
)
decision = await request.await_decision(timeout=180)
```

Async waiting uses `asyncio.sleep` and async HTTPX. Use `async with Governance(...)` or `await governance.aclose()` when finished. Sync clients support `with Governance(...)` or `governance.close()`.

## Integration guide and backend contract

See [the end-to-end developer guide](docs/developer-guide.md), [backend API contract and setup](docs/api-contract.md), [architecture](docs/architecture.md), and [security guide](docs/security.md). The API contract is implemented by the separate FastAPI service in the sibling `Backend` directory. The SDK does not contain or start that service.

## Safety notes

- Never put API keys, passwords, access tokens, or other credentials in action parameters or metadata. Credential-like field names are rejected; pass secret references and resolve secrets only inside the protected function.
- Authorization receipts must be bound to the exact request, project, agent, action, and parameters. The backend must atomically enforce expiry and single-use consumption.
- A failed authorization check always blocks execution. SDK-level duplicate claims protect one process; cross-process prevention requires the backend guarantee.
- Approval decisions should be treated as data until receipt verification succeeds.

