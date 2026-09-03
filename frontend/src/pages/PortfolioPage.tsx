import { Fragment, useState } from 'react'
import { ChevronDown, Layers3 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { PageError, PageLoading } from '../components/PageState'
import { formatTime } from '../features/signals/signalPresentation'
import type { PortfolioDecision } from '../api/types'
import { CycleStatusBanner } from '../features/cycle/CycleStatusBanner'

const regimeLabel: Record<PortfolioDecision['market_regime'], string> = {
  TRENDING: '趋势',
  RANGING: '震荡',
  VOLATILE: '高波动',
  UNCERTAIN: '不确定',
}

const statusLabel: Record<string, string> = {
  APPROVED: '已批准',
  PARTIALLY_APPROVED: '部分批准',
  NO_ACTION: '无调仓',
  REJECTED: '已拒绝',
  EXECUTED: '已执行',
  FAILED: '执行失败',
}

export default function PortfolioPage() {
  const decisions = useQuery({
    queryKey: ['portfolio-decisions'],
    queryFn: api.portfolioDecisions,
    refetchInterval: 15_000,
  })
  const cycle = useQuery({
    queryKey: ['cycle-status'],
    queryFn: api.cycleStatus,
    refetchInterval: 15_000,
  })
  const [expanded, setExpanded] = useState<string | null>(null)

  if (decisions.isLoading) {
    return <><PageHeader title="组合决策" subtitle="AI 目标、风险预算与实际调仓计划" /><PageLoading /></>
  }
  if (decisions.isError) {
    return <><PageHeader title="组合决策" subtitle="AI 目标、风险预算与实际调仓计划" /><PageError message={decisions.error.message} retry={() => decisions.refetch()} /></>
  }
  const rows = decisions.data ?? []
  return <>
    <PageHeader title="组合决策" subtitle="AI 目标、风险预算与实际调仓计划" />
    {cycle.data ? <CycleStatusBanner status={cycle.data} /> : null}
    <section className="surface page-surface portfolio-page">
      <div className="section-head"><h2><Layers3 size={16} /> 最近组合决策</h2><span>{rows.length} 条</span></div>
      {!rows.length ? <EmptyState title="还没有组合决策" detail="开启 Portfolio-v1 后，模型的组合意图和确定性裁剪结果会显示在这里。" /> : <div className="table-scroll"><table className="data-table"><thead><tr><th>时间</th><th>状态</th><th>市场状态</th><th>风险预算</th><th>模型</th><th>分配</th><th>详情</th></tr></thead><tbody>{rows.map((decision) => {
        const isOpen = expanded === decision.id
        const payload = decision.payload
        return <Fragment key={decision.id}>
          <tr key={decision.id}>
            <td>{formatTime(decision.created_at, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</td>
            <td><span className={`result-text ${decision.status.toLowerCase()}`}>{statusLabel[decision.status] ?? decision.status}</span></td>
            <td>{regimeLabel[decision.market_regime]}</td>
            <td className="mono">{(Number(decision.portfolio_risk_budget_fraction) * 100).toFixed(0)}%</td>
            <td>{decision.model_name}</td>
            <td>{decision.payload.allocations.length} 个</td>
            <td><button className="table-expand-button" type="button" onClick={() => setExpanded(isOpen ? null : decision.id)}><ChevronDown size={14} className={isOpen ? 'rotated' : ''} />{isOpen ? '收起' : '展开'}</button></td>
          </tr>
          {isOpen ? <tr key={`${decision.id}-details`} className="signal-detail-table-row"><td colSpan={7}><div className="portfolio-detail"><p>{payload.summary}</p><div className="portfolio-target-strip">{payload.allocations.map((allocation) => <span key={allocation.allocation_id}><strong>{allocation.symbol}</strong> {allocation.target_side} · {(Number(allocation.allocation_fraction) * 100).toFixed(0)}%</span>)}</div><div className="portfolio-allocation-grid">{decision.allocations.map((allocation) => {
            const action = allocation.action ?? 'AI_TARGET'
            const reasons = allocation.reasons?.length
              ? allocation.reasons
              : [...(allocation.reason_codes ?? []), ...(allocation.risk_flags ?? [])]
            const hasCompiledAction = Boolean(allocation.action)
            return <div className="portfolio-allocation" key={allocation.action_id}><div><strong>{allocation.symbol}</strong><span className={`result-text ${action.toLowerCase()}`}>{hasCompiledAction ? action : 'AI目标'}</span></div><dl><div><dt>方向</dt><dd>{allocation.side ?? allocation.target_side ?? '—'}</dd></div><div><dt>当前 → 目标</dt><dd>{allocation.current_quantity != null ? `${allocation.current_quantity} → ${allocation.target_quantity ?? '—'}` : '尚未编译'}</dd></div><div><dt>风险</dt><dd>{allocation.target_risk_usdt != null ? `${allocation.target_risk_usdt} USDT` : `${((Number(allocation.allocation_fraction ?? 0)) * 100).toFixed(0)}%（待编译）`}</dd></div><div><dt>原因</dt><dd>{reasons.join('、') || allocation.thesis || (hasCompiledAction ? '—' : 'AI 意图已记录，等待确定性风控结果')}</dd></div></dl>{allocation.execution ? <small>执行：{allocation.execution.status} · {allocation.execution.order_ids.join(', ') || '无订单'}</small> : <small>{hasCompiledAction ? '尚无执行记录' : '本轮未生成确定性调仓动作'}</small>}</div>
          })}</div><small>失效时间：{formatTime(payload.expires_at, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })} · 提示词：{decision.prompt_version}</small></div></td></tr> : null}
        </Fragment>
      })}</tbody></table></div>}
    </section>
  </>
}
