# Backend API contract and current implementation

## Dashboard recorded-evidence API

Dashboard JWTs belonging to active project members may read
`GET /api/v1/projects/{project_id}/evaluations` (bounded limit/offset pagination)
and `GET /api/v1/projects/{project_id}/evaluations/{request_id}`. SDK project keys
cannot use these endpoints. They expose original saved decisions, available
action context and human-review records with explicit field-availability states,
but never signed receipts. A recorded authorization consumption is not evidence
of tool execution. See backend `docs/recorded-evidence-api.md` for the complete
response, historical limitations, and additive schema setup. SDK evaluation and
approval wire contracts remain unchanged.

## Rule evaluator update

Evaluation uses the request project's enabled policy from the backend database.
Active project members manage it with dashboard-authenticated GET/PUT
`/api/v1/projects/{project_id}/policy`; SDK project keys cannot manage policies.
`GOVERNANCE_POLICY_JSON` is not used as a global fallback. Missing, disabled, or
invalid stored policies require human review. See backend
`docs/project-policy-api.md` for setup, migration, and the policy API contract.
Wire outcomes remain `allow`, `approval_required`, `deny`.
Rules may include a strict boolean `enabled` (default true). Disabled rules remain
stored for dashboard editing but are excluded from matching; if no enabled rule
matches, evaluation requires human approval. Changes to enabled state require a
new policy version and do not revoke receipts from previously pinned decisions.
Rules match exact action names and may be scoped to backend-verified project and
agent identities. Precedence is `deny`, then `approval_required`, then `allow`;
within an outcome, narrower identity scope, higher priority, and stable rule ID
determine the winner. Missing, invalid, or non-matching policy requires approval.
Caller reason, metadata, and action parameters do not influence rule matching.
Responses include `risk_level`, `reason_code`, `matched_rule`
(`rule_id`/`effect`/`priority`), and `policy_version`. Decisions are persisted and
reused on idempotent retries. See backend `docs/rule-based-evaluator.md` for the
policy schema and precedence details.

A policy `allow` issues an automatic authorization receipt, persisted atomically
with the evaluation. It does not create a human approval. Both SDK execution paths
must verify and consume this receipt before calling the protected function.

The SDK calls a FastAPI service over HTTP. The routes below are mounted in this workspace's `Backend/app/governance.py`; they are separate from the SDK package. They currently persist approvals in the backend's existing SQLAlchemy database and surface them through the existing dashboard approval list and decision route.

`POST /api/v1/oversight/evaluate` applies the configured rule policy and may return
`allow`, `approval_required`, or `deny`. The SDK only runs a protected function
after the backend verifies and consumes an authorization receipt. `allow` uses an
automatic receipt; `approval_required` waits for human review and verifies its
receipt; `deny` raises `ApprovalRejectedError` without creating an approval.

## Authentication and identity

SDK requests use `Authorization: Bearer <project API key>`. Keys are project-scoped, stored as hashes, and kept separate from agent IDs. The body `project_id` must match the project associated with the key. For every action the backend checks that `agent_id` exists and is assigned to that project. Agent IDs may be registered or provisioned in advance; multiple IDs can belong to one project.

To create a key, sign in to the hosted dashboard and obtain a dashboard access token using `POST /api/v1/auth/login`. A project administrator (a project member with Owner, Admin, Project Admin, or Team Leader role) calls:

```http
POST /api/v1/projects/{project_id}/api-keys
Authorization: Bearer <dashboard access token>
Content-Type: application/json
```

```json
{"name":"Complaint handling production"}
```

The `201` response includes `id`, `project_id`, `name`, `api_key`, and `created_at`. The raw API key is returned only once; store it in an environment variable or secret manager. `GET /api/v1/projects/{project_id}/api-keys` returns metadata and a short prefix, never the secret. Revoke a key with `DELETE /api/v1/projects/{project_id}/api-keys/{key_id}`. Revocation takes effect on subsequent SDK requests. Admin routes use the dashboard JWT; SDK routes use the project API key.

