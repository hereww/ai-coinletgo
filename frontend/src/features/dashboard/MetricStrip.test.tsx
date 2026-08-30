import { render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { MetricStrip } from './MetricStrip'

const baseMetrics = {
  equity: '1000',
  daily_pnl: '1',
  drawdown_pct: '0.01',
  margin_pct: '0.05',
}

function fundingValue() {
  return within(screen.getByText('资金费').closest('.metric-cell') as HTMLElement).getByText(/USDT/)
}

it('uses income and expense colors for funding', () => {
  const view = render(<MetricStrip metrics={{ ...baseMetrics, funding_today: '2.5' }} />)
  expect(fundingValue()).toHaveClass('positive')

  view.rerender(<MetricStrip metrics={{ ...baseMetrics, funding_today: '-2.5' }} />)
  expect(fundingValue()).toHaveClass('negative')
})
