import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { LoginPage } from './LoginPage'

const apiMock = vi.hoisted(() => ({ login: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock, apiBaseUrl: '' }))

it('logs in with the configured username and password', async () => {
  apiMock.login.mockResolvedValue({ username: 'admin', csrf_token: 'csrf' })
  const onSuccess = vi.fn()
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LoginPage onSuccess={onSuccess} />
    </QueryClientProvider>,
  )

  expect(screen.getByLabelText('用户名')).toHaveValue('admin')
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } })
  fireEvent.click(screen.getByRole('button', { name: '登录控制台' }))

  await waitFor(() => expect(apiMock.login).toHaveBeenCalledWith('admin', 'test-password'))
  await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce())
})