Register an agent with the SDK's `governance.register_agent(name, description, metadata)` (or `aregister_agent`). It sends `POST /api/v1/agents` using the project key and returns the backend-issued `agent_id`. Alternatively, an administrator can provision the agent in advance and configure its ID. Registration requires an existing project and the key's project; the backend persists the association. Agent registration currently accepts but does not implement idempotent retries: avoid retrying a registration after an ambiguous network failure and use a pre-provisioned ID if necessary.

## Shared request and error rules

- API base URL is configured by the SDK. Routes use `/api/v1`.
- Timestamps are timezone-aware RFC 3339 values. Action parameters and metadata must be JSON objects.
- Credential-like fields (for example `api_key`, `token`, `password`, `secret`, or `authorization`) are rejected in action parameters and metadata. Pass a secret reference instead.
- Evaluation and approval creation require `Idempotency-Key` equal to the UUID `request_id`. Reusing a key with different logical action data returns `409`. Timestamp differences are ignored for this comparison.
- The backend returns FastAPI error objects of the form `{"detail":"safe message"}`. It does not intentionally echo API keys or action parameter values. Common statuses: `400/422` invalid input, `401` missing/invalid credentials, `403` project or agent mismatch or a policy denial, `404` missing resource, `409` idempotency conflict/terminal state/replayed receipt, `410` expired receipt, `503` signing not configured, and `5xx` service failure.
- HTTP request timeouts are client-side. Approval expiry is independently enforced by the backend. The SDK fails closed on timeout or unverifiable state.

## Register an agent

`POST /api/v1/agents` with project-key authentication:

```json
{"project_id":"project_123","name":"Support agent","description":"Handles support actions","metadata":{"environment":"prod"}}
```

Returns `201`:

```json
{"agent_id":"agt-...","project_id":"project_123","name":"Support agent","created_at":"2026-01-01T12:00:00+00:00"}
```

Project mismatch returns `403`; missing project returns `404`. The backend checks membership during registration and again for each action. This route shares a path with dashboard agent creation, differentiated by bearer credential type.

## Evaluate a protected action

`POST /api/v1/oversight/evaluate` with project-key auth and `Idempotency-Key`:

```json
{
  "request_id":"uuid",
  "project_id":"project_123",
  "agent_id":"agt-...",
  "agent_name":"Support agent",
  "action_name":"delete_customer",
  "action_parameters":{"customer_id":123},
  "reason":"Customer requested account removal",
  "requested_at":"2026-01-01T12:00:00Z",
  "expires_at":"2026-01-01T12:05:00Z",
  "metadata":{"ticket_id":"SUP-184"}
}
```

Returns `200`:

```json
{
  "outcome":"approval_required",
  "risk_level":"medium",
  "reason":"No applicable valid rule; human review is required.",
  "reason_code":"NO_MATCH",
  "matched_rule":null,
  "policy_version":"default",
  "approval_id":null,
  "authorization_receipt":null
}
```

When a rule matches, `risk_level` and `reason` come from that rule, and
`matched_rule` identifies its ID, effect, and priority. A policy `deny` is returned
as outcome `deny` with HTTP `200`; attempting to create an approval for that
persisted denied evaluation returns `403`. For an idempotent retry after approval
creation, `approval_id` is populated. Evaluation decisions are pinned to the first
request and reused on retries, even if the configured policy changes. Invalid
timestamps, credential-like context, and project/agent mismatches are rejected.
The response schema and policy configuration are documented in backend
`docs/rule-based-evaluator.md`.

## Create an approval

`POST /api/v1/approvals` with the same action body and idempotency key returns `201` with the approval context:

```json
{
  "approval_id":"apr-...","request_id":"uuid","project_id":"project_123",
  "agent_id":"agt-...","agent_name":"Support agent",
  "action_name":"delete_customer","action_parameters":{"customer_id":123},
  "reason":"Customer requested account removal","requested_at":"...Z",
  "submitted_at":"...Z","expires_at":"...Z","metadata":{"ticket_id":"SUP-184"},
  "status":"pending"
}
```

Retries with the same request ID and action return the existing approval; changed action data returns `409`. The backend creates a corresponding row for the existing dashboard approval workflow. The human decision is made through the dashboard's authenticated `POST /api/v1/approvals/{approval_id}/decision` route with `{"decision":"Approved"|"Rejected","comment":"..."}`. Only a reviewer with project membership can decide an SDK approval.

