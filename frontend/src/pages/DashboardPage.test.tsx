import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DashboardPage from './DashboardPage'

const apiMock = vi.hoisted(() => ({
  dashboard: vi.fn(),
  pause: vi.fn(),
  resumeTestnet: vi.fn(),
  flatten: vi.fn(),
  reconcilePositions: vi.fn(),
  unlockLive: vi.fn(),
  runCycle: vi.fn(),
  config: vi.fn(),
  manualEntry: vi.fn(),
  manualEntryAdvice: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))
vi.mock('../features/dashboard/EquityChart', () => ({ EquityChart: () => <div>equity-chart</div> }))

const dashboard = {
  mode: 'TESTNET',
  environment: 'testnet',
  metrics: { equity: '1000', daily_pnl: '3', drawdown_pct: '0.01', margin_pct: '0.05' },
  equity_curve: [],
  risk_capacity: {
    single_trade: { used: '0.001', limit: '0.0025' },
    portfolio: { used: '0.002', limit: '0.0075' },
    margin: { used: '0.05', limit: '0.2' },
    max_leverage: 3,
    positions: { used: 0, limit: 3 },
  },
  positions: [],
  signals: [],
  portfolio_decisions: [],
  health: { ready: true, components: [], checked_at: new Date().toISOString() },
  uptime_seconds: 60,
}

function renderPage() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><DashboardPage /></QueryClientProvider>)
}

