import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { AxiosError } from 'axios'
import { apiClient } from '@/lib/api/client'
import { ExplainabilityPage } from '@/features/explainability/ExplainabilityPage'
import { ReportsPage } from '@/features/policy-audit/ReportsPage'
import { exportEvidence, type RecordedEvidence } from './api'

const record: RecordedEvidence = {
  request_id: 'request-1',
  project_id: 'p',
  evaluated_at: '2026-11-28T10:00:00Z',
  evaluation: {
    outcome: 'deny',
    risk_level: 'high',
    reason: 'Restricted by stored policy',
    reason_code: 'RULE_MATCHED',
    policy_version: 'policy-v7',
    matched_rule: { rule_id: 'block-delete', effect: 'deny', priority: 10 },
    record_state: 'recorded',
    field_states: Object.fromEntries(
      ['outcome', 'risk_level', 'reason', 'reason_code', 'matched_rule', 'policy_version'].map(
        (key) => [key, 'recorded' as const],
      ),
    ),
  },
  context: {
    agent_id: 'agent-7',
    action_name: 'delete_profile',
    action_parameters: { customer_id: 123 },
    metadata: {},
    reason: 'Caller request',
  },
  context_source: 'evaluation_snapshot',
  human_review: null,
  authorization_consumptions: [],
  execution_outcome: null,
  model_reasoning: null,
  integrity_status: 'not_verified',
}
function page(items = [record], total = items.length, offset = 0) {
  return { project_id: 'p', items, total, limit: 25, offset }
}
function mount(audit = false) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={['/projects/p/evidence']}>
        <Link to="/projects/q/evidence">Switch project</Link>
        <Routes>
          <Route
            path="/projects/:projectId/evidence"
            element={audit ? <ReportsPage /> : <ExplainabilityPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
beforeEach(() => vi.spyOn(apiClient, 'get').mockResolvedValue({ data: page() }))
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test.each([false, true])('renders only recorded evidence in the page (audit=%s)', async (audit) => {
  mount(audit)
  expect(await screen.findByText('Restricted by stored policy')).toBeInTheDocument()
  expect(screen.getByText('policy-v7')).toBeInTheDocument()
  expect(screen.getByText('block-delete (deny, priority 10)')).toBeInTheDocument()
  expect(screen.getByText(/does not prove that the tool ran or succeeded/)).toBeInTheDocument()
  expect(screen.queryByText('User task received')).not.toBeInTheDocument()
  expect(screen.queryByText('248')).not.toBeInTheDocument()
  expect(apiClient.get).toHaveBeenCalledWith(
    '/api/v1/projects/p/evaluations',
    expect.objectContaining({ params: { limit: 25, offset: 0 } }),
  )
})

test('shows loading and safe empty states without example decisions', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(apiClient.get).mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve
    }),
  )
  mount()
  expect(screen.getByText('Loading recorded evidence')).toBeInTheDocument()
  finish({ data: page([]) })
  expect(await screen.findByText('No recorded evaluations')).toBeInTheDocument()
  expect(screen.queryByText(/Recorded decision —/)).not.toBeInTheDocument()
})

test('distinguishes absent matched-rule evidence from a recorded no-match result', async () => {
  const missing = {
    ...record,
    context: null,
    context_source: null,
    evaluation: {
      ...record.evaluation,
      matched_rule: null,
      field_states: { ...record.evaluation.field_states, matched_rule: 'not_recorded' as const },
    },
  }
  const checked = {
    ...missing,
    request_id: 'request-2',
    evaluation: {
      ...missing.evaluation,
      reason_code: 'NO_MATCH',
      field_states: { ...missing.evaluation.field_states, matched_rule: 'recorded_null' as const },
    },
  }
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: page([missing, checked]) })
  mount()
  await screen.findByText('Restricted by stored policy')
  expect(screen.queryByText('No rule matched (recorded evaluator result)')).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Recorded evaluation'), { target: { value: 'request-2' } })
  expect(await screen.findByText('No rule matched (recorded evaluator result)')).toBeInTheDocument()
})

test.each([403, 500])(
  'reports HTTP %s errors without falling back to demo evidence',
  async (status) => {
    vi.mocked(apiClient.get).mockRejectedValueOnce(
      new AxiosError('failed', undefined, undefined, undefined, {
        status,
        data: {},
        statusText: '',
        headers: {},
        config: { headers: {} } as never,
      }),
    )
    mount(true)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/Recorded decision —/)).not.toBeInTheDocument()
  },
)

test('rejects cross-project or malformed API data', async () => {
  vi.mocked(apiClient.get).mockResolvedValueOnce({
    data: page([{ ...record, project_id: 'foreign' }]),
  })
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('invalid evidence')
  expect(screen.queryByText('Restricted by stored policy')).not.toBeInTheDocument()
})

test('refresh displays recorded human review without inferring execution', async () => {
  mount()
  await screen.findByText('Restricted by stored policy')
  vi.mocked(apiClient.get).mockResolvedValueOnce({
    data: page([
      {
        ...record,
        human_review: {
          approval_id: 'approval-1',
          status: 'approved',
          submitted_at: record.evaluated_at,
          expires_at: null,
          reviewed_at: '2026-11-28T10:01:00Z',
          reviewer_id: 'reviewer-1',
          reviewer_comment: 'Reviewed request',
        },
        authorization_consumptions: ['2026-11-28T10:02:00Z'],
      },
    ]),
  })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh evidence' }))
  expect(await screen.findByText('Reviewed request')).toBeInTheDocument()
  expect(screen.getByText('approved')).toBeInTheDocument()
  expect(screen.getByText('2026-11-28T10:02:00Z')).toBeInTheDocument()
  expect(screen.getByText(/Tool execution outcome: Not recorded/)).toBeInTheDocument()
})

test('paginates and does not retain an old project selection', async () => {
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: page([record], 26) })
  mount(true)
  await screen.findByText('Restricted by stored policy')
  vi.mocked(apiClient.get).mockResolvedValueOnce({
    data: page([{ ...record, request_id: 'next-page' }], 26, 25),
  })
  fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
  await screen.findByRole('region', { name: 'Evidence for next-page' })
  expect(apiClient.get).toHaveBeenLastCalledWith(
    '/api/v1/projects/p/evaluations',
    expect.objectContaining({ params: { limit: 25, offset: 25 } }),
  )
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { ...page([]), project_id: 'q' } })
  fireEvent.click(screen.getByRole('link', { name: 'Switch project' }))
  await screen.findByText('No recorded evaluations')
  expect(screen.queryByText('Restricted by stored policy')).not.toBeInTheDocument()
})

test('downloads recorded JSON without synthetic events or invented reasoning', async () => {
  let blob!: Blob
  const create = vi.fn((value: Blob) => {
    blob = value
    return 'blob:evidence'
  })
  const revoke = vi.fn()
  const NativeURL = URL
  vi.stubGlobal(
    'URL',
    class extends NativeURL {
      static createObjectURL = create
      static revokeObjectURL = revoke
    },
  )
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  exportEvidence(record)
  const content = await new Promise<string>((resolve) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.readAsText(blob)
  })
  const exported = JSON.parse(content)
  expect(exported.evaluation.reason).toBe('Restricted by stored policy')
  expect(exported.model_reasoning).toBeNull()
  expect(exported.execution_outcome).toBeNull()
  expect(exported).not.toHaveProperty('events')
  expect(exported).not.toHaveProperty('authorization_receipt')
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:evidence'))
})
