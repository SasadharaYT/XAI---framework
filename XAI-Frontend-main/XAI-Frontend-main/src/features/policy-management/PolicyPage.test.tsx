import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { AxiosError } from 'axios'
import { apiClient } from '@/lib/api/client'
import { PolicyPage } from './PolicyPage'
import { getPolicy } from './api'

const policy = {
  project_id: 'project-a',
  version: 'v1',
  enabled: true,
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
  rules: [
    {
      rule_id: 'read',
      action_name: 'read_profile',
      outcome: 'allow',
      risk_level: 'low',
      reason: 'Approved read',
      priority: 10,
      enabled: true,
      project_id: 'project-a',
      agent_id: null,
    },
  ],
}

function apiError(status: number, detail = 'Error') {
  return new AxiosError('Request failed', undefined, undefined, undefined, {
    status,
    data: { detail },
    statusText: '',
    headers: {},
    config: { headers: {} } as never,
  })
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/projects/project-a/policy']}>
        <Link to="/projects/project-b/policy">Switch project</Link>
        <Routes>
          <Route path="/projects/:projectId/policy" element={<PolicyPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.spyOn(apiClient, 'get').mockResolvedValue({ data: structuredClone(policy) })
  vi.spyOn(apiClient, 'put').mockResolvedValue({
    data: { ...structuredClone(policy), version: 'v2' },
  })
})
afterEach(() => vi.restoreAllMocks())

test('loads the route project policy from the API and displays saved settings', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(apiClient.get).mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve
    }),
  )
  mount()
  expect(screen.getByText('Loading policy')).toBeInTheDocument()
  finish({ data: policy })
  expect(await screen.findByDisplayValue('read_profile')).toBeInTheDocument()
  expect(screen.getByText('v1')).toBeInTheDocument()
  expect(apiClient.get).toHaveBeenCalledWith(
    '/api/v1/projects/project-a/policy',
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  )
})

test('edits supported settings and saves the canonical backend payload', async () => {
  mount()
  await screen.findByDisplayValue('read_profile')
  fireEvent.change(screen.getByLabelText('New policy version'), { target: { value: 'v2' } })
  fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'deny' } })
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Restricted action' } })
  fireEvent.click(screen.getByLabelText('Enable policy'))
  fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
  expect(await screen.findByText('Policy saved successfully.')).toBeInTheDocument()
  expect(apiClient.put).toHaveBeenCalledWith('/api/v1/projects/project-a/policy', {
    version: 'v2',
    enabled: false,
    rules: [{ ...policy.rules[0], outcome: 'deny', reason: 'Restricted action' }],
  })
})

