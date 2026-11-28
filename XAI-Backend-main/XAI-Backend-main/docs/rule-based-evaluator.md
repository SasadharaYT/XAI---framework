# Rule-based evaluation contract

POST /api/v1/oversight/evaluate preserves the request, authentication, project/agent
checks, timestamp validation and Idempotency-Key contract. API validation failures
remain HTTP errors. A valid policy BLOCK returns HTTP 200 with outcome `deny`.

The project's enabled database policy configures HTTP evaluation. Active project
members manage it with `GET`/`PUT /api/v1/projects/{project_id}/policy`; see
`project-policy-api.md` for authentication, validation, migration, and setup.
`GOVERNANCE_POLICY_JSON` is no longer a global fallback for HTTP requests.
Example policy body:

```json
{"version":"v1","rules":[{"rule_id":"read-policy","action_name":"read_profile","project_id":"project-1","outcome":"allow","risk_level":"low","reason":"Approved read operation","priority":10}]}
```

Rules require unique IDs, exact action names, outcome, risk level and explanation.
Each rule's optional boolean `enabled` defaults to true. Disabled rules stay in
storage for later re-enabling, but are excluded before matching and precedence.
If no enabled rule matches, evaluation requires human approval.
Optional project_id and agent_id scope rules to identities verified by the backend.
Unknown fields or malformed rules are rejected by the policy API. Missing,
disabled, or invalid stored configuration and unknown actions require approval. There are no automatic
action-name heuristics. Caller reason and metadata never influence matching.
Parameters describe the action but are not verified external facts and are not
matched in this first version. Allow rules therefore authorize all parameter values
for the named action; only register such rules for operations safe at that scope.

Precedence is deny > approval_required > allow, then number of identity constraints
(descending), priority (descending), rule_id (ascending). Risk is an explicit rule
classification: low/medium/high/critical. Fallback risk is medium, a conservative
default label, not a measured score.

Responses retain outcome/reason/approval_id/authorization_receipt and add
risk_level, reason_code, matched_rule (rule_id, effect, priority), policy_version.
The original decision JSON is persisted. Retries return that decision even after
policy changes; an approval_id may be added once human review is created.
Change policy version whenever configuration changes. For `allow`, the backend
atomically persists an automatic authorization and the evaluation, returning a
signed single-use `authorization_receipt` with `approval_id: null`. The SDK must
verify and consume it before execution. `deny` stops execution without approval;
`approval_required` uses the existing human-review receipt flow. Direct approval
creation also evaluates policy and accepts only `approval_required`.

Automatic receipts bind request, project, agent, exact action, canonical parameter
hash, policy version, original expiry, and a random one-time identifier. The
verifier requires the matching persisted ALLOW evaluation and authorization row;
client decision flags never grant authority. Retries return the original receipt,
including after consumption or policy changes, and never renew its expiry.
Legacy ALLOW evaluations with no receipt remain non-executable; use a new request
ID for a new logical attempt. Policy archive management remains separate work.

Upgrade SDK response models before enabling the expanded API response: older
SDK releases reject unknown fields. New models still accept legacy responses.
