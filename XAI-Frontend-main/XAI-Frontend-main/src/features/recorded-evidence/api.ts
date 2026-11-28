import { isAxiosError } from 'axios'
import { z } from 'zod'
import { apiClient } from '@/lib/api/client'

const text = z.string().nullable()
const evidenceSchema = z.object({
  request_id: z.string(),
  project_id: z.string(),
  evaluated_at: text,
  evaluation: z.object({
    outcome: z.enum(['allow', 'approval_required', 'deny']).nullable(),
    risk_level: z.enum(['low', 'medium', 'high', 'critical']).nullable(),
    reason: text,
    reason_code: text,
    policy_version: text,
    matched_rule: z
      .object({
        rule_id: z.string(),
        effect: z.enum(['allow', 'approval_required', 'deny']),
        priority: z.number().int(),
      })
      .nullable(),
    field_states: z.record(
      z.string(),
      z.enum(['recorded', 'recorded_null', 'not_recorded', 'invalid']),
    ),
    record_state: z.enum(['recorded', 'unreadable']),
  }),
  context: z
    .object({
      agent_id: text.optional(),
      agent_name: text.optional(),
      action_name: text.optional(),
      reason: text.optional(),
      requested_at: text.optional(),
      expires_at: text.optional(),
      action_parameters: z.record(z.string(), z.unknown()).nullable().optional(),
      metadata: z.record(z.string(), z.unknown()).nullable().optional(),
    })
    .nullable(),
  context_source: z
    .enum(['evaluation_snapshot', 'approval_record', 'automatic_authorization'])
    .nullable(),
  human_review: z
    .object({
      approval_id: z.string(),
      status: z.string(),
      submitted_at: text,
      expires_at: text,
      reviewer_id: text,
      reviewer_comment: text,
      reviewed_at: text,
    })
    .nullable(),
  authorization_consumptions: z.array(z.string()),
  execution_outcome: z.null(),
  model_reasoning: z.null(),
  integrity_status: z.literal('not_verified'),
})
const pageSchema = z.object({
  project_id: z.string(),
  items: z.array(evidenceSchema),
  total: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
})
export type RecordedEvidence = z.infer<typeof evidenceSchema>

export async function getEvidence(projectId: string, offset: number, signal?: AbortSignal) {
  const { data } = await apiClient.get<unknown>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/evaluations`,
    {
      params: { limit: 25, offset },
      signal,
    },
  )
  const parsed = pageSchema.safeParse(data)
  if (
    !parsed.success ||
    parsed.data.project_id !== projectId ||
    parsed.data.items.some((item) => item.project_id !== projectId)
  ) {
    throw new Error('The server returned invalid evidence. No decision details are displayed.')
  }
  return parsed.data
}

export function evidenceError(error: unknown) {
  if (isAxiosError(error)) {
    if (error.response?.status === 401) return 'Sign in again to view recorded evidence.'
    if (error.response?.status === 403)
      return 'You do not have access to this project’s recorded evidence.'
    if (error.response?.status === 404)
      return 'Recorded evidence is unavailable for this project on this server.'
    return 'Could not load recorded evidence. Try again.'
  }
  return error instanceof Error ? error.message : 'Could not load recorded evidence.'
}

export function exportEvidence(record: RecordedEvidence) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(record, null, 2)], { type: 'application/json' }),
  )
  const link = document.createElement('a')
  link.href = url
  link.download = `evaluation-${encodeURIComponent(record.request_id)}.json`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
