export type SystemMode =
  | 'TESTNET'
  | 'LIVE_LOCKED'
  | 'LIVE_ENABLED'
  | 'PAUSED'
  | 'RISK_HALTED'
  | 'RECONCILIATION_REQUIRED'

export type HealthState = 'HEALTHY' | 'DEGRADED' | 'FAILED' | 'NOT_CONFIGURED'

export interface HealthComponent {
  name: string
  state: HealthState
  latency_ms: number | null
  detail: string | null
  checked_at: string
}

export interface Position {
  position_id: string
  symbol: string
  side: 'LONG' | 'SHORT'
  quantity: string
  entry_price: string
  mark_price: string
  stop_price: string
  tp1_price?: string | null
  tp2_price?: string | null
  unrealized_pnl: string
  current_r: string
  protected: boolean
  opened_at: string
}

export interface SignalMarketContext {
  timestamp: string
  mark_price: string
  spread_pct: string
  funding_rate: string
  open_interest_change_pct: string
  atr_15m: string
  adx_1h: string
  trend_1h: -1 | 0 | 1
  trend_4h: -1 | 0 | 1
  breakout_15m: -1 | 0 | 1
  pullback_15m: -1 | 0 | 1
  volume_zscore: string
  market_regime?: 'TRENDING' | 'RANGING' | 'VOLATILE' | 'UNCERTAIN'
  volatility_percentile?: string
  volatility_risk_multiplier?: string
}

export interface RiskDecision {
  decision_id: string
  signal_id: string
  status: 'APPROVED' | 'REJECTED'
  reasons: string[]
  reasons_zh: string[]
  capital_base: string
  risk_amount_usdt: string
  quantity: string
  entry_price: string
  stop_price: string
  target_price: string
  leverage: number
  estimated_margin: string
  net_reward_risk: string
  risk_multiplier?: string
  decided_at: string
}

export interface Signal {
  id: string
  symbol: string
  action: 'OPEN_LONG' | 'OPEN_SHORT' | 'NO_TRADE'
  result: string
  confidence: string
  entry_min: string | null
  entry_max: string | null
  invalidation_price: string | null
  target_price: string | null
  horizon_minutes: number
  thesis: string
  reason_codes: string[]
  reason: string
  reason_zh?: string
  reason_codes_zh: string[]
  risk_flags: string[]
  risk_flags_zh: string[]
  recommendation_zh?: string
  ai_advice?: string
  expires_at: string | null
  market_context: SignalMarketContext | null
  risk_decision: RiskDecision | null
  created_at: string
}

export interface DashboardData {
  mode: SystemMode
  environment: 'testnet' | 'live'
  entries_enabled: boolean
  halt_reason: string | null
  mode_updated_at: string | null
  metrics: {
    equity: string
    daily_pnl: string
    realized_pnl: string
    fees_today: string
    funding_today: string
    drawdown_pct: string
    margin_pct: string
  }
  equity_curve: Array<{ time: string; equity: string; drawdown: string }>
  risk_capacity: {
    single_trade: { used: string; limit: string }
    portfolio: { used: string; limit: string }
    margin: { used: string; limit: string }
    max_leverage: number
    positions: { used: number; limit: number }
  }
  positions: Position[]
  signals: Signal[]
  /** Portfolio-v1 records shown ahead of legacy signal history. */
  portfolio_decisions?: PortfolioDecision[]
  cycle_status: {
    state: 'RUNNING' | 'COMPLETED' | 'EXECUTED' | 'NO_CANDIDATES' | 'RISK_REJECTED' | 'MODEL_UNAVAILABLE' | 'MODEL_TIMEOUT' | 'MODEL_THROTTLED' | 'BLOCKED_RECONCILIATION' | 'EXCHANGE_UNAVAILABLE' | 'WORKER_BUSY' | 'WORKER_INTERRUPTED' | 'FAILED' | 'UNKNOWN'
    detail: string
    started_at: string | null
    finished_at: string | null
    snapshots: number
    candidates: number
    signals: number
    approved: number
    executed: number
    failed: boolean
    exchange_unavailable?: boolean
  }
  health: {
    ready: boolean
    components: HealthComponent[]
    checked_at: string
  }
  uptime_seconds: number
}

