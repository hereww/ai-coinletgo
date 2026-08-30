import type { AuditEvent, DashboardData, IntegrationStatus, ManualEntryAdvice, ManualEntryDraft, ManualEntryResult, MarketRow, OrderRow, PortfolioDecision, ReplayRun, RiskConfig, Signal } from './types'

const configuredBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').trim()
export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '')

export class ApiError extends Error {
  readonly status?: number
  readonly endpoint: string

  constructor(message: string, endpoint: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.endpoint = endpoint
  }
}

function readCookie(name: string): string | undefined {
  const prefix = `${name}=`
  return document.cookie
    .split(';')
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix))
    ?.slice(prefix.length)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  const csrf = readCookie('frc_csrf')
  if (csrf && init.method && init.method !== 'GET') headers.set('X-CSRF-Token', csrf)
  const endpoint = `${apiBaseUrl}${path}`
  let response: Response
  try {
    response = await fetch(endpoint, { ...init, headers, credentials: 'include' })
  } catch {
    throw new ApiError('无法连接控制台 API，请检查服务器地址、HTTPS 证书或网络连接', endpoint)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: '请求失败' }))
    const error = new ApiError(body.detail ?? `HTTP ${response.status}`, endpoint, response.status)
    if (response.status === 401 && !path.endsWith('/auth/me') && !path.endsWith('/auth/login')) {
      window.dispatchEvent(new CustomEvent('frc:unauthorized'))
    }
    throw error
  }
  return response.json() as Promise<T>
}

export const api = {
  me: () => request<{ username: string; auth_required: boolean; environment: string }>('/api/v1/auth/me'),
  login: (username: string, password: string) =>
    request<{ username: string; csrf_token: string }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/v1/auth/logout', { method: 'POST' }),
  dashboard: () => request<DashboardData>('/api/v1/dashboard'),
  positions: () => request<DashboardData['positions']>('/api/v1/positions'),
  signals: () => request<Signal[]>('/api/v1/signals'),
  portfolioDecisions: () => request<PortfolioDecision[]>('/api/v1/portfolio-decisions'),
  market: () => request<MarketRow[]>('/api/v1/market'),
  orders: () => request<OrderRow[]>('/api/v1/orders'),
  audit: () => request<AuditEvent[]>('/api/v1/audit'),
  config: () => request<RiskConfig>('/api/v1/config'),
  integrations: () => request<IntegrationStatus>('/api/v1/integrations'),
  probeIntegration: (target: 'testnet' | 'model') =>
    request<{ name: string; state: string; detail: string | null }>('/api/v1/integrations/probe', {
      method: 'POST',
      body: JSON.stringify({ target }),
    }),
  updateModelIntegration: (payload: {
    base_url: string | null
    model_name: string
    reasoning_effort: IntegrationStatus['model']['reasoning_effort']
    timeout_seconds: number
    daily_request_limit: number
    strategy_profile: IntegrationStatus['model']['strategy_profile']
  }) => request('/api/v1/integrations/model', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  }),
  selectModelProfile: (profileId: IntegrationStatus['model']['active_profile']) =>
    request<IntegrationStatus['model']>('/api/v1/integrations/model/profile', {
      method: 'PATCH',
      body: JSON.stringify({ profile_id: profileId }),
    }),
  updateConfig: (payload: Partial<RiskConfig> & { totp_code?: string }) =>
    request<RiskConfig>('/api/v1/config', { method: 'PATCH', body: JSON.stringify(payload) }),
  pause: () => request<{ mode: string }>('/api/v1/actions/pause', { method: 'POST' }),
  runCycle: (totpCode = '') => request<{ status: string; operation_id: string }>('/api/v1/actions/run-cycle', {
    method: 'POST',
    body: JSON.stringify({ totp_code: totpCode, confirmation: 'RUN CYCLE' }),
  }),
  resumeTestnet: () =>
    request<{ mode: string }>('/api/v1/actions/resume-testnet', { method: 'POST' }),
  reconcileTakeover: (totpCode: string) =>
    request<{ mode: string }>('/api/v1/actions/reconcile-takeover', {
      method: 'POST',
      body: JSON.stringify({ totp_code: totpCode, confirmation: 'TAKEOVER' }),
    }),
  unlockLive: (totpCode: string) =>
    request<{ mode: string }>('/api/v1/actions/unlock-live', {
      method: 'POST',
      body: JSON.stringify({ totp_code: totpCode, confirmation: 'UNLOCK LIVE' }),
    }),
  flatten: (totpCode: string) =>
    request<{ mode: string }>('/api/v1/actions/emergency-flatten', {
      method: 'POST',
      body: JSON.stringify({ totp_code: totpCode, confirmation: 'FLATTEN' }),
    }),
  reducePosition: (positionId: string, fraction: number, totpCode: string) =>
    request<OrderRow>('/api/v1/actions/reduce-position', {
      method: 'POST',
      body: JSON.stringify({
        position_id: positionId,
        fraction,
        operation_id: crypto.randomUUID(),
        totp_code: totpCode,
        confirmation: 'REDUCE',
      }),
    }),
  manualEntry: (payload: ManualEntryDraft & { operation_id: string }) =>
    request<ManualEntryResult>('/api/v1/actions/manual-entry', {
      method: 'POST',
      body: JSON.stringify({ ...payload, confirmation: 'OPEN TESTNET POSITION' }),
    }),
  manualEntryAdvice: (payload: ManualEntryDraft & { messages: Array<{ role: 'user' | 'assistant'; content: string }> }) =>
    request<ManualEntryAdvice>('/api/v1/actions/manual-entry/advice', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  createReplay: (payload: {
    mode: 'deterministic' | 'recorded_portfolio'
    symbols?: string[]
    start_date?: string
    end_date?: string
    portfolio_decision_id?: string
  }) =>
    request<{ id: string; status: string }>('/api/v1/replays', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  replays: () => request<ReplayRun[]>('/api/v1/replays'),
}
