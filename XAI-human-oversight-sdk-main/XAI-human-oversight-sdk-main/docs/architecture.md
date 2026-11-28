# Architecture

## Boundaries

`Governance` owns validated project configuration, HTTP client lifecycle, and the public API. `ApprovalManager` builds action context and coordinates evaluation and approval creation. `ApprovalHandler` polls final state and verifies receipts. `ExecutionInterceptor` gates protected tool execution. Decorators capture function arguments. `GovernanceHTTPClient` is the only module that performs HTTP calls. Pydantic models validate SDK inputs and backend responses.

The SDK is independent of FastAPI implementation and dashboard code. The contract is documented in `api-contract.md`. The companion backend in this workspace currently implements the documented HITL routes; it remains a separate deployable service.

## Request lifecycle

1. A protected call captures its action name, JSON-compatible parameters, project and agent identity, reason, request ID, timestamp, expiry, and metadata.
2. Credential-like field names in parameters and metadata are rejected before any request is sent.
3. The SDK posts the action to evaluation with the request ID as the idempotency key.
4. `deny` blocks without creating an approval. `allow` provides a persisted,
   single-use automatic receipt bound to the action and policy version.
   `approval_required` creates or attaches to a human approval using the same
   request ID as its idempotency key. Both execution paths verify and consume
   their receipt on the backend before invoking the protected function.
5. The SDK polls approval status. Pending requests do not invoke the function. For automatic allow or approved status, it sends the receipt and original context to the verification endpoint.
6. Only a positive, consumed verification response permits the protected function to run. Rejection, expiry, cancellation, timeout, malformed response, or any backend error blocks it.

## Identity and multiple agents

An API key authenticates the project; `agent_id` identifies the agent making a particular action. They are separate values. A client may hold a default agent ID, and each decorator or manual request may override it. Every protected request must contain one explicit resolved agent ID. The backend must validate that the agent belongs to the authenticated project for each operation. Optional agent names are descriptive context, not identity proof.

## Idempotency and execution

Each logical request gets a UUID request ID. Evaluation and approval creation use that ID as `Idempotency-Key`, including transport retries. The backend returns the same evaluation/approval for repeated keys and rejects key reuse with changed action data. Its single-use receipt endpoint binds a receipt to the request ID, project, agent, action name, and canonical action parameters. The SDK does not mutate parameters between evaluation, approval, verification, and execution. Agent registration itself is not currently idempotent in the backend.

The SDK claims request IDs once per client process. The backend must serialize claims across processes and workers. This provides at-most-once authorization use, not guaranteed completion: a process crash after consumption can leave the protected operation unexecuted. Downstream tools should accept their own idempotency key for side-effecting actions when available.

## Wait strategy

Polling is the initial strategy. `PollingWaitStrategy` uses bounded increasing intervals and sync or async sleep. The public request handle remains independent of transport details, leaving room for server events later without changing caller APIs.