export interface RiskConfig {
  capital_limit_usdt: number
  single_trade_risk_pct: number
  portfolio_risk_pct: number
  daily_loss_pct: number
  max_drawdown_pct: number
  max_leverage: number
  max_margin_pct: number
  max_positions: number
  max_same_direction: number
  correlation_limit: number
  entry_direction: 'both' | 'long_only' | 'short_only'
  entry_trigger: 'breakout_or_pullback' | 'breakout_only' | 'pullback_only'
  candidate_count: number
  scan_interval_minutes: number
  model_strategy_enabled: boolean
  historical_research_enabled: boolean
  min_confidence: number
  min_net_reward_risk: number
  min_stop_atr: number
  max_stop_atr: number
  manual_exit_levels_enabled: boolean
  manual_stop_atr: number
  manual_take_profit_atr: number
  model_primary_portfolio_enabled: boolean
  strong_trend_entry_override_enabled: boolean
  strong_trend_adx_min: number
  trend_adx_min?: number
  volatility_soft_limit_percentile?: number
  volatility_hard_limit_percentile?: number
  elevated_volatility_risk_multiplier?: number
  high_volatility_risk_multiplier?: number
  entry_symbols: string[]
  model_name: string
  strategy_profile?: 'conservative' | 'balanced' | 'trend_following' | 'scalping'
  portfolio_strategy_enabled: boolean
  portfolio_rebalance_deadband_fraction: number
  portfolio_rebalance_cooldown_minutes: number
  factor_policy_enabled: boolean
  factor_rank_weight: number
  factor_min_risk_multiplier: number
  factor_promotion_windows: number
  hft_enabled: boolean
  hft_dry_run: boolean
  hft_symbols: string[]
  hft_event_interval_ms: number
  hft_max_spread_pct: number
  hft_min_depth_usdt: number
  hft_order_notional_usdt: number
  hft_max_inventory_usdt: number
  hft_cooldown_seconds: number
  hft_market_stale_seconds: number
  hft_max_consecutive_losses: number
  hft_imbalance_threshold: number
}

export interface PortfolioExecution {
  status: string
  order_ids: string[]
  detail: string | null
}

export interface PortfolioAllocation {
  action_id: string
  allocation_id: string | null
  symbol: string
  /** Compiled action; absent when only the raw AI intent was persisted. */
  action?: 'OPEN' | 'ADD' | 'REDUCE' | 'CLOSE' | 'HOLD' | 'TIGHTEN_STOP' | 'REJECTED'
  side?: 'LONG' | 'SHORT' | null
  current_quantity?: string
  target_quantity?: string
  quantity_delta?: string
  target_risk_usdt?: string
  confidence: string
  priority: number
  reasons?: string[]
  reason_codes?: string[]
  risk_flags?: string[]
  target_side?: 'LONG' | 'SHORT' | 'FLAT'
  allocation_fraction?: string
  entry_min?: string | null
  entry_max?: string | null
  stop_price?: string | null
  target_price?: string | null
  thesis?: string
  status: string
  execution?: PortfolioExecution | null
}

export interface PortfolioDecision {
  id: string
  status: string
  market_regime: 'TRENDING' | 'RANGING' | 'VOLATILE' | 'UNCERTAIN'
  portfolio_risk_budget_fraction: string
  payload: {
    summary: string
    expires_at: string
    allocations: Array<{
      allocation_id: string
      symbol: string
      target_side: 'LONG' | 'SHORT' | 'FLAT'
      allocation_fraction: string
      priority: number
      confidence: string
      thesis: string
    }>
  }
  prompt_version: string
  model_name: string
  created_at: string
  allocations: PortfolioAllocation[]
}

export interface AuditEvent {
  id: string
  actor: string
  action: string
  resource: string
  outcome: string
  detail: Record<string, unknown>
  ip_address: string | null
  created_at: string
}

export interface MarketRow {
  symbol: string
  mark_price: string
  spread_pct: string
  quote_volume_24h: string
  funding_rate: string
  open_interest_change_pct: string
  atr_15m: string
  adx_1h: string
  trend_1h: -1 | 0 | 1
  trend_4h: -1 | 0 | 1
  breakout_15m: -1 | 0 | 1
  pullback_15m?: -1 | 0 | 1
  volume_zscore: string
  market_regime?: 'TRENDING' | 'RANGING' | 'VOLATILE' | 'UNCERTAIN'
  volatility_percentile?: string
  volatility_risk_multiplier?: string
  score: string
  timestamp: string
}

