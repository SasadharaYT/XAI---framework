// @vitest-environment node
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, rmdirSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { apiClient } from '@/lib/api/client'
import { getPolicy, savePolicy, type PolicyInput } from './api'

const backend = resolve('../..', 'XAI-Backend-main/XAI-Backend-main')
const python =
  process.env.POLICY_INTEGRATION_PYTHON ??
  join(backend, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')

describe.skipIf(!existsSync(python))('real backend policy contract', () => {
  let server: ChildProcess | undefined
  let directory: string | undefined
  let oldBase: string | undefined
  let oldAuth: unknown

  beforeAll(async () => {
    const port = await new Promise<number>((accept) => {
      const socket = createServer()
      socket.listen(0, '127.0.0.1', () => {
        const address = socket.address()
        if (typeof address === 'object' && address) socket.close(() => accept(address.port))
      })
    })
    directory = mkdtempSync(join(tmpdir(), 'xai-policy-'))
    const database = join(directory, 'test.db').replaceAll('\\', '/')
    let output = ''
    let startError: Error | undefined
    server = spawn(python, [resolve('tests/policy_server.py')], {
      env: {
        ...process.env,
        DATABASE_URL: `sqlite:///${database}`,
        POLICY_TEST_PORT: String(port),
        JWT_SECRET: 'isolated-test-dashboard-secret-at-least-32-characters',
        GOVERNANCE_RECEIPT_SECRET: 'isolated-test-receipt-secret-at-least-32-characters',
      },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    server.on('error', (error) => {
      startError = error
    })
    server.stdout?.on('data', (chunk) => {
      output += chunk.toString()
    })
    server.stderr?.on('data', (chunk) => {
      output += chunk.toString()
    })
    oldBase = apiClient.defaults.baseURL
    oldAuth = apiClient.defaults.headers.common.Authorization
    apiClient.defaults.baseURL = `http://127.0.0.1:${port}`
    for (let attempt = 0; attempt < 200; attempt++) {
      if (startError || server.exitCode !== null)
        throw new Error(`Test API failed: ${startError?.message ?? output}`)
      try {
        await apiClient.get('/health', { timeout: 500 })
        break
      } catch {
        if (attempt === 199) throw new Error(`Test API did not start: ${output}`)
        await delay(100)
      }
    }
    const login = await apiClient.post('/api/v1/auth/login', {
      email: 'integration@example.test',
      password: 'IntegrationPass123',
    })
    apiClient.defaults.headers.common.Authorization = `Bearer ${login.data.access_token}`
  }, 30000)

  afterAll(async () => {
    if (server && server.exitCode === null) {
      const exited = new Promise<void>((done) => server!.once('close', () => done()))
      try {
        await apiClient.post('/__test_shutdown', {}, { timeout: 1000 })
      } catch {
        server.kill()
      }
      await exited
    }
    apiClient.defaults.baseURL = oldBase
    apiClient.defaults.headers.common.Authorization = oldAuth as string | undefined
    if (directory) {
      for (let attempt = 0; ; attempt++) {
        try {
          rmSync(join(directory, 'test.db'), { force: true })
          break
        } catch (error) {
          if (attempt >= 20) throw error
          await delay(100)
        }
      }
      rmdirSync(directory)
    }
  })

  test('saves, reloads, disables, re-enables and evaluates policy with real authorization', async () => {
    expect(await getPolicy('integration')).toBeNull()
    const input: PolicyInput = {
      version: 'v1',
      enabled: true,
      rules: [
        {
          rule_id: 'safe-read',
          action_name: 'read_profile',
          outcome: 'allow',
          risk_level: 'low',
          reason: 'Approved read operation',
          priority: 10,
          enabled: false,
        },
      ],
    }
    const created = await savePolicy('integration', input)
    expect((await getPolicy('integration'))?.rules[0].enabled).toBe(false)
    expect(created.rules).toHaveLength(1)
    expect(
      (await apiClient.get('/api/v1/projects')).data.map((project: { id: string }) => project.id),
    ).toEqual(['integration'])
    await expect(getPolicy('other')).rejects.toMatchObject({ response: { status: 403 } })
    await expect(savePolicy('other', input)).rejects.toMatchObject({ response: { status: 403 } })
    const key = await apiClient
      .post('/api/v1/projects/integration/api-keys', { name: 'integration' })
      .catch((error) => {
        expect(error.response.status).toBe(403)
        return null
      })
    // Ordinary members can edit policy, but project API-key issuance remains admin-only.
    expect(key).toBeNull()
    const headers = { Authorization: 'Bearer integration-sdk-key' }
    const evaluate = async (requestId: string) => {
      const now = new Date()
      const action = {
        request_id: requestId,
        project_id: 'integration',
        agent_id: 'integration-agent',
        action_name: 'read_profile',
        action_parameters: { customer_id: 42 },
        requested_at: now.toISOString(),
        expires_at: new Date(now.getTime() + 60000).toISOString(),
      }
      const result = await apiClient.post('/api/v1/oversight/evaluate', action, {
        headers: { ...headers, 'Idempotency-Key': requestId },
      })
      return { action, result: result.data }
    }
    expect((await evaluate('disabled')).result.outcome).toBe('approval_required')
    const enabled = { ...input, version: 'v2', rules: [{ ...input.rules[0], enabled: true }] }
    await savePolicy('integration', enabled)
    const { action, result } = await evaluate('enabled')
    expect(result.outcome).toBe('allow')
    expect(result.policy_version).toBe('v2')
    const verify = {
      request_id: action.request_id,
      project_id: action.project_id,
      agent_id: action.agent_id,
      action_name: action.action_name,
      action_parameters: action.action_parameters,
      authorization_receipt: result.authorization_receipt,
    }
    expect(
      (await apiClient.post('/api/v1/authorizations/verify', verify, { headers })).data.consumed,
    ).toBe(true)
    await expect(
      apiClient.post('/api/v1/authorizations/verify', verify, { headers }),
    ).rejects.toMatchObject({ response: { status: 409 } })
    await savePolicy('integration', { ...enabled, version: 'v3', enabled: false })
    expect((await evaluate('policy-disabled')).result.outcome).toBe('approval_required')
  })
})
