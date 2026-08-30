import { BrowserRouter } from 'react-router-dom'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { AppShell } from './AppShell'

it('opens mobile navigation, closes with Escape, and restores focus', () => {
  render(<BrowserRouter><AppShell user="operator" environment="live"><div>content</div></AppShell></BrowserRouter>)
  expect(screen.getByText('实盘环境')).toBeInTheDocument()
  const menuButton = screen.getByRole('button', { name: '打开导航' })
  fireEvent.click(menuButton)
  expect(menuButton).toHaveAttribute('aria-expanded', 'true')
  const sidebar = screen.getByRole('navigation', { name: '主导航' }).closest('aside')
  expect(within(sidebar as HTMLElement).getByRole('button', { name: '关闭导航' })).toHaveFocus()
  expect(sidebar).toHaveClass('sidebar-open')

  fireEvent.keyDown(window, { key: 'Escape' })
  expect(sidebar).not.toHaveClass('sidebar-open')
  expect(menuButton).toHaveAttribute('aria-expanded', 'false')
  expect(menuButton).toHaveFocus()
})
