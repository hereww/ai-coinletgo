import { lazy, Suspense, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api, apiBaseUrl, ApiError } from './api/client'
import { AppShell } from './layout/AppShell'
import { LoginPage } from './pages/LoginPage'

const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const MarketPage = lazy(() => import('./pages/MarketPage'))
const SignalsPage = lazy(() => import('./pages/SignalsPage'))
const PortfolioPage = lazy(() => import('./pages/PortfolioPage'))
const PositionsPage = lazy(() => import('./pages/PositionsPage'))
const OrdersPage = lazy(() => import('./pages/OrdersPage'))
const ReplayPage = lazy(() => import('./pages/ReplayPage'))
const RiskPage = lazy(() => import('./pages/RiskPage'))
const AuditPage = lazy(() => import('./pages/AuditPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))

function App() {
  const [loginVersion, setLoginVersion] = useState(0)
  const [connectionRetry, setConnectionRetry] = useState(0)
  useEffect(() => {
    const handleUnauthorized = () => setLoginVersion((value) => value + 1)
    window.addEventListener('frc:unauthorized', handleUnauthorized)
    return () => window.removeEventListener('frc:unauthorized', handleUnauthorized)
  }, [])
  const user = useQuery({
    queryKey: ['me', loginVersion, connectionRetry],
    queryFn: api.me,
    retry: false,
  })
  const health = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard, enabled: Boolean(user.data), staleTime: 10_000 })

  if (user.isLoading) return <div className="app-loading">正在连接控制台...</div>
  if (user.isError && (user.error as ApiError).status !== 401) {
    const error = user.error as ApiError
    return (
      <main className="connection-shell">
        <section className="connection-panel" role="alert">
          <h1>无法连接控制台</h1>
          <p>{error.message}</p>
          <code>{apiBaseUrl || '当前页面同源 API'}</code>
          <button className="button button-primary" type="button" onClick={() => setConnectionRetry((value) => value + 1)}>重新连接</button>
        </section>
      </main>
    )
  }
  if (user.isError || !user.data) return <LoginPage onSuccess={() => setLoginVersion((value) => value + 1)} />

  return (
    <AppShell user={user.data.username} environment={user.data.environment} healthReady={health.data?.health.ready}>
      <Suspense fallback={<div className="route-loading">正在加载...</div>}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/market" element={<MarketPage />} />
          <Route path="/signals" element={<SignalsPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/orders" element={<OrdersPage />} />
          <Route path="/replay" element={<ReplayPage />} />
          <Route path="/risk" element={<RiskPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </AppShell>
  )
}

export default App
