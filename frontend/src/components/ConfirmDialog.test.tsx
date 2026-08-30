import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { ConfirmDialog } from './ConfirmDialog'

it('traps focus, closes with Escape, and announces errors', () => {
  const onClose = vi.fn()
  render(
    <ConfirmDialog
      open
      title="确认操作"
      body="请确认"
      confirmLabel="执行"
      requireTotp
      error="操作失败"
      onClose={onClose}
      onConfirm={vi.fn()}
    />,
  )
  const dialog = screen.getByRole('dialog', { name: '确认操作' })
  expect(screen.getByRole('alert')).toHaveTextContent('操作失败')
  fireEvent.keyDown(dialog, { key: 'Escape' })
  expect(onClose).toHaveBeenCalledOnce()
})