export interface OrderRow {
  client_order_id: string
  exchange_order_id: string | null
  symbol: string
  side: string
  position_side: 'LONG' | 'SHORT'
  order_type: string
  quantity: string
  filled_quantity: string
  average_price: string
  price: string | null
  stop_price: string | null
  status: string
  portfolio_decision_id?: string | null
  portfolio_allocation_id?: string | null
  action_sequence?: number | null
  updated_at: string
}

/** A realized-income grouping for one Binance trade identifier, not a position lifecycle. */
export interface TradePnlRow {
  symbol: string
  trade_id: string
  asset: string
  realized_pnl: string
  commission: string
  funding_fee: string
  net_pnl: string
  event_count: number
  first_event_at: string
  last_event_at: string
}

export interface DailyPnlRow {
  date: string
  asset: string
  realized_pnl: string
  commission: string
  funding_fee: string
  net_pnl: string
  event_count: number
}

export interface IncomeLedgerRow {
  income_id: string
  symbol: string
  income_type: string
  income: string
  asset: string
  trade_id: string | null
  event_time: string
}

export interface PnlSyncResult {
  start_date: string
  end_date: string
  fetched_rows: number
  inserted_rows: number
}

export interface ReplayRun {
  id: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
  parameters: {
    mode?: 'deterministic' | 'recorded_portfolio'
    symbols?: string[]
    start_date?: string | null
    end_date?: string | null
    portfolio_decision_id?: string | null
    factor_research_run_id?: string | null
    backtest_config?: Record<string, string | number | null>
  }
  metrics: Record<string, unknown>
  created_at: string
  completed_at: string | null
}

export interface FactorDefinition {
  key: string
  label: string
  category: string
  description: string
  required_data: string[]
}

export interface FactorDataSource {
  key: string
  label: string
  available: boolean
  detail: string
}

export interface FactorCatalog {
  factors: FactorDefinition[]
  data_sources: FactorDataSource[]
  market_source: string
  live_trading_connected: false
  historical_research_enabled: boolean
}

export interface FactorResearchRequest {
  symbols: string[]
  start_date: string
  end_date: string
  interval: '1h' | '4h'
  forward_bars: number
  rebalance_bars: number
  winsorize_quantile: string
  min_cross_section: number
  maker_fee_rate?: string
  taker_fee_rate?: string
  slippage_rate?: string
  funding_rate_fallback?: string
  walk_forward_folds?: number
  portfolio_quantile?: string
}

export interface FactorResearchResult {
  generated_at: string
  summary: {
    factor_count: number
    passed: number
    watch: number
    insufficient: number
    unavailable: number
    timestamps_evaluated: number
    total_observations: number
  }
  factors: Array<{
    key: string
    label: string
    category: string
    description: string
    required_data: string[]
    source_available: boolean
    status: 'PASSED' | 'WATCH' | 'INSUFFICIENT' | 'UNAVAILABLE'
    direction: 'POSITIVE' | 'NEGATIVE'
    mean_ic: number | null
    ic_std: number | null
    icir: number | null
    positive_ic_rate: number | null
    in_sample_ic: number | null
    out_of_sample_ic: number | null
    p_value: number | null
    q_value: number | null
    turnover: number | null
    timestamp_count: number
    observation_count: number
    decay: Array<{ forward_bars: number; mean_ic: number | null; timestamp_count: number }>
    walk_forward_oos_ic?: number | null
    portfolio?: FactorPortfolioMetrics | null
    portfolio_maker?: FactorPortfolioMetrics | null
    walk_forward_portfolio?: FactorPortfolioMetrics | null
    walk_forward?: Array<Record<string, unknown>>
    unavailable_reason: string | null
    gates: Record<string, boolean>
  }>
  methodology: {
    signal_timing: string
    target: string
    correlation: string
    normalization: string
    validation: string
    multiple_testing: string
    icir: string
    portfolio?: string
    cost_model?: string
    maker_fee_rate?: string
    taker_fee_rate?: string
    slippage_rate?: string
    funding_rate_fallback?: string
    portfolio_quantile?: string
    pass_rule: string
  }
  parameters: FactorResearchRequest
  data_sources: FactorDataSource[]
  market_source: string
  live_trading_connected: false
}

