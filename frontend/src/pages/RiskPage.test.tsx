import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import RiskPage from './RiskPage'

const apiMock = vi.hoisted(() => ({ config: vi.fn(), updateConfig: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const config = {
  capital_limit_usdt: 1000,
  single_trade_risk_pct: 0.0025,
  portfolio_risk_pct: 0.0075,
  daily_loss_pct: 0.01,
  max_drawdown_pct: 0.05,
  max_leverage: 3,
  max_margin_pct: 0.2,
  max_positions: 3,
  max_same_direction: 2,
  correlation_limit: 0.8,
  entry_direction: 'both',
  entry_trigger: 'breakout_or_pullback',
  candidate_count: 5,
  scan_interval_minutes: 15,
  min_confidence: 0.75,
  min_net_reward_risk: 2,
  min_stop_atr: 0.8,
  max_stop_atr: 2.5,
  entry_symbols: [],
  model_name: 'gpt-5.6',
  model_daily_request_limit: 110,
}

it('confirms editable risk settings with the operator password', async () => {
  apiMock.config.mockResolvedValue(config)
  apiMock.updateConfig.mockResolvedValue({ ...config, capital_limit_usdt: 900 })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RiskPage /></QueryClientProvider>)
  fireEvent.change(await screen.findByLabelText('资金上限 (USDT)'), { target: { value: '900' } })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  expect(await screen.findByRole('dialog', { name: '确认保存开仓策略' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('操作密码'), { target: { value: 'operator-password' } })
  fireEvent.click(screen.getByRole('button', { name: '确认保存' }))
  await waitFor(() => expect(apiMock.updateConfig).toHaveBeenCalledWith(expect.objectContaining({ capital_limit_usdt: 900, password: 'operator-password' })))
  expect(apiMock.updateConfig.mock.calls[0][0]).not.toHaveProperty('confirmation')
})

it('allows the configured leverage ceiling to be raised to 30x', async () => {
  apiMock.config.mockResolvedValue(config)
  apiMock.updateConfig.mockResolvedValue({ ...config, max_leverage: 30 })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RiskPage /></QueryClientProvider>)
  fireEvent.change(await screen.findByLabelText('最高杠杆'), { target: { value: '30' } })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  fireEvent.change(await screen.findByLabelText('操作密码'), { target: { value: 'operator-password' } })
  fireEvent.click(await screen.findByRole('button', { name: '确认保存' }))
  await waitFor(() => expect(apiMock.updateConfig).toHaveBeenCalledWith(expect.objectContaining({ max_leverage: 30 })))
})

it('allows the scan interval to be configured from 15 to 120 minutes', async () => {
  apiMock.config.mockResolvedValue(config)
  apiMock.updateConfig.mockResolvedValue({ ...config, scan_interval_minutes: 30 })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RiskPage /></QueryClientProvider>)
  const interval = await screen.findByLabelText('扫描周期（分钟）')
  expect(interval).toHaveAttribute('min', '15')
  expect(interval).toHaveAttribute('max', '120')
  fireEvent.change(interval, { target: { value: '30' } })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  fireEvent.change(await screen.findByLabelText('操作密码'), { target: { value: 'operator-password' } })
  fireEvent.click(await screen.findByRole('button', { name: '确认保存' }))
  await waitFor(() => expect(apiMock.updateConfig).toHaveBeenCalledWith(expect.objectContaining({ scan_interval_minutes: 30 })))
})

it('allows previously locked risk limits and wide stop ranges to be edited', async () => {
  apiMock.config.mockResolvedValue(config)
  apiMock.updateConfig.mockResolvedValue({
    ...config,
    daily_loss_pct: 0.08,
    max_drawdown_pct: 0.35,
    max_positions: 12,
    max_same_direction: 7,
    correlation_limit: 0.25,
    min_stop_atr: 0.2,
    max_stop_atr: 4.5,
    single_trade_risk_pct: 0.04,
    portfolio_risk_pct: 0.2,
    max_margin_pct: 0.8,
  })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RiskPage /></QueryClientProvider>)

  fireEvent.change(await screen.findByLabelText('日亏损熔断 (%)'), { target: { value: '8' } })
  fireEvent.change(screen.getByLabelText('总回撤熔断 (%)'), { target: { value: '35' } })
  fireEvent.change(screen.getByLabelText('最多仓位'), { target: { value: '12' } })
  fireEvent.change(screen.getByLabelText('同向最多仓位'), { target: { value: '7' } })
  fireEvent.change(screen.getByLabelText('相关性阈值 (0–1)'), { target: { value: '0.25' } })
  fireEvent.change(screen.getByLabelText('最小止损距离 (ATR)'), { target: { value: '0.2' } })
  fireEvent.change(screen.getByLabelText('最大止损距离 (ATR)'), { target: { value: '4.5' } })
  fireEvent.change(screen.getByLabelText('单笔风险 (%)'), { target: { value: '4' } })
  fireEvent.change(screen.getByLabelText('组合风险 (%)'), { target: { value: '20' } })
  fireEvent.change(screen.getByLabelText('保证金上限 (%)'), { target: { value: '80' } })

  expect(screen.getByLabelText('日亏损熔断 (%)')).not.toBeDisabled()
  expect(screen.getByLabelText('最多仓位')).not.toBeDisabled()
  expect(screen.getByLabelText('日亏损熔断 (%)')).not.toHaveAttribute('max')
  expect(screen.getByLabelText('总回撤熔断 (%)')).not.toHaveAttribute('max')
  expect(screen.getByLabelText('最多仓位')).not.toHaveAttribute('max')
  expect(screen.getByLabelText('最小止损距离 (ATR)')).not.toHaveAttribute('min')
  expect(screen.getByLabelText('最大止损距离 (ATR)')).not.toHaveAttribute('max')
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  fireEvent.change(await screen.findByLabelText('操作密码'), { target: { value: 'operator-password' } })
  fireEvent.click(await screen.findByRole('button', { name: '确认保存' }))
  await waitFor(() => expect(apiMock.updateConfig).toHaveBeenCalledWith(expect.objectContaining({
    daily_loss_pct: 0.08,
    max_drawdown_pct: 0.35,
    max_positions: 12,
    max_same_direction: 7,
    correlation_limit: 0.25,
    min_stop_atr: 0.2,
    max_stop_atr: 4.5,
    single_trade_risk_pct: 0.04,
    portfolio_risk_pct: 0.2,
    max_margin_pct: 0.8,
  })))
})

it('blocks saving when the minimum stop exceeds the maximum stop', async () => {
  apiMock.config.mockResolvedValue(config)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RiskPage /></QueryClientProvider>)
  fireEvent.change(await screen.findByLabelText('最小止损距离 (ATR)'), { target: { value: '4.5' } })
  fireEvent.change(screen.getByLabelText('最大止损距离 (ATR)'), { target: { value: '0.2' } })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('最小止损距离不能大于最大止损距离')
  expect(apiMock.updateConfig).not.toHaveBeenCalled()
})
