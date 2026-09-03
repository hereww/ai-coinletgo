import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SettingsPage from './SettingsPage'

const apiMock = vi.hoisted(() => ({
  dashboard: vi.fn(),
  integrations: vi.fn(),
  logout: vi.fn(),
  probeIntegration: vi.fn(),
  selectModelProfile: vi.fn(),
  updateModelIntegration: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

const dashboard = {
  mode: 'TESTNET',
  environment: 'testnet',
  health: { ready: false, components: [], checked_at: new Date().toISOString() },
  uptime_seconds: 120,
}

const integrations = {
  proxy: {
    enabled: false,
    configured: false,
    scope: '未启用代理',
    detail: 'HTTP 代理未启用',
  },
  binance: {
    environment: 'testnet',
    configured: false,
    base_url: 'https://testnet.binancefuture.com',
    health: { name: 'binance', state: 'NOT_CONFIGURED', detail: 'Binance credentials not configured', latency_ms: 1, checked_at: new Date().toISOString() },
  },
  model: {
    configured: false,
    active_profile: 'relay',
    active_label: 'OpenAI 中转',
    profiles: [
      { id: 'relay', label: 'OpenAI 中转', kind: 'relay', base_url: null, model_name: 'gpt-5.6', api_key_configured: false, configured: false, active: true },
      { id: 'vllm', label: '自建 vLLM', kind: 'self_hosted', base_url: 'http://vllm.example/v1', model_name: 'Qwen/Qwen3.8-27B-FP8', api_key_configured: true, configured: true, active: false },
    ],
    base_url: null,
    model_name: 'gpt-5.6',
    reasoning_effort: 'medium',
    timeout_seconds: 45,
    strategy_profile: 'trend_following',
    api_key_configured: false,
    health: { name: 'model_relay', state: 'NOT_CONFIGURED', detail: 'model relay not configured', latency_ms: null, checked_at: new Date().toISOString() },
  },
}

function renderPage() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SettingsPage /></QueryClientProvider>)
}

afterEach(cleanup)

beforeEach(() => {
  vi.clearAllMocks()
  apiMock.dashboard.mockResolvedValue(dashboard)
  apiMock.integrations.mockResolvedValue(integrations)
  apiMock.probeIntegration.mockResolvedValue({ name: 'binance', state: 'NOT_CONFIGURED', detail: 'missing' })
  apiMock.selectModelProfile.mockResolvedValue({})
  apiMock.updateModelIntegration.mockResolvedValue({})
})

it('shows testnet and model integration state and runs a non-ordering testnet probe', async () => {
  renderPage()
  expect(await screen.findByText('HTTP 代理模式')).toBeInTheDocument()
  expect(screen.getByText('未启用代理')).toBeInTheDocument()
  expect(screen.getByText('币安测试网')).toBeInTheDocument()
  expect(screen.getByText('AI 模型 / Responses API')).toBeInTheDocument()
  expect(screen.getByText('Qwen/Qwen3.8-27B-FP8')).toBeInTheDocument()
  expect(screen.getByLabelText('AI 策略模板')).toHaveValue('trend_following')
  expect(screen.getByText('模型调用额度')).toBeInTheDocument()
  expect(screen.getByText('不限制')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '测试 Binance 连接' }))
  await waitFor(() => expect(apiMock.probeIntegration).toHaveBeenCalledWith('testnet'))
})

it('saves model settings directly without an extra confirmation dialog', async () => {
  renderPage()
  const baseUrl = await screen.findByLabelText('OpenAI 中转 Base URL')
  fireEvent.change(baseUrl, { target: { value: 'https://relay.example.com/v1' } })
  fireEvent.change(screen.getByLabelText('AI 策略模板'), { target: { value: 'balanced' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略与中转设置' }))
  await waitFor(() => expect(apiMock.updateModelIntegration).toHaveBeenCalledWith(expect.objectContaining({
    base_url: 'https://relay.example.com/v1',
    model_name: 'gpt-5.6',
    strategy_profile: 'balanced',
  })))
  expect(apiMock.updateModelIntegration.mock.calls[0][0]).not.toHaveProperty('api_key')
})

it('switches to a configured model profile without sending an api key', async () => {
  renderPage()
  fireEvent.click(await screen.findByRole('button', { name: '切换到此模型' }))
  await waitFor(() => expect(apiMock.selectModelProfile).toHaveBeenCalledWith('vllm'))
})
