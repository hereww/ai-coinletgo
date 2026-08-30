import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Activity,
  BarChart3,
  BookOpenCheck,
  Bot,
  ChevronLeft,
  ClipboardList,
  Boxes,
  Gauge,
  History,
  Menu,
  Settings,
  ShieldCheck,
  WalletCards,
  X,
} from 'lucide-react'
import { NavLink } from 'react-router-dom'

const navItems = [
  { to: '/', label: '总览', icon: Gauge },
  { to: '/market', label: '市场', icon: BarChart3 },
  { to: '/signals', label: '信号', icon: Bot },
  { to: '/portfolio', label: '组合决策', icon: Boxes },
  { to: '/positions', label: '仓位', icon: WalletCards },
  { to: '/orders', label: '订单', icon: ClipboardList },
  { to: '/replay', label: '历史回放', icon: History },
  { to: '/risk', label: '风控', icon: ShieldCheck },
  { to: '/audit', label: '审计', icon: BookOpenCheck },
  { to: '/settings', label: '设置', icon: Settings },
]

export function AppShell({ children, user, environment, healthReady }: { children: ReactNode; user: string; environment: string; healthReady?: boolean }) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [now, setNow] = useState(() => new Date())
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const closeMobileNavigation = () => {
    setMobileOpen(false)
    menuButtonRef.current?.focus()
  }
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1_000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    if (!mobileOpen) return
    closeButtonRef.current?.focus()
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeMobileNavigation()
      }
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [mobileOpen])
  const beijingTime = useMemo(() => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(now), [now])
  const healthClass = healthReady === true ? 'healthy' : healthReady === false ? 'warning' : 'unknown'
  const healthLabel = healthReady === true ? '门禁正常' : healthReady === false ? '门禁未通过' : '门禁检查中'

  return (
    <div className="app-shell">
      <aside id="primary-sidebar" className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true"><Activity size={18} strokeWidth={2} /></div>
          <span>合约风控台</span>
          <button ref={closeButtonRef} className="icon-button mobile-only" onClick={closeMobileNavigation} aria-label="关闭导航"><X size={18} /></button>
        </div>
        <nav className="primary-nav" aria-label="主导航">
          {navItems.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} onClick={() => setMobileOpen(false)}>
              <Icon size={17} strokeWidth={1.8} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="operator-line"><span className="operator-dot" />{user}</div>
          <div className="version-line">console v0.1</div>
        </div>
      </aside>

      {mobileOpen ? <button className="sidebar-scrim" aria-label="关闭导航" onClick={closeMobileNavigation} /> : null}

      <div className="main-column">
        <header className="topbar">
          <button ref={menuButtonRef} className="icon-button mobile-only" onClick={() => setMobileOpen(true)} aria-label="打开导航" aria-controls="primary-sidebar" aria-expanded={mobileOpen}><Menu size={19} /></button>
          <div className="environment-state">
            <span className={`status-dot ${healthClass}`} aria-label={healthLabel} />
            <strong>{environment === 'live' ? '实盘环境' : '测试网'}</strong>
          </div>
          <div className="topbar-separator" />
          <div className="locked-state"><ShieldCheck size={15} /> {healthLabel}</div>
          <div className="topbar-spacer" />
          <div className="clock">北京时间 {beijingTime}</div>
          <button className="icon-button desktop-only" aria-label="收起侧栏" disabled><ChevronLeft size={17} /></button>
        </header>
        <main className="workspace">{children}</main>
      </div>
    </div>
  )
}
