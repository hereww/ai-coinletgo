import { lazy, Suspense, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LockKeyhole, OctagonAlert, Pause, Play, Plus, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import { Button } from '../components/Button'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { HealthList } from '../features/dashboard/HealthList'
import { ManualEntryDialog } from '../features/dashboard/ManualEntryDialog'
import { MetricStrip } from '../features/dashboard/MetricStrip'
import { PositionsTable } from '../features/dashboard/PositionsTable'
import { PortfolioDecisionsList } from '../features/dashboard/PortfolioDecisionsList'
import { RiskCapacity } from '../features/dashboard/RiskCapacity'
import { SignalsList } from '../features/dashboard/SignalsList'

type Dialog = 'flatten' | 'unlock' | 'reconcile' | 'resume' | 'cycle' | 'manual-entry' | null

const EquityChart = lazy(() => import('../features/dashboard/EquityChart').then((module) => ({ default: module.EquityChart })))

export default function DashboardPage() {
  const queryClient = useQueryClient()
  const [dialog, setDialog] = useState<Dialog>(null)
  const dashboard = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard, refetchInterval: 15_000 })
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  const pause = useMutation({ mutationFn: api.pause, onSuccess: refresh })
  const resume = useMutation({ mutationFn: (password: string) => api.resumeTestnet(password), onSuccess: () => { setDialog(null); refresh() } })
  const reconcile = useMutation({ mutationFn: api.reconcilePositions, onSuccess: () => { setDialog(null); refresh() } })
  const flatten = useMutation({ mutationFn: api.flatten, onSuccess: () => { setDialog(null); refresh() } })
  const unlock = useMutation({ mutationFn: api.unlockLive, onSuccess: () => { setDialog(null); refresh() } })
  const runCycle = useMutation({
    mutationFn: (password: string) => api.runCycle(password),
    onSuccess: () => { setDialog(null); refresh() },
  })

  if (dashboard.isLoading) return <><PageHeader title="合约风控台" subtitle="正在同步运行环境" /><PageLoading /></>
  if (dashboard.isError || !dashboard.data) return <><PageHeader title="合约风控台" subtitle="运行状态不可用" /><PageError message={dashboard.error?.message ?? '控制台数据不可用'} retry={() => dashboard.refetch()} /></>
  const data = dashboard.data
  // Keep the console compatible with an older API response during a rolling
  // deploy or with cached test fixtures.  Missing cycle status means unknown,
  // never an interrupted model decision.
  const cycleStatus = data.cycle_status ?? {
    state: 'UNKNOWN' as const,
    detail: '等待 Worker 周期状态（旧版本接口未提供）',
    started_at: null,
    finished_at: null,
    snapshots: 0,
    candidates: 0,
    signals: 0,
    approved: 0,
    executed: 0,
    failed: false,
  }
  // A timeout/interrupted cycle may have no persisted PortfolioDecision. In
  // that case the legacy signal history must not masquerade as the current
  // model decision; show the current cycle status instead.
  const latestPortfolioCreatedAt = data.portfolio_decisions?.[0]?.created_at
  const hasNewerPortfolioCycle = Boolean(
    cycleStatus.started_at
      && (!latestPortfolioCreatedAt
        || new Date(cycleStatus.started_at).getTime() > new Date(latestPortfolioCreatedAt).getTime()),
  )
  const showPortfolioDecisionView = Boolean(data.portfolio_decisions?.length) || hasNewerPortfolioCycle
  const paused = ['PAUSED', 'RISK_HALTED', 'RECONCILIATION_REQUIRED'].includes(data.mode)
  const reconciliationBlocked = data.mode === 'RECONCILIATION_REQUIRED'
  const cycleStateLabel: Record<typeof cycleStatus.state, string> = {
    RUNNING: '本轮执行中',
    COMPLETED: '本轮已完成',
    EXECUTED: '本轮已有执行',
    NO_CANDIDATES: '本轮无候选，未调用模型',
    RISK_REJECTED: '模型已返回，但被硬风控拒绝',
    MODEL_UNAVAILABLE: '模型不可用，本轮未增险',
    MODEL_TIMEOUT: '模型响应超时，本轮未增险',
    MODEL_THROTTLED: '模型节流中，本轮未重复调用',
    BLOCKED_RECONCILIATION: '需先完成仓位对账，本轮未调用模型',
    EXCHANGE_UNAVAILABLE: '交易所暂不可用，本轮未调用模型',
    WORKER_BUSY: '上一轮仍在执行，本轮未重复启动',
    WORKER_INTERRUPTED: 'Worker 中断，本轮未增险',
    FAILED: '本轮异常，已安全停止',
    UNKNOWN: '等待 Worker 状态',
  }

  return (
    <>
      <div className="page-head">
        <div><h1>合约风控台</h1><p>{data.environment === 'testnet' ? '测试网' : '实盘'} · {data.mode.replaceAll('_', ' ')}</p></div>
        <div className="command-row">
          <Button variant="ghost" icon={<RefreshCw size={15} />} onClick={() => dashboard.refetch()} aria-label="刷新数据">刷新</Button>
          {data.environment === 'testnet' ? <Button icon={<Plus size={15} />} onClick={() => setDialog('manual-entry')} disabled={!data.health.ready || data.mode !== 'TESTNET'}>手动开仓</Button> : null}
          <Button icon={<Play size={15} />} onClick={() => setDialog('cycle')} disabled={!data.health.ready || runCycle.isPending || reconciliationBlocked}>{runCycle.isPending ? '分析执行中...' : reconciliationBlocked ? '完成仓位对账后再分析' : data.environment === 'testnet' && data.mode === 'PAUSED' ? '恢复并分析' : '立即分析并执行'}</Button>
          {data.mode === 'RECONCILIATION_REQUIRED' ? <Button icon={<Play size={15} />} onClick={() => setDialog('reconcile')}>确认仓位状态</Button> : paused ? <Button icon={<Play size={15} />} onClick={() => setDialog('resume')} disabled={resume.isPending}>{resume.isPending ? '恢复中...' : '恢复运行'}</Button> : <Button icon={<Pause size={15} />} onClick={() => pause.mutate()} disabled={pause.isPending}>暂停开仓</Button>}
          {data.environment === 'live' ? <Button icon={<LockKeyhole size={15} />} onClick={() => setDialog('unlock')}>解锁实盘</Button> : null}
          <Button variant="danger" icon={<OctagonAlert size={15} />} onClick={() => setDialog('flatten')}>紧急清仓</Button>
        </div>
        {resume.error ? <div className="inline-error" role="alert">{resume.error.message}</div> : null}
        {runCycle.error ? <div className="inline-error" role="alert">{runCycle.error.message}</div> : null}
        <div className={'cycle-status cycle-status-' + cycleStatus.state.toLowerCase()} role="status">
          <strong>{cycleStateLabel[cycleStatus.state]}</strong>
          <span>{cycleStatus.detail}</span>
          <small>快照 {cycleStatus.snapshots} · 候选 {cycleStatus.candidates} · 模型输出 {cycleStatus.signals} · 执行 {cycleStatus.executed}</small>
        </div>
        {data.halt_reason ? <div className="inline-warning" role="status">系统暂停原因：{data.halt_reason}</div> : null}
      </div>

      <MetricStrip metrics={data.metrics} />

      <section className="dashboard-grid top-grid">
        <div className="surface chart-surface"><div className="section-head"><h2>净值与回撤</h2><div className="legend"><span className="equity-line" />净值<span className="drawdown-line" />回撤</div></div><Suspense fallback={<div className="chart-wrap chart-empty" role="status">正在加载图表...</div>}><EquityChart data={data.equity_curve} /></Suspense></div>
        <div className="surface risk-surface"><div className="section-head"><h2>风险容量</h2><span>{data.health.ready ? '门禁正常' : '门禁未通过'}</span></div><RiskCapacity data={data.risk_capacity} /></div>
      </section>

      <section className="surface positions-surface"><div className="section-head"><h2>当前仓位</h2><span>{data.positions.length} / 3</span></div><PositionsTable positions={data.positions} /></section>

      <section className="dashboard-grid lower-grid">
        <div className="surface"><div className="section-head"><h2>{showPortfolioDecisionView ? '最近组合决策' : '最近模型决策'}</h2><span>{showPortfolioDecisionView ? 'Portfolio-v1 · 15 分钟周期' : '兼容历史信号'}</span></div>{showPortfolioDecisionView ? <PortfolioDecisionsList decisions={data.portfolio_decisions} cycleStatus={cycleStatus} /> : <SignalsList signals={data.signals} />}</div>
        <div className="surface"><div className="section-head"><h2>系统健康</h2><span>{data.health.ready ? '全部正常' : '需要处理'}</span></div><HealthList components={data.health.components} /></div>
      </section>

      <ConfirmDialog open={dialog === 'flatten'} title="紧急清仓" body="系统将暂停新开仓，并以市价关闭专用子账户中的全部受管仓位。" confirmLabel="立即清仓" requirePassword danger busy={flatten.isPending} error={flatten.error?.message} onClose={() => setDialog(null)} onConfirm={(password) => flatten.mutate(password)} />
      <ConfirmDialog open={dialog === 'unlock'} title="解锁实盘" body="只有在币安、模型、数据库、时间同步和认证健康检查全部通过时，实盘才会启用。" confirmLabel="检查并解锁" requirePassword busy={unlock.isPending} error={unlock.error?.message} onClose={() => setDialog(null)} onConfirm={(password) => unlock.mutate(password)} />
      <ConfirmDialog open={dialog === 'reconcile'} title="确认交易所仓位" body="系统会重新读取币安仓位。只有每个仓位都存在可识别的交易所端硬止损时，才会接受当前状态。" confirmLabel="验证并确认" requirePassword busy={reconcile.isPending} error={reconcile.error?.message} onClose={() => setDialog(null)} onConfirm={(password) => reconcile.mutate(password)} />
      <ConfirmDialog open={dialog === 'resume'} title="恢复测试网运行" body="系统将恢复测试网自动交易。下一轮周期可能根据 AI 组合决策产生开仓、加仓或调仓动作。" confirmLabel="确认恢复" requirePassword busy={resume.isPending} error={resume.error?.message} onClose={() => setDialog(null)} onConfirm={(password) => resume.mutate(password)} />
      <ConfirmDialog open={dialog === 'cycle'} title={data.environment === 'testnet' && data.mode === 'PAUSED' ? '恢复并分析' : '立即分析并执行'} body="系统将进行一轮完整市场扫描、AI 组合决策、确定性风控和保护单校验；符合条件时可能提交测试网订单。" confirmLabel={data.environment === 'testnet' && data.mode === 'PAUSED' ? '确认恢复并分析' : '确认分析执行'} requirePassword busy={runCycle.isPending} error={runCycle.error?.message} onClose={() => setDialog(null)} onConfirm={(password) => runCycle.mutate(password)} />
      <ManualEntryDialog open={dialog === 'manual-entry'} onClose={() => setDialog(null)} />
    </>
  )
}
