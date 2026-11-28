import { isAxiosError } from 'axios'
import { z } from 'zod'
import { apiClient } from '@/lib/api/client'

const nonblank = z.string().refine((value) => value.trim().length > 0)
export const ruleSchema = z
  .object({
    rule_id: nonblank,
    action_name: nonblank,
    outcome: z.enum(['allow', 'approval_required', 'deny']),
    risk_level: z.enum(['low', 'medium', 'high', 'critical']),
    reason: nonblank,
    priority: z.number().int().default(0),
    enabled: z.boolean().default(true),
    project_id: z.string().nullable().optional(),
    agent_id: z.string().nullable().optional(),
  })
  .strict()

export const policyInputSchema = z
  .object({
    version: z.string().trim().min(1).max(120),
    enabled: z.boolean(),
    rules: z.array(ruleSchema),
  })
  .strict()
  .refine(
    (policy) => new Set(policy.rules.map((rule) => rule.rule_id)).size === policy.rules.length,
    {
      message: 'Rule IDs must be unique.',
    },
  )

const policySchema = z
  .object({
    project_id: z.string(),
    version: z.string(),
    enabled: z.boolean(),
    rules: z.array(ruleSchema),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export type PolicyRule = z.infer<typeof ruleSchema>
export type PolicyInput = z.infer<typeof policyInputSchema>
export type ProjectPolicy = z.infer<typeof policySchema>

function path(projectId: string) {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/policy`
}

function parsePolicy(data: unknown, projectId: string): ProjectPolicy {
  const result = policySchema.safeParse(data)
  if (!result.success || result.data.project_id !== projectId) {
    throw new Error(
      'The server returned an unsupported policy. Reload or contact your administrator.',
    )
  }
  return result.data
}

export async function getPolicy(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectPolicy | null> {
  try {
    const { data } = await apiClient.get<unknown>(path(projectId), { signal })
    return parsePolicy(data, projectId)
  } catch (error) {
    if (
      isAxiosError(error) &&
      error.response?.status === 404 &&
      error.response.data?.detail === 'Project policy not configured'
    )
      return null
    throw error
  }
}

export async function savePolicy(projectId: string, policy: PolicyInput): Promise<ProjectPolicy> {
  const input = policyInputSchema.parse(policy)
  const { data } = await apiClient.put<unknown>(path(projectId), input)
  return parsePolicy(data, projectId)
}

export function policyError(error: unknown): string {
  if (isAxiosError(error)) {
    switch (error.response?.status) {
      case 401:
        return 'Your session has expired. Sign in again to manage this policy.'
      case 403:
        return 'You do not have permission to manage this project’s policy.'
      case 404:
        return 'Policy management is unavailable on this server. Contact your administrator.'
      case 409:
        return 'The policy changed or this version is already in use. Reload the current policy before saving again.'
      case 422:
        return 'The server rejected the policy. Check the rules, project and agent scopes, and version.'
      default:
        return 'Could not reach the policy service. Your edits have not been saved. Try again.'
    }
  }
  return error instanceof Error ? error.message : 'Unable to load or save the policy.'
}