test('persists disabled rules and can re-enable them after reload', async () => {
  const disabled = { ...policy, version: 'v2', rules: [{ ...policy.rules[0], enabled: false }] }
  vi.mocked(apiClient.put).mockResolvedValueOnce({ data: disabled })
  mount()
  await screen.findByDisplayValue('read_profile')
  const active = screen.getByLabelText('Rule 1 active')
  fireEvent.click(active)
  expect(active).not.toBeChecked()
  fireEvent.click(active)
  expect(screen.getByLabelText('Action name')).toBeEnabled()
  fireEvent.click(active)
  fireEvent.change(screen.getByLabelText('New policy version'), { target: { value: 'v2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
  await screen.findByText('Policy saved successfully.')
  expect(apiClient.put).toHaveBeenCalledWith('/api/v1/projects/project-a/policy', {
    version: 'v2',
    enabled: true,
    rules: [{ ...policy.rules[0], enabled: false }],
  })
  expect(screen.getByLabelText('Rule 1 active')).not.toBeChecked()
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: disabled })
  fireEvent.click(screen.getByRole('button', { name: 'Reload current policy' }))
  await waitFor(() => expect(apiClient.get).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.getByLabelText('Rule 1 active')).not.toBeChecked())
  fireEvent.click(screen.getByLabelText('Rule 1 active'))
  fireEvent.change(screen.getByLabelText('New policy version'), { target: { value: 'v3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
  await waitFor(() =>
    expect(apiClient.put).toHaveBeenLastCalledWith('/api/v1/projects/project-a/policy', {
      version: 'v3',
      enabled: true,
      rules: policy.rules,
    }),
  )
})

test('validates versions, required rule fields, and duplicate IDs before saving', async () => {
  mount()
  await screen.findByDisplayValue('read_profile')
  fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Enter a new version')
  fireEvent.change(screen.getByLabelText('New policy version'), { target: { value: 'v1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Choose a new policy version')
  fireEvent.change(screen.getByLabelText('New policy version'), { target: { value: 'v2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add rule' }))
  fireEvent.change(screen.getAllByLabelText('Rule ID')[1], { target: { value: 'read' } })
  fireEvent.change(screen.getAllByLabelText('Action name')[1], {
    target: { value: 'write_profile' },
  })
  fireEvent.change(screen.getAllByLabelText('Reason')[1], { target: { value: 'Review writes' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Rule IDs must be unique')
  expect(apiClient.put).not.toHaveBeenCalled()
})

test('can remove an unfinished rule without losing the existing policy', async () => {
  mount()
  await screen.findByDisplayValue('read_profile')
  fireEvent.click(screen.getByRole('button', { name: 'Add rule' }))
  expect(screen.getAllByLabelText('Rule ID')).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: 'Remove rule 2' }))
  expect(screen.getAllByLabelText('Rule ID')).toHaveLength(1)
  expect(screen.getByLabelText('Action name')).toHaveValue('read_profile')
})

test.each([401, 403, 404, 500])(
  'shows load errors for HTTP %i without an editable fallback',
  async (status) => {
    vi.mocked(apiClient.get).mockRejectedValueOnce(apiError(status, 'Not Found'))
    mount()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save policy' })).not.toBeInTheDocument()
    expect(apiClient.put).not.toHaveBeenCalled()
  },
)

test.each([403, 409, 422, 500])(
  'preserves edits and reports failed saves for HTTP %i',
  async (status) => {
    vi.mocked(apiClient.put).mockRejectedValueOnce(apiError(status))
    mount()
    await screen.findByDisplayValue('read_profile')
    fireEvent.change(screen.getByLabelText('New policy version'), { target: { value: 'v2' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Unsaved edit' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save policy' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Unsaved edit')).toBeInTheDocument()
    expect(screen.getByText('v1')).toBeInTheDocument()
    expect(screen.queryByText('Policy saved successfully.')).not.toBeInTheDocument()
  },
)

test('only the explicit no-policy response permits creating an empty policy', async () => {
  vi.mocked(apiClient.get).mockRejectedValueOnce(apiError(404, 'Project policy not configured'))
  mount()
  expect(
    await screen.findByText('No rules yet. Add a rule to configure an action.'),
  ).toBeInTheDocument()
  expect(screen.getByText('Not configured')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save policy' })).toBeEnabled()
})

test('switching projects discards the old draft and requests the new project', async () => {
  mount()
  await screen.findByDisplayValue('read_profile')
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Project A draft' } })
  vi.mocked(apiClient.get).mockResolvedValueOnce({
    data: { ...policy, project_id: 'project-b', rules: [] },
  })
  fireEvent.click(screen.getByRole('link', { name: 'Switch project' }))
  await screen.findByText('No rules yet. Add a rule to configure an action.')
  expect(screen.queryByDisplayValue('Project A draft')).not.toBeInTheDocument()
  expect(apiClient.get).toHaveBeenLastCalledWith(
    '/api/v1/projects/project-b/policy',
    expect.anything(),
  )
})

test('reload replaces a stale draft with the current backend policy', async () => {
  mount()
  await screen.findByDisplayValue('read_profile')
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Stale draft' } })
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { ...policy, version: 'v3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reload current policy' }))
  await screen.findByText('v3')
  await waitFor(() => expect(screen.getByLabelText('Reason')).toHaveValue('Approved read'))
})

test('rejects a policy returned for a different project', async () => {
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { ...policy, project_id: 'other' } })
  await expect(getPolicy('project-a')).rejects.toThrow('unsupported policy')
})
