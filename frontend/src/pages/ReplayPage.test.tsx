import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import ReplayPage from './ReplayPage'

const apiMock = vi.hoisted(() => ({ replays: vi.fn(), createReplay: vi.fn(), portfolioDecisions: vi.fn(), factorResearchRuns: vi.fn(), config: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))

it('shows replay task status and summary metrics', async () => {
  apiMock.config.mockResolvedValue({ historical_research_enabled: true })
  apiMock.replays.mockResolvedValue([{
    id: 'replay-1', status: 'COMPLETED',
    parameters: { mode: 'deterministic', symbols: ['BTCUSDT'], start_date: '2025-01-01', end_date: '2025-02-01' },
    metrics: { summary: { portfolio_net_return_pct: '0.05', max_drawdown_pct: '0.02', total_trades: 12, symbols_tested: 1 } },
    created_at: new Date().toISOString(), completed_at: new Date().toISOString(),
  }])
  apiMock.portfolioDecisions.mockResolvedValue([])
  apiMock.factorResearchRuns.mockResolvedValue([])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ReplayPage /></QueryClientProvider>)
  expect(await screen.findAllByText('COMPLETED')).not.toHaveLength(0)
  expect(screen.getByText('5.00%')).toBeInTheDocument()
  expect(screen.getByText('2.00%')).toBeInTheDocument()
})

it('labels recorded portfolio verification separately from a performance replay', async () => {
  apiMock.config.mockResolvedValue({ historical_research_enabled: true })
  apiMock.replays.mockResolvedValue([{
    id: 'replay-2', status: 'COMPLETED',
    parameters: { mode: 'recorded_portfolio', portfolio_decision_id: 'decision-12345678' },
    metrics: { summary: { compiler_plan_match: true, planned_actions: 2 } },
    created_at: new Date().toISOString(), completed_at: new Date().toISOString(),
  }])
  apiMock.portfolioDecisions.mockResolvedValue([])
  apiMock.factorResearchRuns.mockResolvedValue([])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ReplayPage /></QueryClientProvider>)
  expect(await screen.findAllByText('决策复现')).not.toHaveLength(0)
  expect(screen.getAllByText('一致')).not.toHaveLength(0)
})
