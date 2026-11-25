# Security

## Credentials and data minimization

Pass API keys from a secret manager or environment variable. The SDK stores the key as a Pydantic `SecretStr` and uses it only for the Authorization header. It never includes credentials in logs or raised messages. Never place secrets in action parameters or metadata. Credential-like field names are rejected recursively; use references such as a vault key and resolve them only inside the protected function. Name-based detection cannot identify every secret hidden under an innocuous key, so callers must not pass secret values.

## Authorization enforcement

The backend is the source of truth for project-agent membership, evaluation, human decisions, receipt validity, receipt expiry, and single-use consumption. An `allow` response or `approved=True` by itself is never sufficient. Both automatic allow and human approval must yield a receipt, and the SDK sends the original request context with that receipt for backend verification. Any missing, invalid, expired, or unverifiable receipt blocks execution. The current backend only issues human-approval receipts because its evaluator always requests review.

Receipts are cryptographically signed by the backend using `GOVERNANCE_RECEIPT_SECRET` and bound to exact project ID, agent ID, action name, canonical parameter content, request ID, approval ID, and expiry. A changed parameter set must use a new request ID and receive new authorization. The verification endpoint atomically consumes a receipt and returns `valid: true` and `consumed: true` only to the sole winning caller. Deployments must set the signing secret to a random value of at least 32 characters and use a shared durable database across workers.

## Retry and duplicate behavior

The SDK generates UUID request IDs and sends them as idempotency keys for evaluate and create operations. The backend must persist idempotency results and return the same approval rather than creating duplicates. Backend authorization consumption must be atomic across processes; in-process SDK locking alone is insufficient. Retries after receipt consumption must not authorize execution again. This is at-most-once behavior and may sacrifice execution if the process fails after consumption.

## Fail-closed cases

The protected function is not called when the service is unavailable, agent/project validation fails, responses do not validate, approval remains pending through timeout, the decision is rejected/expired/cancelled, or authorization verification fails. Do not configure transport retries with a new idempotency key for the same logical request.