describe('DashboardPage', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.dashboard.mockResolvedValue(dashboard)
    apiMock.pause.mockResolvedValue({ mode: 'PAUSED' })
    apiMock.resumeTestnet.mockResolvedValue({ mode: 'TESTNET' })
    apiMock.flatten.mockResolvedValue({ mode: 'PAUSED' })
    apiMock.runCycle.mockResolvedValue({ status: 'QUEUED', operation_id: 'manual-cycle-1' })
    apiMock.config.mockResolvedValue({ entry_symbols: ['BTCUSDT', 'ETHUSDT'], max_leverage: 3 })
    apiMock.manualEntry.mockResolvedValue({
      operation_id: 'manual-entry-1',
      entry: { symbol: 'BTCUSDT', filled_quantity: '0.01' },
      protection: [{ client_order_id: 'stop' }],
      position: { protected: true },
    })
    apiMock.manualEntryAdvice.mockResolvedValue({
      reply: '趋势一致，但请保持小仓位。',
      action: 'OPEN_LONG',
      confidence: '0.82',
      stop_distance_pct: '1.2',
      tp1_r: '1',
      tp2_r: '2.5',
      leverage: 2,
      risk_notes: ['测试网仍需遵守止损'],
    })
  })

  it('renders operational metrics and pauses entries', async () => {
    renderPage()
    expect(await screen.findByText('1,000.00 USDT')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '解锁实盘' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '暂停开仓' }))
    await waitFor(() => expect(apiMock.pause).toHaveBeenCalledOnce())
  })

  it('expands a recent model decision with market and hard-risk details', async () => {
    apiMock.dashboard.mockResolvedValue({
      ...dashboard,
      signals: [{
        id: 'signal-1',
        symbol: 'BTCUSDT',
        action: 'OPEN_LONG',
        result: 'APPROVED',
        confidence: '0.82',
        entry_min: '99.9',
        entry_max: '100.1',
        invalidation_price: '99',
        target_price: '103',
        horizon_minutes: 240,
        thesis: '趋势延续，回踩后等待15分钟确认。',
        reason_codes: ['trend_aligned'],
        reason_codes_zh: ['多周期趋势一致'],
        risk_flags: [],
        risk_flags_zh: [],
        reason: '本轮允许开多，已满足：多周期趋势一致。',
        reason_zh: '本轮允许开多，已满足：多周期趋势一致。',
        recommendation_zh: '建议按本轮信号执行。',
        ai_advice: '建议按本轮信号执行。',
        expires_at: '2026-08-26T04:30:00Z',
        market_context: {
          timestamp: '2026-08-26T04:15:00Z', mark_price: '100', spread_pct: '0.001', funding_rate: '0.0001',
          open_interest_change_pct: '0.02', atr_15m: '1', adx_1h: '30', trend_1h: 1, trend_4h: 1,
          breakout_15m: 1, pullback_15m: 0, volume_zscore: '2',
        },
        risk_decision: {
          decision_id: 'risk-1', signal_id: 'signal-1', status: 'APPROVED', reasons: ['all_hard_limits_passed'],
          reasons_zh: ['已通过全部硬风控'], capital_base: '1000', risk_amount_usdt: '1', quantity: '1',
          entry_price: '100', stop_price: '99', target_price: '103', leverage: 3, estimated_margin: '33.33',
          net_reward_risk: '2.5', decided_at: '2026-08-26T04:15:01Z',
        },
        created_at: '2026-08-26T04:15:00Z',
      }],
    })
    renderPage()
    const row = await screen.findByRole('button', { name: /BTCUSDT/ })
    fireEvent.click(row)
    expect(await screen.findByText('趋势延续，回踩后等待15分钟确认。')).toBeInTheDocument()
    expect(screen.getByText(/净盈亏比/)).toBeInTheDocument()
    expect(screen.getByText('103')).toBeInTheDocument()
    expect(screen.getByText(/2\.50R/)).toBeInTheDocument()
    expect(screen.getAllByText('标记价格').length).toBeGreaterThan(1)
  })

  it('shows the latest portfolio cycle separately from older legacy signals', async () => {
    apiMock.dashboard.mockResolvedValue({
      ...dashboard,
      signals: [{ symbol: 'OLDUSDT', id: 'old-signal', created_at: '2026-08-31T07:00:00Z' }],
      portfolio_decisions: [{
        id: 'portfolio-1', status: 'APPROVED', market_regime: 'TRENDING',
        portfolio_risk_budget_fraction: '0.4', model_name: 'Qwen/Qwen3.8-27B-FP8',
        prompt_version: 'portfolio-v1.3', created_at: '2026-08-31T08:45:10Z',
        payload: { summary: '最新组合保持小额试探。', expires_at: '2026-08-31T09:00:00Z', allocations: [{
          allocation_id: 'allocation-1', symbol: 'BTCUSDT', target_side: 'LONG', allocation_fraction: '0.2', priority: 1, confidence: '0.8', thesis: '趋势一致',
        }] },
        allocations: [],
      }],
      cycle_status: { state: 'EXECUTED', detail: '组合决策完成', started_at: '2026-08-31T08:45:05Z', finished_at: '2026-08-31T08:46:00Z', snapshots: 1, candidates: 1, signals: 1, approved: 1, executed: 1, failed: false },
    })
    renderPage()
    expect(await screen.findByText('最新组合保持小额试探。')).toBeInTheDocument()
    expect(screen.getByText('Qwen/Qwen3.8-27B-FP8')).toBeInTheDocument()
    expect(screen.queryByText('OLDUSDT')).not.toBeInTheDocument()
  })

  it('distinguishes model intent, hard-risk rejection, and the configured position limit', async () => {
    apiMock.dashboard.mockResolvedValue({
      ...dashboard,
      risk_capacity: {
        ...dashboard.risk_capacity,
        positions: { used: 0, limit: 10 },
      },
      portfolio_decisions: [{
        id: 'portfolio-rejected', status: 'REJECTED', market_regime: 'TRENDING',
        portfolio_risk_budget_fraction: '0.35', model_name: 'Qwen/Qwen3.8-27B-FP8',
        prompt_version: 'portfolio-v1.3', created_at: '2026-09-03T01:13:58Z',
        payload: { summary: '模型建议 ARB 做多。', expires_at: '2026-09-03T01:15:00Z', allocations: [{
          allocation_id: 'allocation-rejected', symbol: 'ARBUSDT', target_side: 'LONG', allocation_fraction: '0.35', priority: 1, confidence: '0.68', thesis: '趋势一致',
        }] },
        allocations: [{
          action_id: 'action-rejected', allocation_id: 'allocation-rejected', symbol: 'ARBUSDT',
          action: 'REJECTED', side: 'LONG', confidence: '0.68', priority: 1,
          status: 'REJECTED', reasons: ['net_reward_risk_below_minimum'],
        }],
      }],
      cycle_status: { state: 'RISK_REJECTED', detail: '模型已返回，但被硬风控拒绝', started_at: '2026-09-03T01:13:50Z', finished_at: '2026-09-03T01:14:10Z', snapshots: 30, candidates: 11, signals: 1, approved: 0, executed: 0, failed: false },
    })
    renderPage()

    expect(await screen.findByText('模型意图被风控拒绝（未下单）')).toBeInTheDocument()
    expect(screen.getByText('模型建议：ARBUSDT · LONG · 35%')).toBeInTheDocument()
    expect(screen.getAllByText('0 / 10')).toHaveLength(2)
  })

  it('shows the current cycle timeout instead of presenting an old signal as latest', async () => {
    apiMock.dashboard.mockResolvedValue({
      ...dashboard,
      signals: [{ symbol: 'OLDUSDT', id: 'old-signal', created_at: '2026-08-31T07:00:00Z' }],
      portfolio_decisions: [],
      cycle_status: {
        state: 'MODEL_TIMEOUT',
        detail: '模型中转请求超时，本轮未生成组合决策',
        started_at: '2026-08-31T08:45:05Z',
        finished_at: '2026-08-31T08:45:50Z',
        snapshots: 30,
        candidates: 16,
        signals: 0,
        approved: 0,
        executed: 0,
        failed: true,
      },
    })
    renderPage()

    expect(await screen.findByText('本轮模型响应超时，未生成组合决策')).toBeInTheDocument()
    expect(screen.getAllByText('模型中转请求超时，本轮未生成组合决策').length).toBeGreaterThan(0)
    expect(screen.queryByText('OLDUSDT')).not.toBeInTheDocument()
  })

  it('requires the operator password to resume testnet execution', async () => {
    apiMock.dashboard.mockResolvedValue({ ...dashboard, mode: 'PAUSED' })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '恢复运行' }))
    expect(screen.getByRole('dialog', { name: '恢复测试网运行' })).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: '确认恢复' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
    fireEvent.click(confirm)

    await waitFor(() => expect(apiMock.resumeTestnet).toHaveBeenCalledWith('operator-password'))
    expect(screen.queryByRole('dialog', { name: '恢复测试网运行' })).not.toBeInTheDocument()
  })

  it('does not queue a cycle while position reconciliation is pending', async () => {
    apiMock.dashboard.mockResolvedValue({
      ...dashboard,
      mode: 'RECONCILIATION_REQUIRED',
      cycle_status: {
        state: 'BLOCKED_RECONCILIATION',
        detail: '仓位对账待确认，本轮未调用模型',
        started_at: null,
        finished_at: null,
        snapshots: 0,
        candidates: 0,
        signals: 0,
        approved: 0,
        executed: 0,
        failed: false,
      },
    })
    renderPage()

    const analyze = await screen.findByRole('button', { name: '完成仓位对账后再分析' })
    expect(analyze).toBeDisabled()
    expect(screen.getByText('需先完成仓位对账，本轮未调用模型')).toBeInTheDocument()
    expect(apiMock.runCycle).not.toHaveBeenCalled()
  })

  it('requires the operator password for emergency flatten', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '紧急清仓' }))
    expect(screen.getByRole('dialog', { name: '紧急清仓' })).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: '立即清仓' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(apiMock.flatten.mock.calls[0][0]).toBe('operator-password'))
  })

  it('requires the operator password before queuing a bounded AI and risk cycle', async () => {
    const view = renderPage()
    fireEvent.click(await within(view.container).findByRole('button', { name: '立即分析并执行' }))
    expect(screen.getByRole('dialog', { name: '立即分析并执行' })).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: '确认分析执行' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(apiMock.runCycle).toHaveBeenCalledWith('operator-password'))
    expect(screen.queryByRole('dialog', { name: '立即分析并执行' })).not.toBeInTheDocument()
  })

  it('requires one password confirmation to resume and queue a paused testnet cycle', async () => {
    apiMock.dashboard.mockResolvedValue({ ...dashboard, mode: 'PAUSED' })
    const view = renderPage()

    fireEvent.click(await within(view.container).findByRole('button', { name: '恢复并分析' }))
    expect(screen.getByRole('dialog', { name: '恢复并分析' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
    fireEvent.click(screen.getByRole('button', { name: '确认恢复并分析' }))

    await waitFor(() => expect(apiMock.runCycle).toHaveBeenCalledWith('operator-password'))
    expect(apiMock.resumeTestnet).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog', { name: '恢复并分析' })).not.toBeInTheDocument()
  })

  it('opens a manual entry desk with a separate AI advice conversation', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '手动开仓' }))
    expect(await screen.findByRole('dialog', { name: '手动开仓' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('向 AI 询问开仓建议'), { target: { value: '现在适合做多吗？' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(apiMock.manualEntryAdvice).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'BTCUSDT', side: 'LONG' })))
    expect(await screen.findByText(/趋势一致/)).toBeInTheDocument()
  })

  it('switches the advice contract from the question and fills every manual parameter', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '手动开仓' }))
    fireEvent.change(screen.getByLabelText('向 AI 询问开仓建议'), { target: { value: '给出 ETH 目前市场的开仓建议' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => expect(apiMock.manualEntryAdvice).toHaveBeenCalledWith(expect.objectContaining({
      symbol: 'ETHUSDT',
      side: 'LONG',
      stop_distance_pct: 1,
      tp1_r: 1,
      tp2_r: 2,
      leverage: 2,
    })))
    fireEvent.click(await screen.findByRole('button', { name: '一键填写到左侧开仓参数' }))

    expect(screen.getByRole('combobox', { name: '合约' })).toHaveValue('ETHUSDT')
    expect(screen.getByRole('combobox', { name: '方向' })).toHaveValue('LONG')
    expect(screen.getByRole('combobox', { name: '杠杆' })).toHaveValue('2')
    expect(screen.getByLabelText('止损距离 (%)')).toHaveValue(1.2)
    expect(screen.getByLabelText('第一止盈 (R)')).toHaveValue(1)
    expect(screen.getByLabelText('第二止盈 (R)')).toHaveValue(2.5)
  })

  it('synchronizes the draft symbol with a restricted entry whitelist', async () => {
    apiMock.config.mockResolvedValue({ entry_symbols: ['ETHUSDT'], max_leverage: 3 })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '手动开仓' }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: '合约' })).toHaveValue('ETHUSDT'))
    fireEvent.change(screen.getByLabelText('向 AI 询问开仓建议'), { target: { value: '分析当前参数' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(apiMock.manualEntryAdvice).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'ETHUSDT' })))
  })

  it('offers manual leverage choices through the configured 30x ceiling', async () => {
    apiMock.config.mockResolvedValue({ entry_symbols: ['BTCUSDT'], max_leverage: 30 })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '手动开仓' }))
    const leverage = await screen.findByRole('combobox', { name: '杠杆' })
    await waitFor(() => expect(within(leverage).getByRole('option', { name: '30× 逐仓' })).toBeInTheDocument())
    fireEvent.change(leverage, { target: { value: '30' } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
    fireEvent.click(screen.getByRole('button', { name: '确认测试网开仓' }))
    await waitFor(() => expect(apiMock.manualEntry).toHaveBeenCalledWith(expect.objectContaining({ leverage: 30, password: 'operator-password' })))
  })

  it('requires the operator password and explicit testnet acknowledgement before manual entry', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '手动开仓' }))
    const submit = await screen.findByRole('button', { name: '确认测试网开仓' })
    expect(submit).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
    fireEvent.click(submit)
    await waitFor(() => expect(apiMock.manualEntry).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'BTCUSDT', side: 'LONG', leverage: 2, password: 'operator-password' })))
    expect(await screen.findByText('仓位已成交并完成保护')).toBeInTheDocument()
  })
})
