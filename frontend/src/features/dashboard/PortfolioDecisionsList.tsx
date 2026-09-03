import { AlertTriangle, Layers3 } from 'lucide-react'
import type { DashboardData, PortfolioDecision } from '../../api/types'
import { formatPercent, formatTime, parseApiDate } from '../signals/signalPresentation'

type CycleStatus = DashboardData['cycle_status']

const regimeLabel: Record<PortfolioDecision['market_regime'], string> = {
  TRENDING: '趋势',
  RANGING: '震荡',
  VOLATILE: '高波动',
  UNCERTAIN: '不确定',
}

const statusLabel: Record<string, string> = {
  APPROVED: '风控已批准（待成交）',
  PARTIALLY_APPROVED: '部分通过（待成交）',
  NO_ACTION: '模型建议空仓（未下单）',
  REJECTED: '模型意图被风控拒绝（未下单）',
  EXECUTED: '已成交',
  FAILED: '执行失败',
}

const statusClass = (status: string) => {
  if (status === 'APPROVED' || status === 'EXECUTED') return 'approved'
  if (status === 'NO_ACTION') return 'no_action'
  return 'rejected'
}

function decisionStatus(decision: PortfolioDecision) {
  const executions = decision.allocations
    .map((allocation) => allocation.execution?.status)
    .filter(Boolean)
  if (executions.includes('FAILED')) return { label: statusLabel.FAILED, status: 'FAILED' }
  if (executions.includes('EXECUTED')) return { label: statusLabel.EXECUTED, status: 'EXECUTED' }
  if (executions.includes('NO_FILL')) {
    return { label: '风控已批准，本轮未成交', status: 'NO_ACTION' }
  }
  return { label: statusLabel[decision.status] ?? decision.status, status: decision.status }
}

const cycleNoticeStates = new Set([
  'RUNNING',
  'NO_CANDIDATES',
  'MODEL_UNAVAILABLE',
  'MODEL_TIMEOUT',
  'MODEL_THROTTLED',
  'BLOCKED_RECONCILIATION',
  'EXCHANGE_UNAVAILABLE',
  'WORKER_BUSY',
  'WORKER_INTERRUPTED',
  'FAILED',
])

const cycleNoticeLabel: Record<string, string> = {
  RUNNING: '本轮组合决策仍在生成',
  NO_CANDIDATES: '本轮没有候选，未生成组合决策',
  MODEL_UNAVAILABLE: '本轮模型不可用，未生成组合决策',
  MODEL_TIMEOUT: '本轮模型响应超时，未生成组合决策',
  MODEL_THROTTLED: '本轮模型请求被节流，未生成组合决策',
  BLOCKED_RECONCILIATION: '本轮等待仓位对账，未生成组合决策',
  EXCHANGE_UNAVAILABLE: '本轮交易所不可用，未生成组合决策',
  WORKER_BUSY: '上一轮仍在执行，本轮未生成组合决策',
  WORKER_INTERRUPTED: '本轮 Worker 中断，未生成组合决策',
  FAILED: '本轮异常，未生成组合决策',
}

function isNewerCycle(cycle: CycleStatus, latest: PortfolioDecision | undefined) {
  if (!cycle.started_at) return false
  if (!latest) return true
  return parseApiDate(cycle.started_at).getTime() > parseApiDate(latest.created_at).getTime()
}

export function PortfolioDecisionsList({
  decisions,
  cycleStatus,
}: {
  decisions?: PortfolioDecision[]
  cycleStatus: CycleStatus
}) {
  const rows = decisions ?? []
  const latest = rows[0]
  const showCycleNotice = cycleNoticeStates.has(cycleStatus.state) && isNewerCycle(cycleStatus, latest)

  return (
    <div className="portfolio-decision-list">
      {showCycleNotice ? (
        <article className={'portfolio-cycle-note portfolio-cycle-note-' + cycleStatus.state.toLowerCase()} role="status">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <strong>{cycleNoticeLabel[cycleStatus.state] ?? cycleStatus.state}</strong>
            <span>{cycleStatus.detail}</span>
            {cycleStatus.finished_at || cycleStatus.started_at ? (
              <time>{formatTime(cycleStatus.finished_at ?? cycleStatus.started_at ?? '')}</time>
            ) : null}
          </div>
        </article>
      ) : null}

      {rows.length ? rows.slice(0, 3).map((decision) => {
        const displayStatus = decisionStatus(decision)
        return <article className="portfolio-decision-entry" key={decision.id}>
          <div className="portfolio-decision-head">
            <div>
              <Layers3 size={15} aria-hidden="true" />
              <strong>{regimeLabel[decision.market_regime]}组合</strong>
              <span className={'result-text ' + statusClass(displayStatus.status)}>
                {displayStatus.label}
              </span>
            </div>
            <time>{formatTime(decision.created_at)}</time>
          </div>
          <p>{decision.payload.summary}</p>
          <div className="portfolio-decision-meta">
            <span>风险预算 {formatPercent(decision.portfolio_risk_budget_fraction, 0)}</span>
            <span>{decision.payload.allocations.length} 个目标</span>
            <span>{decision.model_name}</span>
          </div>
          <div className="portfolio-decision-targets">
            {decision.payload.allocations.slice(0, 4).map((allocation) => (
              <span key={allocation.allocation_id}>
                模型建议：{allocation.symbol} · {allocation.target_side} · {formatPercent(allocation.allocation_fraction, 0)}
              </span>
            ))}
          </div>
        </article>
      }) : !showCycleNotice ? (
        <div className="compact-empty">暂无组合决策</div>
      ) : null}

      {rows.length ? (
        <a className="portfolio-decision-link" href="/portfolio">查看完整组合审计 →</a>
      ) : null}
    </div>
  )
}
