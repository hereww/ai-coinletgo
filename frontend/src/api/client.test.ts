import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

it('reports a network connection failure with its API endpoint', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network failed')))

  await expect(api.me()).rejects.toMatchObject({
    name: 'ApiError',
    endpoint: expect.stringContaining('/api/v1/auth/me'),
  } satisfies Partial<ApiError>)
})

it('notifies the app when an authenticated business request loses its session', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Session expired' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  })))
  const unauthorized = vi.fn()
  window.addEventListener('frc:unauthorized', unauthorized, { once: true })

  await expect(api.dashboard()).rejects.toMatchObject({ status: 401 })
  expect(unauthorized).toHaveBeenCalledOnce()
})

it('does not loop the login bootstrap when auth status is unauthorized', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Authentication required' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  })))
  const unauthorized = vi.fn()
  window.addEventListener('frc:unauthorized', unauthorized, { once: true })

  await expect(api.me()).rejects.toMatchObject({ status: 401 })
  expect(unauthorized).not.toHaveBeenCalled()
})
