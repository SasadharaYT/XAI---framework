# Recorded evaluation evidence

These read-only dashboard endpoints expose persisted evidence, not model reasoning
or tool execution traces. They require a dashboard JWT and active membership of
the exact route project. SDK project API keys cannot read dashboard evidence.
Membership is checked on every request before resource lookup. Cross-project
lookups never return another project's evaluation, context, review, or receipt use.

## Endpoints

`GET /api/v1/projects/{project_id}/evaluations?limit=25&offset=0` returns:

```json
{
  "project_id": "project-1",
  "items": [],
  "total": 0,
  "limit": 25,
  "offset": 0
}
```

Default limit is 50; valid limits are 1–100 and offsets are nonnegative. Rows are
ordered by stored evaluation creation timestamp descending, then request ID
descending. Counts refer to this project's stored evaluations, not completed
executions or verified reports. Offset pages reflect the database at read time;
new evaluations can shift subsequent pages. The UI labels page-local results and
uses explicit previous/next controls.

`GET /api/v1/projects/{project_id}/evaluations/{request_id}` returns one evidence
object. A missing evaluation, including an ID belonging to a different project,
returns `404`. Missing/invalid user authentication returns `401`, inactive or
nonmember access returns `403`, and invalid pagination returns `422`. No records
returns an empty successful list. Service/database failures are errors, not empty
evidence or an ALLOW decision.

## Evidence object

- `request_id`, `project_id`, `evaluated_at`: persisted evaluation identity and
  backend creation time (RFC 3339 UTC).
- `evaluation`: stored `outcome`, `risk_level`, `reason`, `reason_code`,
  `matched_rule` (`rule_id`, `effect`, `priority`), and `policy_version`. These are
  taken from the original saved decision, never recomputed using today's policy.
  Risk is a rule classification or conservative fallback label, not a measured
  probability or confidence score. The reason is the evaluator's recorded
  explanation, not private model reasoning.
- `evaluation.field_states`: each projected field is `recorded`, `recorded_null`,
  `not_recorded`, or `invalid`. Explicit JSON null and an absent legacy field are
  different. A null matched rule with the recorded `NO_MATCH` reason code is a
  stored no-match result; absence alone does not establish that a check occurred.
- `evaluation.record_state`: `recorded` for readable object JSON, `unreadable`
  for malformed/nonobject saved results. Unreadable results have no inferred
  decision. Invalid projected fields become null with state `invalid`.
- `context` and `context_source`: a saved evaluation snapshot, an associated
  approval record, an automatic authorization record, or null when unavailable.
  Context may contain agent identity, action name, submitted parameters, caller
  reason/metadata, request timestamp and expiry where actually stored. Legacy
  automatic records lack caller reason, metadata and original request timestamp;
  those fields are omitted rather than filled from a later retry or live agent.
  Unreadable context JSON is unavailable, not evidence that external facts were
  checked. Caller context and evaluator reasons are displayed separately.
- `human_review`: associated approval ID, stored status, submission/deadline,
  reviewer ID/comment and review timestamp, or null if no review row exists.
  Reading evidence does not expire pending approvals, manufacture a review event,
  sign a human receipt, or convert a missing review into a rejection/approval.
  A pending stored status can outlive its deadline until the approval flow records
  a transition. Dashboard-only legacy approvals without a request/evaluation link
  are not guessed into an evaluation's evidence.
- `authorization_consumptions`: stored receipt consumption timestamps. This
  proves only that the authorization verifier consumed a receipt, not that a
  protected tool ran, succeeded, or produced a result.
- `execution_outcome` and `model_reasoning`: null, because these sources do not
  record those facts. `integrity_status` is `not_verified`; database presence is
  not an independent integrity check. These fields express evidence limitations,
  not fabricated outcomes.

The projection never returns signed authorization receipts, receipt IDs (`jti`),
API keys, body fingerprints, or unrecognized raw evaluation fields. Nested
credential-like keys in action parameters/metadata are defensively redacted.
This cannot identify arbitrary secrets pasted into free text; callers and
reviewers must not include secrets in reasons, comments, or metadata. JSON export
uses this same safe projection, with no generated events or invented report IDs.
Exports are local snapshots and are not signed audit certificates.

## Storage and deployment

The additive `evaluation_contexts` table stores request ID (primary key), project
ID (indexed), and the original validated request as JSON. New evaluations write
their decision and context in the same transaction, including denials. Duplicate
request IDs reuse the original decision and do not overwrite or retroactively
populate evidence. Failed signing/validation/transactions leave no new snapshot.

Back up the database and deploy the schema before serving the new routes. As with
the existing project migrations, application startup uses SQLAlchemy
`Base.metadata.create_all`; there is no new ALTER of existing tables or fabricated
historical backfill. From the backend directory, using the intended environment
and `DATABASE_URL`, the additive schema can also be applied without starting the
server or seeding demo users:

```powershell
python -c "from app.main import Base, engine; Base.metadata.create_all(engine)"
```

Use a durable shared database in multiworker deployments. Coordinate backend and
frontend rollout; an older backend missing the evidence routes produces an error
state in the dashboard. Existing evaluations remain readable with explicitly
limited context. No SDK request or evaluation response contract changes.

Backend tests cover stored outcomes/context, legacy gaps, malformed results,
project isolation, pagination, reviews without signing, redaction and the
distinction between consumption and execution. Frontend tests cover both views,
missing versus explicit no-match, loading/error/empty states, paging, project
changes, refresh, and JSON export. The existing real-backend HTTP fixture also
tests the evidence adapter against newly recorded evaluations.
