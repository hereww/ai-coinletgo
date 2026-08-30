import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import PortfolioPage from './PortfolioPage'

const apiMock = vi.hoisted(() => ({ portfolioDecisions: vi.fn(), cycleStatus: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))

it('shows the model target, compiled action and linked execution outcome', async () => {
  apiMock.cycleStatus.mockResolvedValue({ state: 'EXECUTED', detail: '测试状态', snapshots: 1, candidates: 1, signals: 1, executed: 1 })
  apiMock.portfolioDecisions.mockResolvedValue([{
    id: 'decision-1', status: 'APPROVED', market_regime: 'TRENDING',
    portfolio_risk_budget_fraction: '0.8', model_name: 'portfolio-model',
    prompt_version: 'portfolio-v1', created_at: new Date().toISOString(),
    payload: {
      summary: '趋势延续，控制组合风险。', expires_at: '2026-08-27T12:00:00+00:00',
      allocations: [{ allocation_id: 'allocation-1', symbol: 'BTCUSDT', target_side: 'LONG', allocation_fraction: '0.5' }],
    },
    allocations: [{
      action_id: 'action-1', allocation_id: 'allocation-1', symbol: 'BTCUSDT', action: 'OPEN', side: 'LONG',
      current_quantity: '0', target_quantity: '2', quantity_delta: '2', target_risk_usdt: '4', confidence: '0.9', priority: 1,
      reasons: ['portfolio_target_approved'], status: 'EXECUTED',
      execution: { status: 'EXECUTED', order_ids: ['portfolio-order-1'], detail: null },
    }],
  }])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><PortfolioPage /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: '展开' }))
  expect(screen.getByText('趋势延续，控制组合风险。')).toBeInTheDocument()
  expect(screen.getAllByText('BTCUSDT').length).toBeGreaterThan(0)
  expect(screen.getAllByText('LONG').length).toBeGreaterThan(0)
  expect(screen.getAllByText(/portfolio-order-1/).length).toBeGreaterThan(0)
})
