import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import PositionsPage from './PositionsPage'

const apiMock = vi.hoisted(() => ({ positions: vi.fn(), reducePosition: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))

it('confirms a partial reduction with a fixed fraction and totp', async () => {
  apiMock.positions.mockResolvedValue([{
    position_id: 'binance-BTCUSDT-LONG', symbol: 'BTCUSDT', side: 'LONG', quantity: '1',
    entry_price: '100', mark_price: '101', stop_price: '99', unrealized_pnl: '1',
    current_r: '1', protected: true, opened_at: new Date().toISOString(),
  }])
  apiMock.reducePosition.mockResolvedValue({})
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><PositionsPage /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: '减仓' }))
  fireEvent.change(screen.getByLabelText('减仓比例'), { target: { value: '0.25' } })
  fireEvent.change(screen.getByLabelText('输入 REDUCE 确认'), { target: { value: 'REDUCE' } })
  fireEvent.change(screen.getByLabelText('TOTP 验证码'), { target: { value: '654321' } })
  fireEvent.click(screen.getByRole('button', { name: '确认减仓' }))
  await waitFor(() => expect(apiMock.reducePosition).toHaveBeenCalledWith('binance-BTCUSDT-LONG', 0.25, '654321'))
})
