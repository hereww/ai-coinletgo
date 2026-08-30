import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { PageHeader } from './PageHeader'

it('exposes the page title as the primary heading', () => {
  render(<PageHeader title="市场" subtitle="行情筛选" />)

  expect(screen.getByRole('heading', { level: 1, name: '市场' })).toBeInTheDocument()
})
