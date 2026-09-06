import type {
  AuditEvent,
  DailyPnlRow,
  DashboardData,
  FactorCatalog,
  FactorResearchRequest,
  FactorResearchRun,
  FactorResearchSubmission,
  FactorPolicyStatus,
  FactorShadowRanking,
  IncomeLedgerRow,
  IntegrationStatus,
  ManualEntryAdvice,
  ManualEntryDraft,
  ManualEntryResult,
  MarketRow,
  OrderRow,
  PnlSyncResult,
  PortfolioDecision,
  ReplayRun,
  RiskConfig,
  Signal,
  TradePnlRow,
} from './types'

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

const FIELD_LABELS: Record<string, string> = {
  capital_limit_usdt: '资金上限',
  single_trade_risk_pct: '单笔风险',
  portfolio_risk_pct: '组合风险',
  daily_loss_pct: '日亏损熔断',
  max_drawdown_pct: '总回撤熔断',
  max_leverage: '最高杠杆',
  max_margin_pct: '保证金上限',
  max_positions: '最多仓位',
  max_same_direction: '同向最多仓位',
  correlation_limit: '相关性阈值',
  candidate_count: '候选合约数量',
  scan_interval_minutes: '扫描周期',
  min_confidence: '最低置信度',
  min_net_reward_risk: '最低净盈亏比',
  min_stop_atr: '最小止损距离',
  max_stop_atr: '最大止损距离',
  manual_stop_atr: '手动止损距离',
  manual_take_profit_atr: '手动止盈距离',
  model_primary_portfolio_enabled: '模型主导组合决策',
  strong_trend_adx_min: '强趋势最低 ADX',
  trend_adx_min: '最低趋势强度',
  volatility_soft_limit_percentile: '高波动分位',
  volatility_hard_limit_percentile: '极端波动分位',
  elevated_volatility_risk_multiplier: '高波动风险系数',
  high_volatility_risk_multiplier: '极端波动风险系数',
  entry_symbols: '允许开仓代币',
  entry_direction: '允许开仓方向',
  entry_trigger: '入场触发',
  password: '操作密码',
}

const MESSAGE_TRANSLATIONS: Record<string, string> = {
  'minimum stop ATR cannot exceed maximum stop ATR': '最小止损距离不能大于最大止损距离',
  'manual stop ATR cannot exceed maximum stop ATR': '手动止损距离不能大于最大止损距离',
  'volatility soft limit cannot exceed hard limit': '高波动分位不能大于极端波动分位',
  'strong trend entry override is limited to Binance testnet': '强劲上升趋势放行仅限 Binance 测试网',
  'model-primary portfolio mode is limited to Binance testnet': '模型主导组合决策仅限 Binance 测试网',
  'Password verification failed': '操作密码验证失败',
  'Authentication required': '需要登录后才能执行此操作',
  'Session expired': '登录已过期，请重新登录',
}

function translateApiMessage(message: string): string {
  return MESSAGE_TRANSLATIONS[message] ?? message
}

function validationFieldName(loc: unknown): string | undefined {
  if (!Array.isArray(loc)) return undefined
  const field = [...loc].reverse().find((item): item is string => (
    typeof item === 'string' && item !== 'body' && item !== 'query' && item !== 'path'
  ))
  return field ? FIELD_LABELS[field] ?? field : undefined
}

function formatValidationError(value: Record<string, unknown>): string {
  const message = typeof value.msg === 'string' ? value.msg : ''
  const field = validationFieldName(value.loc)
  if (!message) return formatApiErrorDetail(value)
  if (!field) return translateApiMessage(message)

  const max = message.match(/less than or equal to ([^ ]+)/i)?.[1]
  if (max) return `${field}不能大于 ${max}`
  const min = message.match(/greater than or equal to ([^ ]+)/i)?.[1]
  if (min) return `${field}不能小于 ${min}`
  const greater = message.match(/greater than ([^ ]+)/i)?.[1]
  if (greater) return `${field}必须大于 ${greater}`
  const less = message.match(/less than ([^ ]+)/i)?.[1]
  if (less) return `${field}必须小于 ${less}`
  if (/valid|validation/i.test(message)) return `${field}格式无效`
  return `${field}：${translateApiMessage(message)}`
}

