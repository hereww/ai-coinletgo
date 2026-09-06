import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import FactorResearchPage from './FactorResearchPage'

const apiMock = vi.hoisted(() => ({
  factorCatalog: vi.fn(),
  researchFactors: vi.fn(),
  factorResearchRuns: vi.fn(),
  factorShadowRankings: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: apiMock }))

const catalog = {
  factors: Array.from({ length: 12 }, (_, index) => ({
    key: `factor-${index}`,
    label: `因子 ${index}`,
    category: '动量',
    description: '说明',
    required_data: ['price'],
  })),
  data_sources: [
    { key: 'price', label: '价格K线', available: true, detail: '历史K线' },
    { key: 'open_interest', label: '历史OI', available: false, detail: '尚未接入' },
  ],
  market_source: 'Binance USD-M production public market data',
  live_trading_connected: false,
}

const result = {
  generated_at: '2025-04-01T00:00:00Z',
  summary: { factor_count: 12, passed: 1, watch: 5, insufficient: 2, unavailable: 4, timestamps_evaluated: 60, total_observations: 900 },
  factors: [
    {
      key: 'momentum_5d', label: '5日动量', category: '动量', description: '价格动量',
      required_data: ['price'], source_available: true, status: 'PASSED', direction: 'POSITIVE',
      mean_ic: 0.12, ic_std: 0.2, icir: 0.6, positive_ic_rate: 0.65,
      in_sample_ic: 0.1, out_of_sample_ic: 0.14, p_value: 0.01, q_value: 0.03,
      turnover: 0.25, timestamp_count: 60, observation_count: 480,
      decay: [{ forward_bars: 24, mean_ic: 0.12, timestamp_count: 60 }], unavailable_reason: null,
      gates: { minimum_timestamps: true, absolute_ic: true, oos_same_direction: true, fdr_5pct: true },
    },
    {
      key: 'price_oi_state', label: '价格-OI状态', category: '衍生品', description: 'OI组合',
      required_data: ['price', 'open_interest'], source_available: false, status: 'UNAVAILABLE', direction: 'NEGATIVE',
      mean_ic: null, ic_std: null, icir: null, positive_ic_rate: null,
      in_sample_ic: null, out_of_sample_ic: null, p_value: null, q_value: null,
      turnover: null, timestamp_count: 0, observation_count: 0, decay: [], unavailable_reason: '缺少历史OI数据',
      gates: { minimum_timestamps: false, absolute_ic: false, oos_same_direction: false, fdr_5pct: false },
    },
  ],
  methodology: {
    signal_timing: 'bar_close_point_in_time', target: 'cross_sectional_forward_return',
    correlation: 'spearman_rank_ic', normalization: 'cross_sectional_winsorized_zscore',
    validation: 'chronological_half_holdout', multiple_testing: 'benjamini_hochberg',
    icir: 'unannualized_mean_ic_over_sample_std', pass_rule: '统计门槛',
  },
  parameters: {
    symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'], start_date: '2025-01-01', end_date: '2025-03-31',
    interval: '1h', forward_bars: 24, rebalance_bars: 24, winsorize_quantile: '0.05', min_cross_section: 3,
  },
  data_sources: catalog.data_sources,
  market_source: catalog.market_source,
  live_trading_connected: false,
}

const submission = { id: 'run-1', status: 'QUEUED', parameters: result.parameters }
const completedRun = {
  id: submission.id,
  status: 'COMPLETED',
  parameters: result.parameters,
  report: result,
  created_at: '2025-04-01T00:00:00Z',
  completed_at: '2025-04-01T00:01:00Z',
}

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <FactorResearchPage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiMock.factorCatalog.mockReset().mockResolvedValue(catalog)
  apiMock.researchFactors.mockReset().mockResolvedValue(submission)
  apiMock.factorResearchRuns.mockReset().mockResolvedValue([])
  apiMock.factorShadowRankings.mockReset().mockResolvedValue([])
})

afterEach(cleanup)

it('shows catalog data boundaries before a run', async () => {
  renderPage()
  expect(await screen.findByText('价格K线')).toBeInTheDocument()
  expect(screen.getByText('历史OI')).toBeInTheDocument()
  expect(screen.getByText('尚未运行研究')).toBeInTheDocument()
  expect(screen.getByText('12 个候选因子等待评估')).toBeInTheDocument()
})

it('submits normalized symbols and renders statistical evidence', async () => {
  apiMock.factorResearchRuns.mockResolvedValueOnce([]).mockResolvedValue([completedRun])
  renderPage()
  const symbolInput = await screen.findByLabelText('合约列表')
  fireEvent.change(symbolInput, { target: { value: ' btcusdt, ETHUSDT,btcusdt,SOLUSDT ' } })
  fireEvent.click(screen.getByRole('button', { name: '运行因子研究' }))

  await waitFor(() => expect(apiMock.researchFactors).toHaveBeenCalledWith(
    expect.objectContaining({
      symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'],
      interval: '1h',
      forward_bars: 24,
      rebalance_bars: 24,
    }),
    expect.anything(),
  ))
  await waitFor(() => expect(apiMock.factorResearchRuns).toHaveBeenCalledTimes(2))
  expect(await screen.findByText('5日动量')).toBeInTheDocument()
  expect(screen.getByText('价格-OI状态')).toBeInTheDocument()
  expect(screen.getByText('缺少历史OI数据')).toBeInTheDocument()
  expect(screen.getAllByText('0.120')).toHaveLength(2)
  expect(screen.getAllByText('通过')).toHaveLength(2)
  expect(screen.getAllByText('数据缺失')).toHaveLength(2)
})

it('shows a request failure without replacing data coverage', async () => {
  apiMock.researchFactors.mockRejectedValue(new Error('公共行情暂不可用'))
  renderPage()
  await screen.findByText('价格K线')
  fireEvent.click(screen.getByRole('button', { name: '运行因子研究' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('公共行情暂不可用')
  expect(screen.getByText('历史OI')).toBeInTheDocument()
})

it('shows the persisted error when a queued run fails', async () => {
  apiMock.factorResearchRuns.mockResolvedValueOnce([]).mockResolvedValue([{
    ...completedRun,
    status: 'FAILED',
    report: { error: 'BTCUSDT 历史行情获取失败' },
  }])
  renderPage()
  await screen.findByText('价格K线')
  fireEvent.click(screen.getByRole('button', { name: '运行因子研究' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('BTCUSDT 历史行情获取失败')
  expect(screen.getByText('尚未运行研究')).toBeInTheDocument()
})
