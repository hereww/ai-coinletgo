import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import SignalsPage from './SignalsPage'

const apiMock = vi.hoisted(() => ({ signals: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))

describe('SignalsPage', () => {
  it('shows unknown rejection states and expands legacy records safely', async () => {
    apiMock.signals.mockResolvedValue([{
      id: 'signal-legacy', symbol: 'ETHUSDT', action: 'OPEN_SHORT', result: 'REJECTED_UNKNOWN_SYMBOL', confidence: '0.55',
      entry_min: null, entry_max: null, invalidation_price: null, target_price: null, horizon_minutes: 240,
      thesis: '', reason_codes: [], reason_codes_zh: [], risk_flags: [], risk_flags_zh: [],
      reason: '本轮未开仓，信号未通过确定性硬风控。', reason_zh: '本轮未开仓，信号未通过确定性硬风控。',
      recommendation_zh: '建议等待下一轮周期。', ai_advice: '建议等待下一轮周期。', expires_at: '',
      market_context: null, risk_decision: null, created_at: '2026-08-26T04:15:00Z',
    }])
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignalsPage /></QueryClientProvider>)
    expect(await screen.findByText('已拒绝')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(await screen.findByText('该历史记录未保存决策时的行情快照。')).toBeInTheDocument()
    expect(screen.getByText('该历史记录未保存硬风控结论。')).toBeInTheDocument()
  })
})
