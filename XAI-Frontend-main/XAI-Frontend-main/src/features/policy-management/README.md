# Project policy dashboard

The authenticated dashboard route `/projects/:projectId/policy` uses the route
project ID and shared Axios client. The existing authentication provider attaches
the dashboard bearer token. The page uses no demo project policy or local-storage
policy data. Project changes remount the editor and use separate query keys.

The adapter calls the restored `GET` and `PUT
/api/v1/projects/{project_id}/policy` endpoints. Policies are stored per project
and evaluation reads that project's enabled policy. A missing API or failed
authorization displays an error and cannot open an editor. Only the explicit
backend detail `Project policy not configured` opens an empty creation form.

The payload contains version, policy-level enabled state, and the existing rule
fields: rule ID, exact action name, decision, risk level, reason, priority, and
optional project/agent scope. Existing scopes are preserved. Project scopes must
match the current project; agent membership and all authorization remain backend
responsibilities. A new version is required to save. Policy responses for a
different project or unsupported schemas block editing rather than silently
dropping fields. Save success is shown only after a validated backend response.

Rule toggles save the backend-validated boolean `enabled` on each rule. Disabled
rules remain stored and editable, survive reload, and can be enabled again later.
The evaluator excludes disabled rules from matching and precedence. If no enabled
rule matches, the action requires human review. Older records without an enabled
flag default to true; no database column migration is needed for this JSON field.
Disabling or enabling a rule requires a new policy version, just like other edits.

Frontend validation improves feedback but does not authorize policy changes.
HTTP 401, 403, 404, 409, 422, and service failures have explicit error states.
Failed saves retain the draft. Reload deliberately replaces it with the current
backend policy. Existing authorization receipts and evaluations are unaffected.

Tests spy on the shared HTTP client to cover loading, editing, toggling, payloads,
validation, version conflicts, API errors, project changes, and response isolation.
Run `npm run test`, `npm run typecheck`, `npm run lint`, `npm run format:check`, and
`npm run build` from the frontend directory. The test suite includes a real HTTP
integration test that starts an isolated FastAPI backend with a temporary SQLite
database, exercises this adapter against it, and shuts it down. It uses the
backend's `.venv` Python (or `POLICY_INTEGRATION_PYTHON`); the integration suite is
skipped when that interpreter is absent. Install the backend requirements and
httpx/pytest plus SDK dependencies before running backend tests. Integration
fixtures and credentials are confined to the temporary database.

Configure `VITE_API_BASE_URL` for the backend as usual. Apply the additive schema
and set signing secrets as documented in the backend's `docs/project-policy-api.md`.
The workspace project selector now loads the authenticated member's real projects;
the backend filters its project list by active membership.
