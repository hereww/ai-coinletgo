import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DashboardPage from './DashboardPage'

const apiMock = vi.hoisted(() => ({
  dashboard: vi.fn(),
  pause: vi.fn(),
  resumeTestnet: vi.fn(),
  flatten: vi.fn(),
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

  it('resumes directly from paused mode without a confirmation dialog', async () => {
    apiMock.dashboard.mockResolvedValue({ ...dashboard, mode: 'PAUSED' })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: '恢复运行' }))

    await waitFor(() => expect(apiMock.resumeTestnet).toHaveBeenCalledOnce())
    expect(apiMock.resumeTestnet).toHaveBeenCalledWith()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('does not queue a cycle while reconciliation takeover is pending', async () => {
    apiMock.dashboard.mockResolvedValue({
      ...dashboard,
      mode: 'RECONCILIATION_REQUIRED',
      cycle_status: {
        state: 'BLOCKED_RECONCILIATION',
        detail: '仓位对账待人工接管，本轮未调用模型',
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

    const analyze = await screen.findByRole('button', { name: '接管后再分析' })
    expect(analyze).toBeDisabled()
    expect(screen.getByText('需先完成仓位接管，本轮未调用模型')).toBeInTheDocument()
    expect(apiMock.runCycle).not.toHaveBeenCalled()
  })

  it('requires typed confirmation and totp for emergency flatten', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '紧急清仓' }))
    const dialog = screen.getByRole('dialog', { name: '紧急清仓' })
    const confirm = screen.getByRole('button', { name: '立即清仓' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('输入 FLATTEN 确认'), { target: { value: 'FLATTEN' } })
    fireEvent.change(screen.getByLabelText('TOTP 验证码'), { target: { value: '123456' } })
    expect(dialog).toBeInTheDocument()
    fireEvent.click(confirm)
    await waitFor(() => expect(apiMock.flatten.mock.calls[0][0]).toBe('123456'))
  })

  it('queues a bounded AI and risk cycle directly from the dashboard', async () => {
    const view = renderPage()
    fireEvent.click(await within(view.container).findByRole('button', { name: '立即分析并执行' }))
    await waitFor(() => expect(apiMock.runCycle).toHaveBeenCalledOnce())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('resumes a paused testnet before queuing the cycle', async () => {
    apiMock.dashboard.mockResolvedValue({ ...dashboard, mode: 'PAUSED' })
    const view = renderPage()

    fireEvent.click(await within(view.container).findByRole('button', { name: '恢复并分析' }))

    await waitFor(() => {
      expect(apiMock.resumeTestnet).toHaveBeenCalledOnce()
      expect(apiMock.runCycle).toHaveBeenCalledOnce()
    })
    expect(apiMock.runCycle.mock.invocationCallOrder[0]).toBeGreaterThan(apiMock.resumeTestnet.mock.invocationCallOrder[0])
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
    fireEvent.click(screen.getByRole('button', { name: '确认测试网开仓' }))
    await waitFor(() => expect(apiMock.manualEntry).toHaveBeenCalledWith(expect.objectContaining({ leverage: 30 })))
  })

  it('requires an explicit testnet acknowledgement before manual entry', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '手动开仓' }))
    const submit = await screen.findByRole('button', { name: '确认测试网开仓' })
    expect(submit).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(submit)
    await waitFor(() => expect(apiMock.manualEntry).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'BTCUSDT', side: 'LONG', leverage: 2 })))
    expect(await screen.findByText('仓位已成交并完成保护')).toBeInTheDocument()
  })
})
