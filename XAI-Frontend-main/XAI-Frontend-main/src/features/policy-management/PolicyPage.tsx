import { useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { Page } from '@/components/layout/Page'
import { AsyncState, Badge, Card } from '@/components/shared/Primitives'
import {
  getPolicy,
  policyError,
  policyInputSchema,
  savePolicy,
  type PolicyRule,
  type ProjectPolicy,
} from './api'
import './policy.css'

type DraftRule = PolicyRule & { key: number }

export function PolicyPage() {
  const { projectId } = useParams()
  // Remount the editor when the route project changes, discarding the old project's draft.
  return projectId ? <ProjectPolicyPage key={projectId} projectId={projectId} /> : null
}

function ProjectPolicyPage({ projectId }: { projectId: string }) {
  const [editorKey, setEditorKey] = useState(0)
  const query = useQuery({
    queryKey: ['project-policy', projectId],
    queryFn: ({ signal }) => getPolicy(projectId, signal),
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  })
  return (
    <Page
      title="Project policy"
      description="Manage which agent actions are allowed, blocked, or sent for human review."
    >
      <div className="policy-management">
        <p className="muted">
          Project: <strong>{projectId}</strong>
        </p>
        {query.isPending ? (
          <AsyncState title="Loading policy" description="Checking the current project policy…" />
        ) : query.isError ? (
          <Card role="alert">
            <p>{policyError(query.error)}</p>
            <button className="secondary-button" onClick={() => void query.refetch()}>
              Retry loading
            </button>
          </Card>
        ) : (
          <PolicyEditor
            key={editorKey}
            projectId={projectId}
            initial={query.data}
            reload={async () => {
              const result = await query.refetch()
              if (result.isSuccess) setEditorKey((value) => value + 1)
            }}
          />
        )}
      </div>
    </Page>
  )
}

function PolicyEditor({
  projectId,
  initial,
  reload,
}: {
  projectId: string
  initial: ProjectPolicy | null
  reload: () => Promise<unknown>
}) {
  const client = useQueryClient()
  const [saved, setSaved] = useState(initial)
  const [version, setVersion] = useState('')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [rules, setRules] = useState<DraftRule[]>(() =>
    (initial?.rules ?? []).map((rule, key) => ({ ...rule, key })),
  )
  const [nextKey, setNextKey] = useState(rules.length)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  function changeRule(key: number, update: Partial<DraftRule>) {
    setRules((current) => current.map((rule) => (rule.key === key ? { ...rule, ...update } : rule)))
    setMessage('')
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setMessage('')
    setError('')
    const input = policyInputSchema.safeParse({
      version,
      enabled,
      rules: rules.map((rule) => ({
        rule_id: rule.rule_id,
        action_name: rule.action_name,
        outcome: rule.outcome,
        risk_level: rule.risk_level,
        reason: rule.reason,
        priority: rule.priority,
        enabled: rule.enabled,
        ...(rule.project_id !== undefined ? { project_id: rule.project_id } : {}),
        ...(rule.agent_id !== undefined ? { agent_id: rule.agent_id } : {}),
      })),
    })
    if (!input.success) {
      setError(
        'Enter a new version and complete every rule. Rule IDs must be unique and priorities must be whole numbers.',
      )
      return
    }
    if (input.data.version === saved?.version) {
      setError('Choose a new policy version before saving changes.')
      return
    }
    if (input.data.rules.some((rule) => rule.project_id && rule.project_id !== projectId)) {
      setError('Rule project scopes must match the current project.')
      return
    }
    setSaving(true)
    try {
      const response = await savePolicy(projectId, input.data)
      setSaved(response)
      setVersion('')
      setEnabled(response.enabled)
      setRules(response.rules.map((rule, key) => ({ ...rule, key })))
      setNextKey(response.rules.length)
      client.setQueryData(['project-policy', projectId], response)
      setMessage('Policy saved successfully.')
    } catch (cause) {
      setError(policyError(cause))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} noValidate>
      <Card>
        <div className="policy-heading">
          <h2>Active policy</h2>
          <Badge tone={saved?.enabled ? 'green' : 'amber'}>
            {saved ? (saved.enabled ? 'Enabled' : 'Disabled') : 'Not configured'}
          </Badge>
        </div>
        <p>
          Saved version: <strong>{saved?.version ?? 'None'}</strong>
        </p>
        {saved && <p className="muted">Updated {new Date(saved.updated_at).toLocaleString()}</p>}
        {!saved && (
          <p>
            No policy is configured. Actions require human review until an enabled policy is saved.
          </p>
        )}
        <p className="muted">
          Missing or unmatched rules require human review. Policy changes apply to new requests.
        </p>
        <button
          type="button"
          className="secondary-button"
          disabled={saving}
          onClick={() => void reload()}
        >
          Reload current policy
        </button>
      </Card>
      <fieldset disabled={saving}>
        <Card>
          <div className="policy-grid">
            <label>
              New policy version
              <input
                value={version}
                maxLength={120}
                onChange={(event) => {
                  setVersion(event.target.value)
                  setMessage('')
                }}
                placeholder="Enter a new version"
              />
            </label>
            <label className="policy-check">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => {
                  setEnabled(event.target.checked)
                  setMessage('')
                }}
              />
              Enable policy
            </label>
          </div>
          <p>Disabled policies require human review for all new actions.</p>
        </Card>
        <div className="policy-heading">
          <h2>Rules</h2>
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              setRules((current) => [
                ...current,
                {
                  key: nextKey,
                  enabled: true,
                  rule_id: '',
                  action_name: '',
                  outcome: 'approval_required',
                  risk_level: 'medium',
                  reason: '',
                  priority: 0,
                },
              ])
              setNextKey((value) => value + 1)
              setMessage('')
            }}
          >
            Add rule
          </button>
        </div>
        <p className="muted">
          Disabled rules stay saved and can be enabled again later. They do not participate in
          evaluation.
        </p>
        {rules.length === 0 && <Card>No rules yet. Add a rule to configure an action.</Card>}
        {rules.map((rule, index) => (
          <Card key={rule.key}>
            <div className="policy-heading">
              <h3>Rule {index + 1}</h3>
              <button
                type="button"
                className="secondary-button"
                aria-label={`Remove rule ${index + 1}`}
                onClick={() => {
                  setRules((current) => current.filter((item) => item.key !== rule.key))
                  setMessage('')
                }}
              >
                Remove rule
              </button>
              <label className="policy-check">
                <input
                  type="checkbox"
                  checked={rule.enabled}
                  onChange={(event) => changeRule(rule.key, { enabled: event.target.checked })}
                  aria-label={`Rule ${index + 1} active`}
                />
                Active
              </label>
            </div>
            <fieldset className="policy-grid">
              <label>
                Rule ID
                <input
                  value={rule.rule_id}
                  onChange={(event) => changeRule(rule.key, { rule_id: event.target.value })}
                />
              </label>
              <label>
                Action name
                <input
                  value={rule.action_name}
                  onChange={(event) => changeRule(rule.key, { action_name: event.target.value })}
                />
              </label>
              <label>
                Decision
                <select
                  value={rule.outcome}
                  onChange={(event) =>
                    changeRule(rule.key, { outcome: event.target.value as PolicyRule['outcome'] })
                  }
                >
                  <option value="approval_required">Require approval</option>
                  <option value="allow">Allow</option>
                  <option value="deny">Block</option>
                </select>
              </label>
              <label>
                Risk level
                <select
                  value={rule.risk_level}
                  onChange={(event) =>
                    changeRule(rule.key, {
                      risk_level: event.target.value as PolicyRule['risk_level'],
                    })
                  }
                >
                  {(['low', 'medium', 'high', 'critical'] as const).map((risk) => (
                    <option key={risk} value={risk}>
                      {risk}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Priority
                <input
                  type="number"
                  step="1"
                  value={Number.isNaN(rule.priority) ? '' : rule.priority}
                  onChange={(event) =>
                    changeRule(rule.key, {
                      priority: event.target.value === '' ? NaN : Number(event.target.value),
                    })
                  }
                />
              </label>
              <label>
                Agent ID (optional)
                <input
                  value={rule.agent_id ?? ''}
                  onChange={(event) =>
                    changeRule(rule.key, { agent_id: event.target.value || null })
                  }
                />
              </label>
              <label className="policy-wide">
                Reason
                <textarea
                  value={rule.reason}
                  onChange={(event) => changeRule(rule.key, { reason: event.target.value })}
                />
              </label>
            </fieldset>
            {rule.outcome === 'allow' && rule.enabled && (
              <p className="muted">
                Allow applies to all parameter values for this action within its project and agent
                scope.
              </p>
            )}
          </Card>
        ))}
        <button className="primary-button" type="submit">
          {saving ? 'Saving policy…' : 'Save policy'}
        </button>
      </fieldset>
      {saving && <p role="status">Saving policy…</p>}
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
    </form>
  )
}