If the persisted evaluation for this request has outcome `deny`, approval creation returns `403`; an existing approval for that request cannot be used to authorize execution either.

Direct/manual approval creation first evaluates and persists the policy decision
if needed. Only `approval_required` may create an approval; `allow` also returns
`403` here and must use its automatic receipt. This prevents one request from
obtaining separate automatic and human authorizations.

## Read, wait, and cancel

- `GET /api/v1/approvals/{approval_id}` returns the full context and `status`. Terminal status also includes `decision` with `status`, `reviewer_id`, `reviewer_comment`, `reviewed_at`, `reason`, and `authorization_receipt` (only for approval).
- `GET /api/v1/approvals/{approval_id}/wait?timeout_seconds=20` is a bounded long-poll endpoint (0–30 seconds) returning the same response shape. SDK versions may instead poll status.
- `POST /api/v1/approvals/{approval_id}/cancel` body: `{"request_id":"uuid"}`. Returns the approval object; repeated cancellation returns the same cancelled state. A terminal approval cannot be cancelled.

All these SDK routes require project-key auth and scope the resource to that project. Pending requests transition to `expired` when their deadline is reached and are reflected in the dashboard approval record.

## Authorization verification and receipt guarantees

The backend signs receipts using `GOVERNANCE_RECEIPT_SECRET`, which must be a deployment-managed random secret of at least 32 characters. Configure it in `Backend/.env` (see `.env.example`); do not commit a real secret. Approval cannot be recorded as approved if signing is unconfigured.

Immediately before protected execution, the SDK calls `POST /api/v1/authorizations/verify` with project key:

```json
{
  "request_id":"uuid","approval_id":"apr-...",
  "authorization_receipt":"opaque-signed-receipt","project_id":"project_123",
  "agent_id":"agt-...","action_name":"delete_customer",
  "action_parameters":{"customer_id":123}
}
```

For automatic receipts, omit `approval_id` or send `null`. For human receipts,
provide the approved review ID as above. Only matching, unexpired, server-issued
authorization is consumed. Success is `200`:

```json
{"valid":true,"consumed":true,"expires_at":"...Z"}
```

The receipt binds approval ID, request ID, project, agent, action name, canonical parameter hash, and expiry. Verification inserts a unique receipt-use record; a second or concurrent consume returns `409`. Expired receipts return `410`; signature/action mismatch returns `403`. This is enforced transactionally by the database uniqueness constraint across backend workers. Do not treat a client-side `approved` flag as permission to execute.

Automatic receipts instead bind the automatic authorization ID (`sub`),
`kind: automatic`, request ID, project, agent, action name, canonical parameter
hash (`action_hash`), policy version, expiry (`exp`), and a random unique `jti`.
Canonical parameters use UTF-8 JSON with sorted object keys, compact separators,
unescaped Unicode, and finite numbers only. The verifier checks the authenticated
project and current agent membership, signature, expiry, every action binding,
the persisted authorization row, and the original persisted ALLOW evaluation and
receipt. The policy version comes from that evaluation, never the client.

Idempotent evaluation retries return the original receipt even after consumption
or policy changes; they do not mint new authority or extend expiry. A legacy ALLOW
evaluation without a receipt remains blocked. Missing, invalid, expired, replayed,
or unverifiable receipts, service errors, and responses without literal
`valid: true` and `consumed: true` block SDK execution. The SDK also checks the
returned expiry. Receipt consumption is deliberately not retryable: after an
ambiguous verification failure or a tool failure after consumption, execution
stays blocked for that request. This provides at-most-once authorization, not a
transaction spanning the backend and the external tool.

## Deployment and limitations

The backend requires `GOVERNANCE_RECEIPT_SECRET` to mint human and automatic
receipts. An ALLOW evaluation fails with `503` and persists no new decision if
signing is unconfigured. Use a durable shared database for multi-worker
deployments; SQLite is suitable only for local/single-instance development.
Startup creates the additive `automatic_authorizations` table; deploy this schema
before routing traffic to the new code. Existing human receipts and approvals
remain valid under their original rules. Automatic agent disablement, dashboard
approval filtering by project, and formal SDK-agent-registration idempotency are
not implemented. Coordinate rollout rather than running old and new backend
versions against the same database during schema changes.