export interface FactorPortfolioMetrics {
  observations: number
  gross_return: number | null
  fee_cost: number | null
  slippage_cost: number | null
  funding_cost: number | null
  net_return: number | null
  average_turnover: number | null
  total_turnover: number | null
  max_drawdown: number | null
  annualized_volatility: number | null
  sharpe: number | null
}

export type FactorResearchStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'

export interface FactorResearchSubmission {
  id: string
  status: FactorResearchStatus
  parameters: FactorResearchRequest
}

export interface FactorResearchRun {
  id: string
  status: FactorResearchStatus
  parameters: FactorResearchRequest
  report: FactorResearchResult | { error?: string }
  created_at: string
  completed_at: string | null
}

export interface FactorShadowRanking {
  id: string
  research_run_id: string
  timestamp: string
  payload: {
    research_run_id: string
    generated_at: string
    selected_factors: Array<{ key: string; label: string; direction: string }>
    rankings: Array<{
      symbol: string
      baseline_score: string
      baseline_percentile: string
      factor_score: string
      factor_percentile: string
      combined_score: string
      factor_coverage: string
      contributions: Record<string, string>
      risk_multiplier: string
      baseline_rank: number
      factor_rank: number
      combined_rank: number
      rank_change: number
    }>
    window_start?: string
    window_end?: string
    window_created?: boolean
    promotion?: FactorPromotionMetrics
    execution_effect: string
  }
  created_at: string
}

export interface FactorPolicyVersion {
  id: string
  research_run_id: string
  status: 'ACTIVE' | 'SHADOW' | 'RETIRED' | 'REPLACED'
  frozen_snapshot: {
    completed_at?: string | null
    factors: Array<{ key: string; label: string; direction: 'POSITIVE' | 'NEGATIVE' }>
    parameters: Record<string, unknown>
  }
  activated_at: string | null
  created_at: string
  updated_at: string
}

export interface FactorPromotionMetrics {
  matured_windows: number
  required_windows: number
  progress: string
  oriented_mean_ic: string | null
  shadow_net_return: string
  baseline_net_return: string
  shadow_max_drawdown: string
  baseline_max_drawdown: string
  candidate_turnover: string
  gates: Record<string, boolean>
  failure_reasons: string[]
  eligible_for_promotion: boolean
}

export interface FactorPolicyStatus {
  enabled: boolean
  historical_research_enabled: boolean
  environment: 'testnet' | 'live'
  rank_weight: number
  minimum_risk_multiplier: number
  promotion_windows: number
  active: FactorPolicyVersion | null
  shadow: FactorPolicyVersion | null
  promotion: FactorPromotionMetrics
  windows: Array<{
    id: string
    status: 'PENDING' | 'MATURED' | 'INVALID'
    window_start: string
    window_end: string
    result: Record<string, unknown>
  }>
}

export interface IntegrationStatus {
  proxy: {
    enabled: boolean
    configured: boolean
    scope: string
    detail: string
  }
  binance: {
    environment: 'testnet' | 'live'
    configured: boolean
    base_url: string
    health: HealthComponent
  }
  model: {
    configured: boolean
    active_profile: 'relay' | 'vllm'
    active_label: string
    profiles: Array<{
      id: 'relay' | 'vllm'
      label: string
      kind: 'relay' | 'self_hosted'
      base_url: string | null
      model_name: string
      api_key_configured: boolean
      configured: boolean
      active: boolean
    }>
    base_url: string | null
    model_name: string
    reasoning_effort: 'none' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
    timeout_seconds: number
    strategy_profile: 'conservative' | 'balanced' | 'trend_following' | 'scalping'
    api_key_configured: boolean
    health: HealthComponent
  }
}

export interface ManualEntryDraft {
  symbol: string
  side: 'LONG' | 'SHORT'
  leverage: number
  stop_distance_pct: number
  tp1_r: number
  tp2_r: number
}

export interface ManualEntryAdvice {
  reply: string
  action: 'NO_TRADE' | 'OPEN_LONG' | 'OPEN_SHORT'
  confidence: string
  stop_distance_pct: string | null
  tp1_r: string | null
  tp2_r: string | null
  leverage: number | null
  risk_notes: string[]
}

export interface ManualEntryResult {
  operation_id: string
  entry: OrderRow
  protection: OrderRow[]
  position: Position | null
}
