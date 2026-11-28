# Governance API backend

This FastAPI service implements the HTTP API used by the separate Human-in-the-Loop Python SDK. It is an API service only; it does not add a policy/risk engine or frontend.

## Local setup

From this directory, create a virtual environment, install `requirements.txt`, copy `.env.example` to `.env`, then set `JWT_SECRET` and `GOVERNANCE_RECEIPT_SECRET` to deployment-managed random values. `GOVERNANCE_RECEIPT_SECRET` must be at least 32 characters and must remain stable across all backend workers. Keep `.env` out of version control.

Run the service with:

```powershell
python -m uvicorn app.main:app --reload
```

The default local database is SQLite in this directory. Use a durable shared database for multi-worker or production deployments. Existing SQLite databases are updated with project ownership columns for seeded agents; back up production data before applying schema changes.

## Provisioning the SDK

1. Sign in through the existing dashboard and obtain a dashboard access token.
2. Have a project administrator create a project API key with `POST /api/v1/projects/{project_id}/api-keys`.
3. Store the returned API key once in a secret manager or environment variable. Listing keys only returns metadata and a prefix; it cannot recover the secret.
4. Register each agent once with project-key `POST /api/v1/agents`, or use an existing agent ID assigned to that project.
5. Configure `GOVERNANCE_API_KEY`, `GOVERNANCE_PROJECT_ID`, `GOVERNANCE_AGENT_ID`, and `GOVERNANCE_BASE_URL` in the agent application.

The complete request/response contract, permissions, status codes, idempotency rules, and current limitations are in the SDK's `docs/api-contract.md`.

## Current behavior

The rule evaluator returns `allow`, `approval_required`, or `deny`. ALLOW issues a
persisted, signed, expiring automatic receipt without creating an approval. Human
review records reviewer details and issues the existing approval receipt. Both
receipts must be verified and atomically consumed once before SDK execution.
BLOCK stops execution and cannot create an approval. Missing or invalid policy
defaults to human review. See `docs/rule-based-evaluator.md` for configuration.
Policies are stored per project and managed through authenticated GET/PUT
`/api/v1/projects/{project_id}/policy`. See
[project policy setup and migration](docs/project-policy-api.md). HTTP evaluation
does not use `GOVERNANCE_POLICY_JSON` as a fallback.
Startup creates the additive `automatic_authorizations` table. Avoid treating
dashboard state or any client-supplied decision or approval flag as authorization.
