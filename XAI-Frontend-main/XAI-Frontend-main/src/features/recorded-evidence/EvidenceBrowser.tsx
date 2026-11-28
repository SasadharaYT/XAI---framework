import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { Page } from '@/components/layout/Page'
import { AsyncState, Card } from '@/components/shared/Primitives'
import { evidenceError, exportEvidence, getEvidence, type RecordedEvidence } from './api'
import './evidence.css'

export function EvidenceBrowser({ audit = false }: { audit?: boolean }) {
  const { projectId } = useParams()
  return projectId ? (
    <ProjectEvidence key={`${projectId}-${audit}`} projectId={projectId} audit={audit} />
  ) : null
}

function ProjectEvidence({ projectId, audit }: { projectId: string; audit: boolean }) {
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const query = useQuery({
    queryKey: ['recorded-evidence', projectId, offset],
    queryFn: ({ signal }) => getEvidence(projectId, offset, signal),
    retry: false,
    gcTime: 0,
  })
  const page = query.data
  const current = page?.items.find((item) => item.request_id === selected) ?? page?.items[0]
  return (
    <Page
      title={audit ? 'Transparency reports' : 'Explainability'}
      description="Review stored evaluation results and human decisions for this project."
    >
      <div className="recorded-evidence">
        <p className="muted">
          Project: {projectId}. These records do not establish tool execution or provide model
          reasoning. Record integrity has not been independently verified.
        </p>
        <button
          className="secondary-button"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          {query.isFetching ? 'Loading evidence…' : 'Refresh evidence'}
        </button>
        {query.isPending ? (
          <AsyncState
            title="Loading recorded evidence"
            description="Retrieving this project’s stored evaluations."
          />
        ) : query.isError ? (
          <Card role="alert">{evidenceError(query.error)}</Card>
        ) : (
          page && (
            <>
              <p>
                {page.total} recorded evaluations. Showing {page.items.length} on this page.
              </p>
              {page.items.length === 0 ? (
                <AsyncState
                  title="No recorded evaluations"
                  description="There are no evaluation records on this page. Missing evidence does not mean an action was allowed or checked."
                />
              ) : (
                <>
                  {audit ? (
                    <Card className="table-scroll">
                      <table>
                        <caption>Recorded evaluations</caption>
                        <thead>
                          <tr>
                            <th>Request / action</th>
                            <th>Evaluated at</th>
                            <th>Decision</th>
                            <th>Risk classification</th>
                            <th>Human review</th>
                            <th>Evidence</th>
                          </tr>
                        </thead>
                        <tbody>
                          {page.items.map((item) => (
                            <tr key={item.request_id}>
                              <td>
                                {item.request_id}
                                <small>{item.context?.action_name ?? 'Action not recorded'}</small>
                              </td>
                              <td>{item.evaluated_at ?? 'Not recorded'}</td>
                              <td>{item.evaluation.outcome ?? 'Not recorded'}</td>
                              <td>{item.evaluation.risk_level ?? 'Not recorded'}</td>
                              <td>{item.human_review?.status ?? 'No review record'}</td>
                              <td>
                                <button
                                  type="button"
                                  className="text-button"
                                  onClick={() => setSelected(item.request_id)}
                                  aria-label={`View evidence ${item.request_id}`}
                                >
                                  Details
                                </button>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </Card>
                  ) : (
                    <label className="evidence-selector">
                      Recorded evaluation
                      <select
                        value={current?.request_id ?? ''}
                        onChange={(event) => setSelected(event.target.value)}
                      >
                        {page.items.map((item) => (
                          <option key={item.request_id} value={item.request_id}>
                            {item.request_id} — {item.context?.action_name ?? 'Action not recorded'}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {current && <EvidenceDetails record={current} />}
                </>
              )}
              <nav className="evidence-pagination" aria-label="Evidence pages">
                <button
                  className="secondary-button"
                  disabled={offset === 0 || query.isFetching}
                  onClick={() => setOffset(Math.max(0, offset - 25))}
                >
                  Previous page
                </button>
                <button
                  className="secondary-button"
                  disabled={offset + page.limit >= page.total || query.isFetching}
                  onClick={() => setOffset(offset + 25)}
                >
                  Next page
                </button>
              </nav>
            </>
          )
        )}
      </div>
    </Page>
  )
}

function Value({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div>
      <dt>{name}</dt>
      <dd>{children ?? 'Not recorded'}</dd>
    </div>
  )
}

function EvidenceDetails({ record }: { record: RecordedEvidence }) {
  const result = record.evaluation
  const context = record.context
  const review = record.human_review
  function resultText(
    field: 'outcome' | 'risk_level' | 'reason' | 'reason_code' | 'policy_version',
  ) {
    const state = result.field_states[field]
    if (state === 'invalid') return 'Stored value is invalid; unavailable'
    if (state === 'recorded_null') return 'Recorded as null'
    return result[field] ?? 'Not recorded'
  }
  const matched = result.matched_rule
  const matchedText = matched
    ? `${matched.rule_id} (${matched.effect}, priority ${matched.priority})`
    : result.field_states.matched_rule === 'recorded_null'
      ? result.reason_code === 'NO_MATCH'
        ? 'No rule matched (recorded evaluator result)'
        : 'Recorded as null; no rule details'
      : result.field_states.matched_rule === 'invalid'
        ? 'Stored rule is invalid; unavailable'
        : 'Not recorded'
  return (
    <section className="evidence-details" aria-label={`Evidence for ${record.request_id}`}>
      <Card>
        <h2>Recorded decision — {record.request_id}</h2>
        {result.record_state === 'unreadable' && (
          <p role="alert">The stored evaluation result is unreadable. No decision is inferred.</p>
        )}
        <dl>
          <Value name="Evaluation timestamp">{record.evaluated_at}</Value>
          <Value name="Decision">{resultText('outcome')}</Value>
          <Value name="Risk classification">{resultText('risk_level')}</Value>
          <Value name="Evaluator reason">{resultText('reason')}</Value>
          <Value name="Reason code">{resultText('reason_code')}</Value>
          <Value name="Matched rule">{matchedText}</Value>
          <Value name="Policy version">{resultText('policy_version')}</Value>
        </dl>
        <p className="muted">
          Risk is the stored rule classification or fallback label, not a measured probability or
          model confidence.
        </p>
      </Card>
      <Card>
        <h2>Recorded action context</h2>
        <p>Source: {record.context_source ?? 'No readable context record available'}</p>
        <dl>
          <Value name="Agent ID">{context?.agent_id}</Value>
          <Value name="Agent name">{context?.agent_name}</Value>
          <Value name="Action name">{context?.action_name}</Value>
          <Value name="Caller-supplied reason">{context?.reason}</Value>
          <Value name="Requested at">{context?.requested_at}</Value>
          <Value name="Request expires at">{context?.expires_at}</Value>
        </dl>
        <h3>Submitted parameters</h3>
        <pre>
          {context?.action_parameters == null
            ? 'Not recorded or unreadable'
            : JSON.stringify(context.action_parameters, null, 2)}
        </pre>
        <h3>Caller metadata</h3>
        <pre>
          {context?.metadata == null
            ? 'Not recorded or unreadable'
            : JSON.stringify(context.metadata, null, 2)}
        </pre>
        <p className="muted">
          Submitted context is caller-provided data; this record does not verify external facts.
        </p>
      </Card>
      <Card>
        <h2>Human review evidence</h2>
        {review ? (
          <dl>
            <Value name="Approval ID">{review.approval_id}</Value>
            <Value name="Stored review status">{review.status}</Value>
            <Value name="Review requested at">{review.submitted_at}</Value>
            <Value name="Approval deadline">{review.expires_at}</Value>
            <Value name="Reviewer ID">{review.reviewer_id}</Value>
            <Value name="Reviewer comment">{review.reviewer_comment}</Value>
            <Value name="Reviewed at">{review.reviewed_at}</Value>
          </dl>
        ) : (
          <p>
            No human-review record is stored for this evaluation. This does not establish that human
            review occurred or was unnecessary.
          </p>
        )}
      </Card>
      <Card>
        <h2>Authorization evidence and limitations</h2>
        {record.authorization_consumptions.length ? (
          <>
            <p>Authorization consumption recorded at:</p>
            <ul>
              {record.authorization_consumptions.map((time, index) => (
                <li key={`${time}-${index}`}>{time}</li>
              ))}
            </ul>
          </>
        ) : (
          <p>No authorization-consumption record is stored.</p>
        )}
        <p>
          Tool execution outcome: Not recorded. Consuming an authorization does not prove that the
          tool ran or succeeded.
        </p>
        <p>Model reasoning: Not recorded. Record integrity: Not independently verified.</p>
        <button type="button" className="secondary-button" onClick={() => exportEvidence(record)}>
          Download recorded evidence JSON
        </button>
      </Card>
    </section>
  )
}
