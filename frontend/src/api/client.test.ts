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

it('formats FastAPI validation details instead of rendering object placeholders', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: [
      { type: 'less_than_equal', loc: ['body', 'max_leverage'], msg: 'Input should be less than or equal to 30', input: 31 },
      { type: 'greater_than_equal', loc: ['body', 'scan_interval_minutes'], msg: 'Input should be greater than or equal to 5', input: 4 },
    ],
  }), {
    status: 422,
    headers: { 'Content-Type': 'application/json' },
  })))

  await expect(api.dashboard()).rejects.toMatchObject({
    message: '最高杠杆不能大于 30；扫描周期不能小于 5',
  })
})

it('translates structured business errors into readable Chinese', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: 'Password verification failed',
  }), {
    status: 403,
    headers: { 'Content-Type': 'application/json' },
  })))

  await expect(api.dashboard()).rejects.toMatchObject({ message: '操作密码验证失败' })
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

it('sends the operator password for manual testnet entry', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)

  await api.manualEntry({
    operation_id: 'manual-entry-123',
    symbol: 'BTCUSDT',
    side: 'LONG',
    leverage: 2,
    stop_distance_pct: 1,
    tp1_r: 1,
    tp2_r: 2,
    password: 'operator-password',
  })

  const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
  expect(JSON.parse(String(init.body))).toMatchObject({ password: 'operator-password' })
  expect(JSON.parse(String(init.body))).not.toHaveProperty('confirmation')
})

it('does not echo read-only model metadata when saving risk configuration', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)

  await api.updateConfig({
    max_leverage: 20,
    model_name: 'gpt-5.6',
    strategy_profile: 'trend_following',
    password: 'operator-password',
  })

  const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
  expect(JSON.parse(String(init.body))).toEqual({
    max_leverage: 20,
    password: 'operator-password',
  })
})
