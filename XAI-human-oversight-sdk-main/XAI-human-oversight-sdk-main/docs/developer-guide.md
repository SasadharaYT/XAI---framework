# Developer integration guide

This guide adds approval gates to an existing Python agent application. It assumes your agent already calls ordinary Python functions for tools. The SDK wraps those functions; it does not run the agent, provide tools, or execute manually approved actions.

## 1. Prerequisites and installation

- Python 3.11 or newer.
- A governance project and API key from the hosted governance service.
- A running governance backend with the routes described in [api-contract.md](api-contract.md). The companion FastAPI service in this workspace implements them; the SDK does not start the service.
- One registered agent ID for each agent identity that will make governed calls.

For local development, install from the package directory:

```bash
python -m venv .venv
# Activate the environment using your platform's command.
python -m pip install -e ".[dev]"
```

From the SDK root, install it with `pip install -e .`. If the SDK is checked out elsewhere, point your application’s dependency configuration at that directory. Before publishing, pin an approved package version in your normal dependency lockfile.

## 2. Configure project credentials

Store `GOVERNANCE_API_KEY` in a secret manager in deployed environments. The project ID and agent IDs are identifiers, not authentication secrets. Keep environment-specific values outside source control.

```dotenv
GOVERNANCE_API_KEY=...
GOVERNANCE_PROJECT_ID=project_123
GOVERNANCE_BASE_URL=https://governance.example.com
GOVERNANCE_AGENT_ID=agent_support
```

Initialize one client for the project. Never put the key in action parameters, metadata, source code, or logs.

```python
import os
from governance_sdk import Governance

governance = Governance(
    api_key=os.environ["GOVERNANCE_API_KEY"],
    project_id=os.environ["GOVERNANCE_PROJECT_ID"],
    base_url=os.environ["GOVERNANCE_BASE_URL"],
    agent_id=os.getenv("GOVERNANCE_AGENT_ID"),  # optional default identity
    timeout=10.0,             # per HTTP request
    approval_timeout=300.0,   # complete approval lifecycle
    poll_interval=2.0,
    max_poll_interval=10.0,
)
```

Use `with Governance(...) as governance:` for sync applications. In async applications, use `async with Governance(...) as governance:`. The same client can serve multiple agents in one project; pass the agent identity at each decorator or request when agents differ.

## 3. Register and configure agents

Register an agent during your deployment/provisioning workflow, not each agent turn. Persist the returned ID in your application configuration or secret/config store. An operator can alternatively provision an agent in the hosted dashboard and copy its ID. To create SDK API keys, a project administrator signs into the dashboard and calls `POST /api/v1/projects/{project_id}/api-keys`; setup and permission details are in [api-contract.md](api-contract.md). The raw key is shown once.

```python
agent_id = governance.register_agent(
    name="Customer support agent",
    description="Handles support triage and approved account operations",
    metadata={"environment": "production"},
)
print(agent_id)  # store as configuration; it is not a credential
```

The backend binds the new agent to the authenticated project and validates membership on every action. Registration is not idempotent yet, so run it once during provisioning and retain the ID; do not automatically retry an ambiguous registration. Do not reuse an agent ID from another project. `agent_name` is optional display context and does not replace `agent_id`.

For a project with several agents, choose the ID from the actual calling agent:

```python
@governance.protect(agent_id="agent_billing", reason="Agent initiated a refund")
def issue_refund(order_id: str, amount_cents: int) -> str:
    return payments.refund(order_id, amount_cents)
```

If an ID cannot be resolved from the decorator, request, or client default, the SDK rejects the action before contacting the backend.

## 4. Protect an existing tool with minimal changes

Start with an ordinary tool function. Add the decorator and a reason that will help the human reviewer. The current backend routes each action to review, and the function body runs only after human approval and backend receipt verification.

```python
@governance.protect(
    agent_id="agent_support",
    reason="Support agent requested account deletion",
    metadata={"source": "support-ticket"},
)
def delete_customer(customer_id: int) -> dict[str, object]:
    return customer_api.delete(customer_id)

result = delete_customer(customer_id=123)
```

The SDK uses the decorated function name and bound arguments as the action context. Use JSON-compatible parameters. For functions that take service clients or other non-JSON objects, keep those objects outside the function signature (for example, close over them) so they are not serialized as action parameters.

For retries initiated by your application, pass an idempotency-key factory. It must return the same UUID for another attempt of the same logical operation and a new UUID for a distinct operation. Derive it from a durable operation ID rather than only the business entity ID. The SDK uses the returned value as `request_id` and the backend `Idempotency-Key` for evaluation and approval creation.

```python
@governance.protect(
    agent_id="agent_billing",
    idempotency_key=lambda operation_id, **_: operation_id,  # operation_id is a UUID
)
def issue_refund(operation_id: str, order_id: str, amount_cents: int) -> None:
    payments.refund(order_id, amount_cents)
```

The process-local execution guard rejects a second use of the same key. Backend idempotency and atomic receipt consumption are still required across processes. For protected calls, `on_decision` receives reviewer details after receipt verification and before the function runs:

```python
@governance.protect(on_decision=lambda decision: audit(decision.reviewer_comment))
def delete_customer(customer_id: int) -> None:
    customer_api.delete(customer_id)
```

Handle governance failures at your agent/tool boundary. A blocked operation must not be treated as a successful tool result:

```python
from governance_sdk import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    BackendUnavailableError,
    GovernanceError,
)

try:
    result = delete_customer(customer_id=123)
except ApprovalRejectedError as exc:
    audit_tool_block("rejected", reviewer_comment=exc.comment)
except (ApprovalExpiredError, ApprovalTimeoutError):
    audit_tool_block("not_decided")
except BackendUnavailableError:
    # Keep the agent from invoking an alternate unprotected path.
    audit_tool_block("governance_unavailable")
    raise
except GovernanceError:
    audit_tool_block("governance_error")
    raise
```

Do not catch an approval exception and then call the underlying function directly. If the service fails, fail closed.

## 5. Manual request: application owns execution

Use the manual interface when your application needs to decide what to do after approval. The SDK submits and waits; it never invokes your business function in this path.

```python
request = governance.request_approval(
    agent_id="agent_support",
    action="delete_customer",
    parameters={"customer_id": 123},
    reason="Customer requested account removal",
    metadata={"ticket_id": "SUP-184"},
    timeout=180,
)
decision = request.wait()

if decision.approved:
    print(f"Reviewed by {decision.reviewer_id}: {decision.reviewer_comment}")
    # Application code explicitly executes its own action here.
    customer_api.delete(123)
else:
    print(f"Request {decision.status}; reviewer comment: {decision.reviewer_comment}")
```

Approval decisions returned by the handle have had their receipt verified. Your application is responsible for ensuring the manually executed operation still uses the exact parameters that were approved. Prefer `protect()` when you need the SDK to bind and gate the function call itself.

## 6. Async integration

Decorate `async def` functions to keep both HTTP operations and approval polling off the event loop's blocking path:

```python
@governance.protect(agent_id="agent_billing", timeout=240)
async def refund_order(order_id: str, amount_cents: int) -> None:
    await payment_client.refund(order_id, amount_cents)

await refund_order("ord_42", 2500)
```

For manual asynchronous approvals, request creation has an async equivalent and the returned handle exposes `await_decision()`:

```python
request = await governance.arequest_approval(
    agent_id="agent_billing",
    action="refund_order",
    parameters={"order_id": "ord_42", "amount_cents": 2500},
    reason="Customer reported a duplicate charge",
)
decision = await request.await_decision(timeout=240)
if decision.approved:
    await payment_client.refund("ord_42", 2500)
```

## 7. Timeouts and cancellation

`timeout` on a protected decorator/request bounds the approval lifecycle. The client also has a default `approval_timeout`. Each HTTP operation has its own `timeout`. If approval remains pending at the deadline, a typed timeout is raised and a protected function is not called.

Cancel explicitly when your workflow no longer needs the review:

```python
request = governance.request_approval(
    agent_id="agent_support", action="delete_customer",
    parameters={"customer_id": 123}, reason="Requested by the customer",
)
request.cancel()
```

Async cancellation can use `await request.acancel()`. Cancelling a Python task while awaiting does not silently authorize the action. Applications should cancel the backend request if its lifecycle should end; the contract defines this operation.

## 8. Safe approval context

Include enough context for a reviewer to understand the operation: clear action names, a reason, safe parameters, and non-sensitive metadata such as ticket IDs. Never send API keys, passwords, tokens, private keys, or credential values. The SDK rejects common credential-like field names recursively, but it cannot detect secrets stored under arbitrary names. Pass a reference (such as a vault record ID) and resolve the secret only inside the protected function.

Do not place unnecessary personal or regulated data in metadata. The human review screen should show the agent ID/name where available, action, parameters, reason, request time, expiry, and optional safe context. The returned decision includes reviewer identity/comment when the backend exposes them.

## 9. Local testing without a backend

Inject mocked HTTPX transports in unit tests or replace the client's private transport in a fixture. Keep tests deterministic by returning explicit evaluation outcomes, approval states, and receipt verification responses. For an approved path, the mock verification response must say both `valid: true` and `consumed: true`; a bare approved decision is not enough. Never use a real API key in tests.

Run the SDK tests from its directory:

```bash
pytest
ruff check .
ruff format --check .
```

## 10. Production checklist

- Confirm the deployed backend implements every operation in [api-contract.md](api-contract.md), especially API-key auth, project-agent membership validation, idempotency, receipt binding, expiry, and atomic single-use consumption.
- Load API keys from a secret manager and rotate them through the provider's normal process.
- Set bounded network and approval timeouts; handle governance exceptions as blocked actions.
- Ensure all call paths for a sensitive tool use the protected wrapper; do not retain an unguarded alternate route.
- Use downstream idempotency keys for side-effecting tools where available. SDK/backend guarantees provide at-most-once authorization; they cannot guarantee a business operation completed after a process crash.
- Keep the SDK and API contract versions aligned. The backend contract is implemented by the companion FastAPI service; confirm its deployed version and database configuration before enabling protected tools.

