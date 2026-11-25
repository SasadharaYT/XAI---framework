# Rule-based evaluation contract

POST /api/v1/oversight/evaluate preserves the request, authentication, project/agent
checks, timestamp validation and Idempotency-Key contract. API validation failures
remain HTTP errors. A valid policy BLOCK returns HTTP 200 with outcome `deny`.

Deployment-owned GOVERNANCE_POLICY_JSON configures the evaluator:

```json
{"version":"v1","rules":[{"rule_id":"read-policy","action_name":"read_profile","project_id":"project-1","outcome":"allow","risk_level":"low","reason":"Approved read operation","priority":10}]}
```

Rules require unique IDs, exact action names, outcome, risk level and explanation.
Optional project_id and agent_id scope rules to identities verified by the backend.
Unknown fields or malformed rules invalidate the entire configuration. Missing or
invalid configuration and unknown actions require approval. There are no automatic
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
Change policy version whenever configuration changes. Policy archive management
and automatic authorization issuance are separate features. This branch returns
null authorization_receipt even for allow: the existing SDK fails closed without
a verified receipt. A policy allow is not itself execution authorization.

Upgrade SDK response models before enabling the expanded API response: older
SDK releases reject unknown fields. New models still accept legacy responses.