/** Convert FastAPI/Pydantic errors into text that can be shown in the UI. */
export function formatApiErrorDetail(detail: unknown): string {
  if (detail == null) return '请求失败'
  if (typeof detail === 'string') return translateApiMessage(detail)
  if (typeof detail === 'number' || typeof detail === 'boolean') return String(detail)
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => formatApiErrorDetail(item))
      .filter((item) => item && item !== '请求失败')
    return messages.length ? messages.join('；') : '请求失败'
  }
  if (typeof detail === 'object') {
    const value = detail as Record<string, unknown>
    if (value.detail !== undefined) return formatApiErrorDetail(value.detail)
    if (Array.isArray(value)) return formatApiErrorDetail(value)
    if (typeof value.msg === 'string') return formatValidationError(value)
    if (value.reason !== undefined) return formatApiErrorDetail(value.reason)
    if (value.message !== undefined) return formatApiErrorDetail(value.message)
    try {
      return JSON.stringify(value)
    } catch {
      return '请求失败'
    }
  }
  return String(detail)
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
    const body: unknown = await response.json().catch(() => ({ detail: '请求失败' }))
    const detail = (
      body && typeof body === 'object' && 'detail' in body
        ? (body as { detail?: unknown }).detail
        : body
    ) ?? `HTTP ${response.status}`
    const error = new ApiError(formatApiErrorDetail(detail), endpoint, response.status)
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
  cycleStatus: () => request<DashboardData['cycle_status']>('/api/v1/cycle-status'),
  market: () => request<MarketRow[]>('/api/v1/market'),
  orders: () => request<OrderRow[]>('/api/v1/orders'),
  pnlTrades: (startDate: string, endDate: string) =>
    request<TradePnlRow[]>(`/api/v1/pnl/trades?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`),
  pnlDaily: (startDate: string, endDate: string) =>
    request<DailyPnlRow[]>(`/api/v1/pnl/daily?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`),
  pnlLedger: (startDate: string, endDate: string) =>
    request<IncomeLedgerRow[]>(`/api/v1/pnl/ledger?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`),
  syncPnl: (payload: { start_date: string; end_date: string }) =>
    request<PnlSyncResult>('/api/v1/pnl/sync', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
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
  updateConfig: (payload: Partial<RiskConfig> & { password: string }) => {
    // GET /config includes display-only model metadata. The PATCH schema
    // deliberately forbids unknown fields, so never echo those values back.
    const updates = { ...payload }
    delete updates.model_name
    delete updates.strategy_profile
    return request<RiskConfig>('/api/v1/config', {
      method: 'PATCH',
      body: JSON.stringify(updates),
    })
  },
  pause: () => request<{ mode: string }>('/api/v1/actions/pause', { method: 'POST' }),
  runCycle: (password: string) => request<{ status: string; operation_id: string }>('/api/v1/actions/run-cycle', {
    method: 'POST',
    body: JSON.stringify({ password }),
  }),
  resumeTestnet: (password: string) =>
    request<{ mode: string }>('/api/v1/actions/resume-testnet', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  reconcilePositions: (password: string) =>
    request<{ mode: string }>('/api/v1/actions/reconcile', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  unlockLive: (password: string) =>
    request<{ mode: string }>('/api/v1/actions/unlock-live', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  flatten: (password: string) =>
    request<{ mode: string }>('/api/v1/actions/emergency-flatten', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  reducePosition: (positionId: string, fraction: number, password: string) =>
    request<OrderRow>('/api/v1/actions/reduce-position', {
      method: 'POST',
      body: JSON.stringify({
        position_id: positionId,
        fraction,
        operation_id: crypto.randomUUID(),
        password,
      }),
    }),
  manualEntry: (payload: ManualEntryDraft & { operation_id: string; password: string }) =>
    request<ManualEntryResult>('/api/v1/actions/manual-entry', {
      method: 'POST',
      body: JSON.stringify(payload),
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
    factor_research_run_id?: string
    backtest_config?: Partial<{
      initial_equity: string
      risk_pct: string
      stop_atr: string
      trailing_atr: string
      fee_rate: string
      slippage_rate: string
      estimated_funding_rate: string
      daily_loss_pct: string
      max_drawdown_pct: string
      portfolio_risk_pct: string
      max_leverage: number
      max_margin_pct: string
      max_positions: number
      max_same_direction: number
      correlation_limit: string
      candidate_count: number
      max_spread_pct: string
      max_abs_funding_rate: string
      max_abs_basis_pct: string
      min_book_depth_usdt: string
      min_listing_days: number
      max_volatility_percentile: string
      entry_direction: 'both' | 'long_only' | 'short_only'
      entry_trigger: 'breakout_or_pullback' | 'breakout_only' | 'pullback_only'
      min_confidence: string
      min_net_reward_risk: string
      min_stop_atr: string
      max_stop_atr: string
      manual_exit_levels_enabled: boolean
      manual_stop_atr: string
      manual_take_profit_atr: string
      strong_trend_entry_override_enabled: boolean
      strong_trend_adx_min: string
      trend_adx_min: string
      volatility_soft_limit_percentile: string
      volatility_hard_limit_percentile: string
      elevated_volatility_risk_multiplier: string
      high_volatility_risk_multiplier: string
    }>
  }) =>
    request<{ id: string; status: string }>('/api/v1/replays', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  replays: () => request<ReplayRun[]>('/api/v1/replays'),
  factorCatalog: () => request<FactorCatalog>('/api/v1/factors/catalog'),
  researchFactors: (payload: FactorResearchRequest) =>
    request<FactorResearchSubmission>('/api/v1/factors/research', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  factorResearchRuns: () => request<FactorResearchRun[]>('/api/v1/factors/research'),
  factorShadowRankings: () => request<FactorShadowRanking[]>('/api/v1/factors/shadow'),
  factorPolicyStatus: () => request<FactorPolicyStatus>('/api/v1/factors/policy'),
}
