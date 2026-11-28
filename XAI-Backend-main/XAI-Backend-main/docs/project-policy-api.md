# Project policy API and storage

Each project has at most one policy in `project_policies`. Its project ID is the
primary key and a foreign key to `projects.id`. The row contains a version,
enabled boolean, validated rules serialized as JSON, and UTC creation/update
timestamps. Policy changes require a different version. Use a fresh version for
every change, including enabling or disabling a policy. This table stores the
current policy, not a version archive; evaluation records retain the original
decision and version for idempotent retries and receipt verification.

## Authentication and isolation

Both endpoints require a dashboard user JWT, obtained through
`POST /api/v1/auth/login`. Project SDK API keys cannot manage policies. The caller
must have an active team membership containing the exact route project ID. Any
active project member may read and replace that project's policy. The existing
membership data uses comma-separated project IDs and the member's account email.
Nonmembers and inactive members receive `403`; missing/invalid credentials receive
`401`. Membership is checked before looking up project or policy data.

`GET /api/v1/projects/{project_id}/policy` returns the policy or `404` when none
has been configured. `PUT` at the same URL creates or completely replaces it:

```json
{
  "version": "v1",
  "enabled": true,
  "rules": [
    {
      "rule_id": "read-profile",
      "action_name": "read_profile",
      "outcome": "allow",
      "risk_level": "low",
      "reason": "Approved read operation",
      "priority": 10
    }
  ]
}
```

PUT returns `200` for both creation and replacement. The response adds
`project_id`, `created_at`, and `updated_at` (RFC 3339 UTC). Repeating an identical
PUT preserves timestamps. Changing rules or enabled state without changing the
version returns `409`. Concurrent inserts or updates detected during a transaction
also return `409`; fetch the current policy and retry deliberately. This is not an
HTTP ETag protocol: sequential replacements with different versions are accepted.

The existing rule schema and precedence are defined in
`rule-based-evaluator.md`. Unknown fields, invalid outcome/risk values, noninteger
priorities, blank identifiers/actions/reasons, and duplicate rule IDs return
`422`. Version is a required nonblank string of at most 120 characters; enabled
defaults to true and must be a JSON boolean when supplied. Empty rules are valid
and conservatively require human review. Optional rule `project_id` must equal
the route project; optional `agent_id` must belong to that project. The project
cannot be selected or changed through a body field. Invalid requests do not
change stored policies.

## Evaluation behavior

Evaluation authenticates the SDK key, checks project/agent membership, then loads
only that project's stored enabled policy. Missing, disabled, malformed, or
non-matching policies return `approval_required` with medium fallback risk and no
automatic receipt. A missing policy reports version `default`; corrupt data
reports `invalid`; a disabled policy retains its configured version. Database
errors fail the request and never grant ALLOW. `GOVERNANCE_POLICY_JSON` is no
longer used by HTTP evaluation; there is no global policy fallback or automatic
import. Configure each project explicitly through PUT.

Existing requests keep their original persisted decision and receipt across
policy updates. Changes affect new request IDs. Disabling or replacing a policy
does not revoke previously issued receipts; those remain bound to the original
decision, expiry, and single-use consumption rules.

## Migration and local setup

1. Back up the database and configure `DATABASE_URL` for the intended database.
   Relative SQLite URLs resolve from the working directory, so run setup and the
   server from the same backend directory. Without an override, the backend uses
   `industry_software.db` in that directory.
2. Activate a Python environment and install `requirements.txt`. Copy
   `.env.example` to `.env` and set deployment-managed `JWT_SECRET` and
   `GOVERNANCE_RECEIPT_SECRET` (at least 32 characters for receipt signing).
3. Apply the additive schema before serving traffic. This project uses
   SQLAlchemy `Base.metadata.create_all`, not Alembic. Normal application startup
   creates the new table. To create missing tables without starting the server or
   running demo-user seeding, run from the backend directory:

   ```powershell
   python -c "from app.main import Base, engine; Base.metadata.create_all(engine)"
   ```

   The operation is repeatable and preserves existing rows. No existing column
   alteration or policy backfill is required. Existing projects start with the
   conservative fallback until a policy is configured. The existing startup
   migration for agent project ownership remains in place. Production database
   credentials need table/index creation privileges for this schema step; use a
   coordinated deployment and a durable shared database for multiple workers.
4. Start `python -m uvicorn app.main:app --reload`, sign in as an active project
   member, then PUT the project's policy. A policy ALLOW also requires configured
   receipt signing before evaluation can succeed. No frontend changes are needed
   to use these endpoints through an HTTP client or `/docs`.

For tests, install pytest and httpx plus the SDK's dependencies in the test
environment, keep the sibling SDK source checkout available for the integration
tests, and run `python -m pytest tests -q -p no:cacheprovider` from the backend
directory. Tests use isolated databases and do not require real credentials.
