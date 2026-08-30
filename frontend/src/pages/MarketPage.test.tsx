import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import MarketPage from './MarketPage'

const apiMock = vi.hoisted(() => ({ market: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))

it('keeps the page heading while market data is loading', () => {
  apiMock.market.mockReturnValue(new Promise(() => undefined))

  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MarketPage />
    </QueryClientProvider>,
  )

  expect(screen.getByRole('heading', { level: 1, name: '市场' })).toBeInTheDocument()
  expect(screen.getByText('正在同步数据...')).toBeInTheDocument()
})
